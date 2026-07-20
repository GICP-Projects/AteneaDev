from app_telegram.models import SeedItem, TelegramAuth, RoomItem, UserItem, MessageItem
from app_telegram.services.telegram import (
    items_to_index, 
    access_entities,
    populate_seeds, 
    scan_rooms, 
    scan_comments,
    postprocess_msgs
)
from app_entity.services.ner import ner_db_store
from app_telegram.serializers import ANY as TAG_ANY
from app_base.api import create_advance_filter, bulk_add_query_relationships_with_pks
from celery.app import shared_task
from celery import group, chord, chain
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.db.models import Prefetch
import logging
import math


# Get an instance of a logger
logger = logging.getLogger(__name__)


def run_generic_pipeline(
    main_task_func, 
    queryset,
    load_balancer_func,
    tasks_signatures_to_chain = [], 
    callback_task_func = None,
    extra_args = {}, 
):
    """This function wraps common functionality related to the creation of celery 
    pipelines to exploid the Telegram API. It allows to customize the pipeline by 
    adding a main task, which use the Telegram API, and a chain of tasks to process 
    the data from the main one. Finally, it allows to add a callback task to be executed 
    after the whole pipeline is completed.
     
    It also divides the workload in blocks of the same size and, to reduce speed 
    limits, it rotate all valid TelegramAuth credentials and use a different one   
    in each block (repeating them in a loop).

    If extra_args contains the query `token`, it will be related to all the items
    to be processed (in the main_task_func), NOTE: these elements may not be all 
    the elements of the queryset 
    
    Parameters
    ----------
    main_task_func: celery.task
        This function contains the first task that is going to be executed in the 
        pipeline, is going to receive all the arguments and must have the 
        following parameters structure:
            - First parameter must be a List of items (in dictionary format)
            - Second parameter must be 'auth_key' parameter (dictionary from a TelegramAuth item)
            - Any other argument must be set in the 'extra_args' dictionary parameter

    queryset: django.db.models.QuerySet
        QuerySet of items to be processed by the main task. It must use `.values()`
        or `.values_list()` (to be able to send JSON serialized data to the tasks).

    load_balancer_func: function
        This function must receive the following parameters in order:
            - tauth_list: A list of TelegramAuth items in dictionary format 
            (["pk", "name", "api_id", "api_hash", "session"] fields)
            - The `main_task_func` parameter
            - The `queryset` parameter
            - The `tasks_signatures_to_chain` parameter
            - The `extra_args` parameter 
        It will split the workload and return the following parameters:
            - tasks: A list of tasks to group (with all the workload splitted)
            - assigned_items_pk: A list of primary keys of the assigned items from `queryset`

    tasks_signatures_to_chain: list[celery.canvas.Signature], default=[]
        List of celery task signatures: [task.s(....), ...] that are going to be chained.
        This tasks already have their arguments defined.
        
        The elements in the list are going to be chained in order.
        
    extra_args: dict, default={}
        All additional arguments to be sent to the main celery `main_task_func` 
        task. e.g: token, max_msgs etc..

    Returns
    ----------
    count: int
        Number of items to be processed.
    """

    # Extract total items count and exit if there is nothing to process
    if not queryset.exists():
        return 0

    
    # Split the workload in tasks with an custom load balancer function
    tasks, assigned_items_pks = load_balancer_func(
        main_task_func, 
        queryset, 
        tasks_signatures_to_chain, 
        extra_args
    )

    # Execute pipeline 
    if callback_task_func:
        chord(group(tasks))(callback_task_func.s())
    else:
        group(tasks).apply_async()

    """
    if extra_args.get('token'):
        result = jobs.apply_async(task_id=extra_args.get('token'))
        result.save()  # for been able to retrieve its data later if its needed
    else:
        jobs.apply_async()
    """

    # Bind the Query to all the affected items involved (only the `assigned_items`)
    if extra_args.get("token", None):
        bulk_add_query_relationships_with_pks(
            items_pks=assigned_items_pks,
            item_content_type=ContentType.objects.get_for_model(queryset.model),
            query_pk=extra_args["token"]
        )

    return len(assigned_items_pks)


