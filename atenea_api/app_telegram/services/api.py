from django.conf import settings
from app_telegram.services.pipelines import (
    index_msgs_pipeline,
    populate_pipeline,
    scan_pipeline, 
    scan_comments_pipeline,
    access_room_pipeline,
)
from app_telegram.serializers import ANY as TAG_ANY
from app_telegram.models import RoomItem, SeedItem, TelegramAuth, MessageItem
from app_base.api import create_advance_filter, bulk_add_query_relationships
from app_entity.services.pipelines import ner_extraction_msgs_pipeline
from app_metadata.services.pipelines import (
    sentiment_classification_msg_pipeline,
    embeddings_msgs_pipeline
)
from app_metadata.services.embeddings import run_categorizer
from app_metadata.services.api import embed_search
import logging


# Get an instance of a logger
logger = logging.getLogger(__name__)


# ==================================================================
#                 TelegramAuth endpoints handlers
# ==================================================================

def tgauth_search(name=None):
    """Search for TelegramAuth items.

    Parameters
    ----------
    name: str, default=None
        Filter TelegramAuth items by the name.

    Returns
    -------
    queryset: QuerySet[TelegramAuth]
        A queryset of TelegramAuth items.
    """
    return TelegramAuth.objects.filter(name__icontains=name) if name else TelegramAuth.objects.all()


# ==================================================================
#                   Seed endpoints handlers
# ==================================================================

def seed_bulk_create(token, seeds):
    """Bulk create a list of dictionary Seed items.

    This function serves the endpoint `/seed/bulk`
    
    NOTE: This items have been already validated and cleaned by its Serializer 
    so it's not necessary to re-check them. 

    Parameters
    ----------
    token: str
        UUID.hex 

    seed: List[dict]
        List of SeedItem dictionaries, the SeedIngestSerializer has already 
        cleaned up each link an checked duplicates.

    Returns
    -------
    affected_items: List[SeedItem]
        List of SeedItem items created.
    """
    # Bulk create
    affected_items = SeedItem.objects.bulk_create(
        [SeedItem(**seed) for seed in seeds],
        batch_size=settings.BULK_BATCH_SIZE,
        ignore_conflicts=True
    )
    # Bind the Query to all the items involved
    if token:
        bulk_add_query_relationships(affected_items, token)
    return affected_items

def seed_list(
    token,
    by_resource = None,
    by_title = None,
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    type = [],
    is_valid = None,
    is_seeded = None,
    collected_min=None, 
    collected_max=None, 
):
    """ Retrieve a list of seed items.

    Parameters
    ----------
    token: string
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

    type: List[str], default=[]
        Filter seeds according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    is_valid: Boolean, default=None
        Filter only valid or invalid seeds. Default: None.

    is_seeded: Boolean, default=None
        Filter only seeded or unseeded seeds. Default: None.

    collected_min: datetime.datetime, default=None
        Interval start date for the collected_at field

    collected_max: datetime.datetime, default=None
        Interval start date for the collected_at field

    Returns
    ----------
    seeds_qs: QuerySet[SeedItem]
        A queryset of seed items.
    """
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
                    "is_valid": is_valid,
                    "is_seeded": is_seeded,
                }, 
                list_filter_fields={
                    "tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}
                }
            )
        )
        .order_by("pk") 
    )
    if token:
        bulk_add_query_relationships(queryset, token)
    return queryset