def load_balancer_split_by_auth(
    main_task_func,
    queryset,
    tasks_signatures_to_chain,
    extra_args,
):
    """ Load balancing for main_task_func that makes use of "ResolveUsername" API call 
    (by using `get_entity()` method), this is a powerful API call that allows to get 
    an access_hash from a user/room name (this hash is related to an specific auth).

    Telegram limits the number of “ResolveUsername” to 200 per session per day, 
    so only 200*N_credential items can be processed.  

    Parameters
    ----------
    main_task_func: celery.task
        This function contains the first task that is going to be executed.
        More information about this parameter can be found in the 
        `run_generic_pipeline` function.
    
    queryset: django.db.models.QuerySet
        QuerySet of items. More information about this parameter can be found 
        in the `run_generic_pipeline` function.

    tasks_signatures_to_chain: list[celery.canvas.Signature], default=[]
        List of celery task signatures to chain. More information about this 
        parameter can be found in the `run_generic_pipeline` function.

    extra_args: dict
        Extra arguments to be sent to the main celery `main_task_func` task. 
        More information about this parameter can be found in the 
        `run_generic_pipeline` function.

    Returns
    ----------
    tasks: list[celery.canvas.Signature]
        A list of tasks to group (with all the workload splitted)

    assigned_items_pk: list[uuid.UUID]
        A list of primary keys of the assigned items from `queryset`
    """

    tauth_list = list(
        # Filter by is_valid and by credentials without a FloodWaitError
        TelegramAuth.objects.filter(
            is_valid=True, wait_until__lte=timezone.now()
        ).values("pk", "name", "api_id", "api_hash", "session")
    )

    if not tauth_list:
        logger.info(
            "Unable to use Telegram API, system does not contain any valid authentication key."
        )
        return [],[]

    tasks = []
    # Determine the size of each block (Ensure doesn't exceed 200 by account ~ Telegram API limitation)
    # e.g: With 5 credentials the max amount of items are 1000 (the rest will be ignored)
    if len(queryset) > len(tauth_list): # `len` to force queryset evaluation
        MAX_ITEMS_BY_AUTH = min(len(queryset) // len(tauth_list), 200)
        MAX_ITEMS_TO_PROCESS = MAX_ITEMS_BY_AUTH*len(tauth_list)
    else:
        # If the queryset is smaller or equal than the number of credentials, the 
        # workload will be split by one item per credential (some credentials will be ignored)
        MAX_ITEMS_BY_AUTH = 1
        MAX_ITEMS_TO_PROCESS = len(queryset)
    for i in range(0, MAX_ITEMS_TO_PROCESS, MAX_ITEMS_BY_AUTH):
        main_task = main_task_func.s( 
            list(queryset[i:i+MAX_ITEMS_BY_AUTH]), # Queryset to list (is not JSON serializable)
            # Auth item to dict (model_to_dict don't return pk)
            tauth_list[i//MAX_ITEMS_BY_AUTH],
            **extra_args
        )
        # If there are additional tasks, apart from the main task, chain them
        if len(tasks_signatures_to_chain):
            tasks.append(chain([main_task] + [ce_task for ce_task in tasks_signatures_to_chain]))
        else:
            tasks.append(main_task)

    # Check excess items
    excess_items_count = queryset.count() - MAX_ITEMS_TO_PROCESS
    if excess_items_count:
        logger.info(
            f"{excess_items_count} items couldn't be assigned... No more available " 
            "Telegram auths (max 200 items per account)."
        )

    # Return pks (use .values_list("pk", flat=True) in case it hasn't already been done)
    assigned_items_pk = queryset.values_list("pk", flat=True)[:MAX_ITEMS_TO_PROCESS]
    return tasks, assigned_items_pk


def load_balancer_group_pks_by_auth(
    main_task_func,
    queryset,
    tasks_signatures_to_chain,
    extra_args,
):
    """ Load balancer designed to split the workload in N main_task_func tasks 
    according to the number of TelegramAuth items available. Each task will process
    all the items (that meet the filters) related to a TelegramAuth credential.

    Currently, TelegramAuth allows queryset of RoomItem and UserItem models. 

    NOTE: Only the primary keys from the queryset will be sent to the main_task_func
    task. 

    Parameters
    ----------
    main_task_func: celery.task
        This function contains the first task that is going to be executed.
        More information about this parameter can be found in the 
        `run_generic_pipeline` function.
    
    queryset: django.db.models.QuerySet
        QuerySet of items. More information about this parameter can be found 
        in the `run_generic_pipeline` function.
        NOTE: This queryset will be used inside a Prefetch, therefore, it can't 
        contain `.values()` or `.values_list()`. 

    tasks_signatures_to_chain: list[celery.canvas.Signature], default=[]
        List of celery task signatures to chain. More information about this 
        parameter can be found in the `run_generic_pipeline` function.

    extra_args: dict
        Extra arguments to be sent to the main celery `main_task_func` task. 
        More information about this parameter can be found in the 
        `run_generic_pipeline` function.

    Returns
    ----------
    tasks: list[celery.canvas.Signature]
        A list of tasks to group (with all the workload splitted)

    assigned_items_pk: list[uuid.UUID]
        A list of primary keys of the assigned items from `queryset`
    """

    # Related fields by its Modelclass inside the TelegramAuth model
    telegramAuth_available_relations = {
        RoomItem: 'roomitems_related',
        UserItem: 'useritems_related'
    }
    prefch_items = Prefetch(
        telegramAuth_available_relations[queryset.model], 
        queryset=queryset, 
        to_attr='prefetch_items'
    )
    tauth_items = (
        # Filter by is_valid and by credentials without a FloodWaitError
        # wait_until__lte=timezone.now() - wait_until only controls FloodWaitError about ResolveUsername
        TelegramAuth.objects.filter(is_valid=True).prefetch_related(prefch_items)
        #Prefetch don't allow .values("pk", "name", "api_id", "api_hash", "session")
    )

    if not tauth_items.exists():
        logger.info(
            "Unable to use Telegram API, system does not contain any valid authentication key."
        )
        return [],[]

    tasks = []
    assigned_items_pk = []
    for tauth in tauth_items:
        items_pks = [item.pk for item in tauth.prefetch_items]
        if items_pks:
            main_task = main_task_func.s( 
                items_pks,
                # TelegramAuth item to dict (model_to_dict don't return pk)
                {
                    "pk": tauth.pk,
                    "name": tauth.name,
                    "api_id": tauth.api_id,
                    "api_hash": tauth.api_hash,
                    "session": tauth.session
                },
                **extra_args
            )
            assigned_items_pk += items_pks
            # If there are additional tasks, apart from the main task, chain them
            if len(tasks_signatures_to_chain):
                tasks.append(chain([main_task] + [ce_task for ce_task in tasks_signatures_to_chain]))
            else:
                tasks.append(main_task)

    return tasks, assigned_items_pk


# ==========================================================
# ====               TELEGRAM PIPELINES                =====
# ==========================================================
# ====     Functions that wrap all the functionality   =====
# ====  needed to start pipelines related to Telegram  =====
# ====                 and its data.                   =====
# ==========================================================


# ==========================================================
# ====         TELEGRAM PIPELINES: INDEXATION          =====
# ==========================================================

@shared_task(track_started=True)
def index_msgs_pipeline(
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 500000,
    token = None
):
    """ MessageItem Indexation pipeline wrapper.
    It initializes the pipeline to index the messages that fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the indexation endpoint.

    Parameters
    ----------
    room: List[str]
        List of channel/group names from which messages are to be extracted. By 
        default, the list will be empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    is_reply: bool, default=None
        Filter messages that are replies to another message.
        
    createdat_min: datetime.datetime, default=None
        Interval start date for the created_at field

    createdat_max: datetime.datetime, default=None
        Interval start date for the created_at field

    lang: List[str], default=[]
        Language codes in ISO 639-1

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field
        
    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    block_size: int
        The size of each block of items to be processed in memory from the DB 
        at a time.

    token: string, default=None
        UUID.hex
    """
    
    and_filter_fields = {
        # Last update range filter
        "created_at__gte": createdat_min,
        "created_at__lte": createdat_max,
        "stored_date__gte": stored_since,
        "media_type__in": type,
        "lang__in": lang,
        "is_reply": is_reply
    }

    items_to_index.delay(
        query_filter_by_model={
            MessageItem.__name__: {
                "and_filter_fields": and_filter_fields,
                "list_filter_fields": {
                    "room__unique_name__iexact": {"values": [r.strip() for r in room], "OR": True},
                    "room__tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY},
                }
            }
        },
        apply_distinct=False, # No risk of duplicatos given the filters used
        block_size=block_size,
        token=token
    )


# ==========================================================
# ====            TELEGRAM PIPELINES: FULL             =====
# ====          (EXTRACT, PROCESS AND INDEX)           =====
# ==========================================================

@shared_task(track_started=True)
def populate_pipeline(
    by_resource = None,
    by_title = None,
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    type = [],
    collected_min=None, 
    collected_max=None,
    token = None
):
    """ Populated pipeline wrapper.
    It initializes the pipeline to populate the seed items that fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the populate endpoint.
    
    Parameters
    ----------
    token: str
        UUID.hex 

    by_resource: str, default=None
        Check if the seed link contains the given resource.

    by_title: str, default=None
        Check if the seed title contains the given title.
        
    tags: List[str], default=[]
        To filter the seeds according to this list of tags.
    
    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').
  
    lang: List[str], default=[]
        Language codes in ISO 639-1

    collected_min: datetime.datetime, default=None
        Start of the datetime interval

    collected_max: datetime.datetime, default=None
        End of the datetime interval

    Returns
    ----------
    items_count: int
        Number of items to be processed.
    """

    # Get items to process
    queryset = (
        SeedItem.objects.filter(
            create_advance_filter(
                and_filter_fields={
                    "link__icontains": by_resource,
                    "title__icontains": by_title,
                    "collected_at__gte": collected_min,
                    "collected_at__lte": collected_max,
                    "type__in": type,
                    "lang__in": lang,
                    "is_valid": True,
                    "is_seeded": False,
                }, 
                list_filter_fields={
                    "tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}
                }
            )
        )
        #.distinct("pk") Ignoring, there is no risk of duplicates with the current filters.
        .order_by("pk") # Order to allow Queryset slicing
        .values_list("pk", flat=True) # Only the primary keys are sent to the pipeline
    )

    items_count = run_generic_pipeline(
        main_task_func=populate_seeds,
        queryset=queryset,
        load_balancer_func=load_balancer_split_by_auth,
        callback_task_func=items_to_index,
        extra_args={'token': token},
    )
    
    return items_count

@shared_task(track_started=True)
def scan_pipeline(
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    is_channel = None,
    lastup_min = None,
    lastup_max = None,
    max_msgs = 2500,
    update_users = False,
    token = None
):
    """ Scan pipeline wrapper.
    It initializes the pipeline to scan the room items that fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the scan endpoint.

    Parameters
    ----------
    token: str
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names to be scanned. By default, the list will be 
        empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    lang: List[str], default=[]
        Language codes in ISO 639-1

    is_channel: Boolean, default=None
        To filter only Channels (True), Groups (False) or any (None).

    lastup_min: datetime.datetime, default=None
        Interval start date for the last_update field

    lastup_max: datetime.datetime, default=None
        Interval start date for the last_update field

    max_msgs: int, default=2500
        Max number of messages they will be extracted per room.

    update_users: Boolean, default=False
        To allow already existing users to be updated.

    Returns
    ----------
    items_count: int
        Number of items to be processed.
    """

    # Get items to process
    queryset = (
        RoomItem.objects.filter(
            create_advance_filter(
                and_filter_fields={
                    "lang__in": lang,
                    "is_channel": is_channel,
                    # Last update range filter
                    "last_update__gte": lastup_min,
                    "last_update__lte": lastup_max,
                    # Only allowed Rooms
                    "is_valid": True,
                    "is_private": False,
                    "is_deleted": False,
                    "following": True
                }, 
                list_filter_fields={
                    "unique_name__iexact": {"values": [r.strip() for r in room], "OR": True},
                    "tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}
                }
            )
        )
        .order_by("pk") # Order to allow Queryset slicing
    )

    items_count = run_generic_pipeline(
        main_task_func=scan_rooms,
        queryset=queryset,
        load_balancer_func=load_balancer_group_pks_by_auth,
        tasks_signatures_to_chain=[
            postprocess_msgs.s(), 
            ner_db_store.s(MessageItem.__name__, MessageItem._meta.app_label)
        ],
        callback_task_func=items_to_index,
        extra_args={'token': token, 'max_msgs': max_msgs, 'update_existing_users': update_users},
    )
    return items_count


def scan_comments_pipeline(
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    langs = [],
    types = [],
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    max_msgs = 2500,
    total_msgs = 1250000,
    token = None
):
    """ Scan comments pipeline wrapper.
    It initializes the pipeline to scan comments from the messages that fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the scan comments endpoint.

    Parameters
    ----------
    token: str
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names to be scanned. By default, the list will be 
        empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    langs: List[str], default=[]
        Language codes in ISO 639-1

    types: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    createdat_min: datetime.datetime, default=None
        Interval start date for the created_at field.

    createdat_max: datetime.datetime, default=None
        Interval start date for the created_at field.

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field.

    max_msgs: int, default=2500
        Max number of comments they will be extracted per message.

    total_msgs: int, default=1250000
        Total number of messages to be extracted globally. In order to avoid an 
        out-of-control usage of resources

    Returns
    ----------
    items_count: int
        number of rooms whose messages, which meet the filters, will be processed.
    """
    # Get items to process
    queryset = (
        RoomItem.objects.filter(
            create_advance_filter(
                and_filter_fields={
                    # Only allowed Rooms
                    "is_channel": True,
                    "is_valid": True,
                    "is_private": False,
                    "is_deleted": False,
                    "following": True
                }, 
                list_filter_fields={
                    "unique_name__iexact": {"values": [r.strip() for r in room], "OR": True},
                    "tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}
                }
            )
        )
        .order_by("pk") # Order to allow Queryset slicing
    )

    # Prepare message filters
    message_filters = {
        "and_filter_fields": {
            "lang__in": langs,
            "media_type__in": types,
            "created_at__gte": createdat_min,
            "created_at__lte": createdat_max,
            "stored_date__gte": stored_since,
            "is_valid": True,
            "is_reply": False, # Only main channel messages not comments
        },
    }

    items_count = run_generic_pipeline(
        main_task_func=scan_comments,
        queryset=queryset,
        load_balancer_func=load_balancer_group_pks_by_auth,
        tasks_signatures_to_chain=[
            postprocess_msgs.s(), 
            ner_db_store.s(MessageItem.__name__, MessageItem._meta.app_label)
        ],
        callback_task_func=items_to_index,
        extra_args={
            'token': token, 
            'message_filters': message_filters, 
            'max_msgs': max_msgs,
            'total_msgs': total_msgs
        },
    )
    return items_count


@shared_task(track_started=True)
def access_room_pipeline(
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    is_channel = None,
    lastup_min = None,
    lastup_max = None,
    token = None
):
    """ Scan pipeline wrapper.
    It initializes the pipeline to scan the room items that fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the scan endpoint.

    Parameters
    ----------
    token: str
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names to be scanned. By default, the list will be 
        empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    lang: List[str], default=[]
        Language codes in ISO 639-1

    is_channel: Boolean, default=None
        To filter only Channels (True), Groups (False) or any (None).

    lastup_min: datetime.datetime, default=None
        Interval start date for the last_update field

    lastup_max: datetime.datetime, default=None
        Interval start date for the last_update field

    Returns
    ----------
    items_count: int
        Number of items to be processed.
    """
    # Get items to process
    queryset = (
        RoomItem.objects.filter(
            create_advance_filter(
                and_filter_fields={
                    "lang__in": lang,
                    "is_channel": is_channel,
                    # Last update range filter
                    "last_update__gte": lastup_min,
                    "last_update__lte": lastup_max,
                    # Only allowed Rooms
                    "is_valid": False,
                    "following": True,
                    "is_deleted": False
                }, 
                list_filter_fields={
                    "unique_name__iexact": {"values": [r.strip() for r in room], "OR": True},
                    "tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}
                }
            )
        )
        .order_by("pk") # Order to allow Queryset slicing
        .values_list("pk", flat=True) # Only the primary keys are sent to the pipeline
    )

    items_count = run_generic_pipeline(
        main_task_func=access_entities,
        queryset=queryset,
        load_balancer_func=load_balancer_split_by_auth,
        extra_args={'model_class_name': RoomItem.__name__, 'token': token},
    )
    
    return items_count