def seed_populate(
    token, 
    by_resource = None,
    by_title = None,
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    type = [],
    collected_min=None, 
    collected_max=None, 
):
    """ Populated the seed items tht fits the filter and split the workload into 
    Celery tasks.

    This function serves the endpoint '/seed/populate'

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

    # Call the populate wrapper function
    # Start the pipeline by getting all the seeds to be populated and populating them
    total_items = populate_pipeline(
        by_resource=by_resource,
        by_title=by_title,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        type=type,
        collected_min=collected_min,
        collected_max=collected_max,
        token=token
    )

    return total_items


# ==================================================================
#                  Room endpoints handlers
# ==================================================================

def room_scanning(
    token = None, 
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    is_channel = None,
    lastup_min = None,
    lastup_max = None,
    max_msgs = 2500,
    update_users = False,
):
    """ Scans any room that fits the filter and split the workload into Celery 
    tasks.
    
    This function serves the endpoint '/room/scan'

    Parameters
    ----------
    token: string, default=None
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
        Max number of messages they will be extracted for each room. 

    update_users: Boolean, default=False
        To allow already existing users to be updated.

    Returns
    ----------
    items_count: int
        Number of rooms to be processed.
    """

    # Call the scan wrapper function
    # Start the pipeline by getting all the rooms to be scanned and scanning them
    total_items = scan_pipeline(
        room=room,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        is_channel=is_channel,
        lastup_min=lastup_min,
        lastup_max=lastup_max,
        max_msgs=max_msgs,
        update_users=update_users,
        token=token
    )

    return total_items

def room_access(
    token = None, 
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [],
    is_channel = None,
    lastup_min = None,
    lastup_max = None,
):
    """ Recalcule the access of any room with `is_valid=False` that fits the filter 
    and split the workload into Celery tasks.
    
    This function serves the endpoint '/room/access'

    Parameters
    ----------
    token: string, default=None
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
        Number of rooms to be processed.
    """

    # Call the access wrapper function
    # Start the pipeline by getting all the rooms to recalculate the access_hash
    total_items = access_room_pipeline(
        room=room,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        is_channel=is_channel,
        lastup_min=lastup_min,
        lastup_max=lastup_max,
        token=token
    )

    return total_items


# ==================================================================
#                    Message endpoints handlers 
# ==================================================================

def msg_scanning_comments(
    token = None, 
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    type = [],
    lang = [],
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    max_msgs = 2500,
    total_msgs = 1250000,
):
    """ Scans any room that fits the filter and split the workload into Celery 
    tasks.
    
    This function serves the endpoint '/room/scan'

    Parameters
    ----------
    token: string, default=None
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names to be scanned. By default, the list will be 
        empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    lang: List[str], default=[]
        Message language codes in ISO 639-1    

    created_at_min: datetime.datetime, default=None
        Interval start date for the created_at field

    created_at_max: datetime.datetime, default=None
        Interval start date for the created_at field

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field
        
    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    max_msgs: int, default=2500
        Max number of comments they will be extracted for each msg. 

    total_msgs: int, default=1250000
        Total number of messages to be extracted globally. In order to avoid an 
        out-of-control usage of resources

    Returns
    ----------
    items_count: int
        Number of rooms whose messages, which meet the filters, will be processed.

    """

    # Call the scan wrapper function
    # Start the pipeline by getting all the rooms to be scanned and scanning them
    total_items = scan_comments_pipeline(
        room=room,
        tags=tags,
        tag_match=tag_match,
        langs=lang,
        types=type,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        max_msgs=max_msgs,
        total_msgs = total_msgs,
        token=token
    )

    return total_items

def msg_ner(
    token, 
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 100000
):
    # Use wrapper
    ner_extraction_msgs_pipeline(
        room=room,
        is_reply=is_reply,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        lang=lang,
        type=type,
        block_size=block_size,
        token=token
    )

def msg_sentiment(
    token, 
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [], 
    type = [],
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    block_size = 500000
):
    """ Scans any room that fits the filter and split the workload into Celery 
    tasks.
    
    This function serves the endpoint '/room/scan'

    Parameters
    ----------
    token: string, default=None
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names to be scanned. By default, the list will be 
        empty and this filter will be ignored.

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

    lang: List[str], default=[]
        Language codes in ISO 639-1.

    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    lang: List[str], default=[]
        Message language codes in ISO 639-1    

    is_reply: bool, default=None
        Filter messages that are replies to another message.

    created_at_min: datetime.datetime, default=None
        Interval start date for the created_at field

    created_at_max: datetime.datetime, default=None
        Interval end date for the created_at field

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field

    block_size: int, default=100000
        Number of items to be read by the database at once.
    """
    sentiment_classification_msg_pipeline(
        room=room,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        type=type,
        is_reply=is_reply,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        block_size=block_size,
        token=token
    )

def msg_index(
    token,
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 500000
):
    """ Indexes in the search database any message that matches the filter using 
    a Celery task. 
    
    Parameters
    ----------
    token: string, default=None
        UUID.hex 

    room: List[str], default=[]
        List of channel/group names from which messages are to be extracted. By 
        default, the list will be empty and this filter will be ignored.

    created_at_min: datetime.datetime, default=None
        Interval start date for the created_at field

    created_at_max: datetime.datetime, default=None
        Interval end date for the created_at field

    lang: List[str], default=[]
        Language codes in ISO 639-1

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field
        
    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    block_size: int, default=500000
        The size of each block of items to be processed in memory from the DB 
        at a time.
    """
    index_msgs_pipeline(
        room=room,
        is_reply=is_reply,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        lang=lang,
        type=type,
        block_size=block_size,
        token=token
    )

def msg_embed(
    token, 
    instruct = "",
    slot="default",
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    refresh = False,
    block_size = 100000
):
    
    # Use wrapper
    embeddings_msgs_pipeline(
        instruct=instruct,
        slot=slot,
        room=room,
        is_reply=is_reply,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        lang=lang,
        type=type,
        refresh=refresh,
        block_size=block_size,
        token=token
    )

def _func_msg(
    token, 
    task_func,
    embed_field_name,
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 500000     
):
    and_filter_fields = {
        "created_at__gte": createdat_min,
        "created_at__lte": createdat_max,
        "stored_date__gte": stored_since,
        "media_type__in": type,
        "lang__in": lang,
        "is_reply": is_reply,
        f"{embed_field_name}__isnull": False
    }

    # Calculate (or re-calculate) embeddings for all the messages
    task_func.delay(
        token=token, 
        model_class_name=MessageItem.__name__,
        model_class_app_label=MessageItem._meta.app_label,
        model_class_embeddings_field_name=embed_field_name,
        and_filter_fields=and_filter_fields,
        list_filter_fields={
            "room__unique_name__iexact": {"values": [r.strip() for r in room], "OR": True}
        } if room else {},
        apply_distinct=False, # With the current filters there is no risk of duplicates.
        block_size=block_size
    )

def msg_categorize(
    token, 
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 500000
):
    
    _func_msg(
        token=token,
        task_func=run_categorizer,
        embed_field_name="cat_embeddings",
        room=room,
        is_reply=is_reply,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        lang=lang,
        type=type,
        block_size=block_size
    )

def msg_search_embeds(token, q, instruct="", empty=False):
    return embed_search(
        token, 
        q, 
        source_model_class="MessageItem",
        instruct=instruct, 
        empty=empty
    )

def _filter_messages(qs, createdat_min=None, createdat_max=None, stored_since=None, room=None, is_reply=None):
    """Helper function to apply common filters to a message queryset."""
    if createdat_min:
        qs = qs.filter(created_at__gte=createdat_min)
    if createdat_max:
        qs = qs.filter(created_at__lte=createdat_max)
    if stored_since:
        qs = qs.filter(stored_date__gte=stored_since)
    
    if room:
        # Construye una expresión regular: (sala1|sala2|sala3)
        regex = r'(' + '|'.join(room) + r')'
        # Busca las PKs usando la expresión regular case-insensitive
        room_pks = RoomItem.objects.filter(unique_name__iregex=regex).values_list('pk', flat=True)
        
        # Filtra los mensajes por esas PKs
        qs = qs.filter(room_id__in=room_pks)

    if is_reply is not None:
        qs = qs.filter(is_reply=is_reply)
        
    return qs

def get_messages_embeddings(
    createdat_min=None, 
    createdat_max=None, 
    stored_since=None, 
    room=None,
    is_reply=None
):
    """Retrieves a filtered list of message embeddings.

    Parameters
    ----------
    createdat_min: datetime.date, optional
        Interval start date for the `created_at` field.
    createdat_max: datetime.date, optional
        Interval end date for the `created_at` field.
    stored_since: datetime.date, optional
        Interval start date for the `stored_date` field.
    room: List[str], optional
        List of room unique names to filter by.
    is_reply: bool, optional
        Filter messages that are replies.

    Returns
    -------
    QuerySet[MessageItem]
        A queryset of MessageItem objects with embeddings.
    """
    qs = MessageItem.objects.filter(embeddings__isnull=False).select_related('embeddings', 'room')
    qs = _filter_messages(qs, createdat_min, createdat_max, stored_since, room, is_reply)
    return qs.order_by('created_at', 'id')

def get_messages(
    createdat_min=None, 
    createdat_max=None, 
    stored_since=None, 
    room=None,
    is_reply=None
):
    """Retrieves a filtered list of messages.

    Parameters
    ----------
    createdat_min: datetime.date, optional
        Interval start date for the `created_at` field.
    createdat_max: datetime.date, optional
        Interval end date for the `created_at` field.
    stored_since: datetime.date, optional
        Interval start date for the `stored_date` field.
    room: List[str], optional
        List of room unique names to filter by.
    is_reply: bool, optional
        Filter messages that are replies.

    Returns
    -------
    QuerySet[MessageItem]
        A queryset of MessageItem objects.
    """
    qs = MessageItem.objects.select_related('room')
    qs = _filter_messages(qs, createdat_min, createdat_max, stored_since, room, is_reply)
    return qs.order_by('created_at', 'id')
