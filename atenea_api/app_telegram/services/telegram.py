import re
import uuid
import time
import logging
import asyncio
#import traceback
from itertools import chain as iterchain
from collections import ChainMap
from django.apps import apps
from django.conf import settings
from django.utils import timezone
from django.db.models import Prefetch
from django.db import transaction, IntegrityError
from app_base.tagger import extract_tags
from app_base.utils import (
    convert_to_standard_text, 
    markdown_to_text, 
    detect_lang,
    AsyncTimedIterable
)
from app_base.documents import update_index
from app_base.api import (
    create_advance_filter,
    bulk_add_query_relationships, 
    bulk_add_generic_relationships,
)
from app_telegram.models import (
    SeedItem, 
    TelegramAuth, 
    BaseTelegramEntity,
    RoomItem, 
    UserItem, 
    MessageItem
)
from app_telegram.documents import RoomDocument, MessageDocument
from app_entity.services.ner import ner_extraction
from celery.app import shared_task
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import (
    User, 
    Channel, 
    InputPeerChannel,
    Chat, 
    ChannelParticipantAdmin, 
    ChannelParticipantCreator, 
    MessageMediaWebPage
)
from telethon.errors import (
    UsernameInvalidError,
    AuthKeyError, 
    FloodWaitError, 
    MsgIdInvalidError,
    MsgTooOldError,
    ChannelPrivateError,
    BadRequestError,
    RPCError, # Base exception from Telethon
)


# Get an instance of a logger
logger = logging.getLogger(__name__)


# SLEEP GLOBAL VARIABLES

# Sleep time after a RCPError, ValueError, etc.. (Anything except FloodWaitError)
# f.e: Telethon doesn't handle sleeps with ValueError: No user has "XXXXX" as username
SLEEP_AFTER_EXCEPTION = 5 

# Sleep time after a number of requests to the Telegram API to avoid FloodWaitError
SLEEP_AFTER_X_REQUESTS = 10 

# MAX WAIT TIME TO THE TELEGRAM API (in seconds)
MAX_WAIT_TIME = 300 # 5 mins


async def sleep_on_exception(e, seconds=SLEEP_AFTER_EXCEPTION):
    """ Sleeps for a number of seconds after an exception.
    
    Parameters
    ----------
    e: Exception
        Exception to be handled.    

    seconds: int, default=SLEEP_AFTER_EXCEPTION
        Number of seconds to sleep.
    """
    logger.debug(f"{e.__class__.__name__}: Sleeping after exception for {seconds}secs.")
    await asyncio.sleep(seconds)


# ======================================================
# =====            TELEGRAM CELERY TASKS           =====
# ======================================================
# =====  Celery tasks that have specific functio-  =====
# =====   -nality to extract data from Telegram,   ===== 
# =====           process and index it.            =====
# ======================================================


# ======================================================
# =====      CELERY TASKS: INDEXATION MANAGER      =====
# ======================================================

@shared_task(track_started=True, ignore_result=False)
def items_to_index(
    query_filter_by_model, 
    apply_distinct = False, 
    block_size=500000,
    es_chunk_size=4500, 
    thread_count=10,
    token=None,
):
    """Receive a list of filters to extract RoomItem/MessageItem items and index 
    them in the elasticsearch cluster.

    Parameters
    ----------
    query_filter_by_model: list[dict] or dict 
        A list of dictionaries (one from each celery task, in case of only one
        celery tasks, the argument will be a dict) with all filters from each 
        Model to update/create their items in their respective index. 
        Dictionary structure:
        ```
        {
            "RoomItem" : {
                "and_filter_fields": {"pk__in": [UUID(...), ...], ...}
                "list_filter_fields": {}
            },
            "MessageItem" : {
                "and_filter_fields": {"pk__in": [UUID(...), ...], ...}
                "list_filter_fields": {}
            },
        }
        ```

        If the keys "RoomItem" or "MessageItem" appear in the dictionary but empty
        or their sub-dicts "and_filter_fields" and "list_filter_fields" are empty, it 
        indicates that you want to index all documents without any filters. 
        e.g: query_filter_by_model example for index all messages: 
        ```
        }       
            "MessageItem" : {},
        }
        ```   
        or       
        ``` 
        }       
            "MessageItem" : {
                "and_filter_fields": {}
                "list_filter_fields": {}
            },
        }
        ```

        On the other hand, if the keys "RoomItem" or "MessageItem" do not appear, 
        it implies that you do not want to index that model.

    apply_distinct: boolean, default=False
        In case the filters inside `and_filter_fields` or `list_filter_fields` contain 
        related fields that may cause duplicates. This often happens with "related 
        fields" with a `Many` relationship. For example, 
        `MessageItem.objects.filter(entities__entity__name__icontains="Pedro")` 
        returns duplicates (entities is a Many-to-Many relationship field). On the 
        other hand, `MessageItem.objects.filter(room__tags__icontains="politic")` 
        is safe, as it is a One-to-Many relationship accessed through a One relationship 
        (each message has only one room).
        NOTE: `.distinct("pk")` may significantly impact performance.
        
    token: string, default=None
        UUID.hex 

    block_size: int
        The size of each block of items to be processed in memory from the DB 
        at a time.

    es_chunk_size: int
        The size of each chunk of items to be indexed by Elasticsearch.
        It can lead to a memory error caused by exceeding the configured heap 
        size in our Elasticsearch cluster. e.g:
        ```
            ApiError(429, 'circuit_breaking_exception', 'Data too large, data for 
            [<http_request>] would be ... which is larger than the limit of ... 
        ```
        In this case, reduce the concurrent requests to the cluster and/or the 
        number of items sent.

    thread_count: int
        The number of threads to use for parallel indexing of the chunks on the 
        Elasticsearch cluster.
    """

    def _join_filter_dicts(list_of_dicts):
        """
        Joins a list of dictionaries containing nested filter fields into a single dictionary.

        Parameters:
        -----------
        list_of_dicts : list of dict
            A list of dictionaries to be joined. Each dictionary should have the following structure:
            {
                "and_filter_fields": {"pk__in": [UUID(...), ...], ...},
                "list_filter_fields": {"data__iexact": [UUID(...), ...], ...}
            }

        Returns:
        --------
        dict
            A single dictionary with combined 'and_filter_fields' and 'list_filter_fields'.
        """
        def merge_dicts(d1, d2):
            for key, value in d2.items():
                if isinstance(value, list):
                    if key in d1:
                        d1[key].extend(value)
                    else:
                        d1[key] = value.copy()
                else:
                    d1[key] = value
            return d1

        # Collapsed dict for filter using the app_base.api.create_advance_filter function
        collapsed_dict = {
            "and_filter_fields": {},
            "list_filter_fields": {}
        }
        
        for d in list_of_dicts:
            for key in ["and_filter_fields", "list_filter_fields"]:
                collapsed_dict[key] = merge_dicts(collapsed_dict[key], d.get(key, {}))
        return collapsed_dict

    # Normalization: In case of one group taks, the arg will be a dict instead of a list
    if not isinstance(query_filter_by_model, list):
        query_filter_by_model = [query_filter_by_model]

    # prepare items from each celery task 
    rooms_dicts = []
    msgs_dicts = []
    for dict_by_task in query_filter_by_model:
        # Ignore empty dicts only allow dicts with RoomItem or MessageItem keys
        if RoomItem.__name__ in dict_by_task:
            rooms_dicts += [dict_by_task[RoomItem.__name__]]
        if MessageItem.__name__ in dict_by_task:
            msgs_dicts += [dict_by_task[MessageItem.__name__]] 
    
    room_filters = _join_filter_dicts(rooms_dicts) if rooms_dicts else None
    msgs_filters = _join_filter_dicts(msgs_dicts) if msgs_dicts else None
    
    try:
        if room_filters:   
            update_index(
                token,
                RoomDocument(), 
                **room_filters,
                apply_distinct=apply_distinct,
                block_size=block_size, 
                action="index",
                es_chunk_size=es_chunk_size, 
                thread_count=thread_count,
            )
        if msgs_filters:
            update_index(
                token,
                MessageDocument(), 
                **msgs_filters,
                apply_distinct=apply_distinct,
                block_size=block_size, 
                action="index",
                es_chunk_size=es_chunk_size, 
                thread_count=thread_count,
            )

    except Exception as e:
        #full_traceback = traceback.format_exc()
        logger.error(
            f"{e.__class__.__name__}: "
            f"An error has occurred during the elastic index update - {e}. "
            #f"Traceback:\n{full_traceback}"
        )


# ======================================================
# =====        CELERY TASKS: DATA MANAGERS         =====
# =====       EXTRACTION FROM TELEGRAM'S API       =====     
# ======================================================

@shared_task(track_started=True, ignore_result=True)
def access_entities(items_pk, auth_key, model_class_name, token = None):
    """ Extracts only the access_hash from each item using Telegrams'API and its name.

    This pipeline is used to restore access to items that have been orphaned, meaning 
    their access credentials have either been deleted or have entered hibernation.
    Parameters
    ----------
    items_pk: list[uuid.UUID]
        List of primary keys from BaseTelegramEntity items.
        NOTE: This rows will be blocked (inside a transaction) during the process.

    auth_key: dict
        Dict with the credentials info to login into Telegram API. Required Data:
        {
            pk: UUID(...)
            name: '...'
            api_id: 123XXXX
            api_hash: "..."
            session: "....."
        }
        
    model_class_name: str
        Contains the class name of the Django Model (must inherit from BaseTelegramEntity)
        of the `items_pk` parameter.

    token: string, default=None
        Query token to add a relation with the extracted items (Users/Messages)

    Returns
    ----------
    pks_by_model: dict
        A dictionary with all primary keys from each Model to update/create their 
        items in their respective index. Dictionary structure:
        {
            'ROOMS' : [UUID(...), ...], (optional)
            'MESSAGES' : [UUID(...), ...], (optional)
        }
    """

    async def _run_extract_tasks(items, auth_key, batch_size=6):
        """ To launch a task by each seed
        
        Parameters
        ----------
        items: List[BaseTelegramEntity]
            A list of Telegram entities to be extracted (users, channels, etc.)
        
        auth_key: dict
            Dictionary (related to a TelegramAuth item) with the credentials to 
            init a Telegram client. It contains the following data: "pk", "session", 
            "api_id" and "api_hash".

        batch_size: int, default=4
            Max number of requests to be made asynchronously over the Telegram API. 
            It is advisable not to exceed this number to avoid early rate limits. 

        Returns
        ----------
        items_to_update: list[BaseTelegramEntity]
            List of BaseTelegramEntity items to be updated.

        disable_count: int
            Number of items that have been marked as deleted (in telegram).
            `is_deleted` = True
        """

        items_to_update = []
        disable_count = 0
        try:
            async with TelegramClient(
                StringSession(auth_key.get("session", "")), 
                auth_key.get("api_id", 0), 
                auth_key.get("api_hash", "")
            ) as client:
                
                await client.start()
                
                flood_event = asyncio.Event()
                session_expired_event = asyncio.Event()
                wait_seconds = 0 # Ini floodWait to 0
                auth_exception_log = None # Which exception has been raised if session_expired_event is set
                sleep_counter = 0 # To avoid FloodWaitError
                # Max number of requests at a time = batch_size
                for i in range(0, len(items), batch_size):

                    # Sleep for 10secs (each 60 rooms) in order to avoid rate limits/bans
                    if sleep_counter == 60/batch_size:
                        logger.debug(
                            f"Anti-FloodWait: [ACCESS] '{auth_key.get('name', 'NOT FOUND!')}' sleeping for {SLEEP_AFTER_X_REQUESTS}s)"
                        )
                        sleep_counter = 0
                        await asyncio.sleep(SLEEP_AFTER_X_REQUESTS)

                    # each tuple contains: (updated item, required_sleep, exception)
                    tuples = await asyncio.gather(
                        *[_extract_access_hash(client, item, auth_key.get('pk'), flood_event, session_expired_event) 
                          for item in items[i:i+batch_size]
                        ]
                    )
                    # Post-process each tuple
                    for item, seconds, exception in tuples:
                        if item:
                            items_to_update.append(item)
                            if item.is_deleted:
                                disable_count += 1
                        if not wait_seconds and seconds:
                            wait_seconds = seconds
                        if not auth_exception_log and exception:
                            auth_exception_log = exception

                    # Finally check if FloodError or AuthKeyError occurred, then, 
                    # set wait_seconds/raise and break out of the loop
                    if session_expired_event.is_set():
                        # An error ocurred with the session, this event allow to 
                        # process all the extracted entities obtained before the error
                        # without losing any information
                        raise auth_exception_log
                    if flood_event.is_set():
                        # Wait time to the Telegram Api Key
                        wait_until = timezone.now() + timezone.timedelta(seconds=wait_seconds)
                        # NOTE: Use 'aupdate' version inside async function
                        await TelegramAuth.objects.filter(
                            pk=auth_key.get('pk')
                        ).aupdate(wait_until=wait_until)
                        logger.info(
                            f"FloodWaitError: [ACCESS] Stopping... Auth '{auth_key.get('name')}' have to sleep until '{wait_until}'."
                        )
                        break
                    

                    sleep_counter += 1
         
        except (ValueError, RuntimeError, AuthKeyError, EOFError) as e:
            # Problem with this credential, set 'is_valid' to false
            # NOTE: Use 'aupdate' version inside async function
            await TelegramAuth.objects.filter(pk=auth_key.get('pk')).aupdate(is_valid=False)
            logger.error(
                f"{e.__class__.__name__ }: Error in auth '{auth_key.get('name', 'NOT FOUND!')}' "
                f"- {e}"
            )
        
        # Finnaly, return all the results that could be obtained
        return items_to_update, disable_count

    async def _extract_access_hash(
        client: TelegramClient, 
        item: BaseTelegramEntity, 
        tauth_pk: uuid.UUID, 
        flood_event: asyncio.Event,
        session_expired_event: asyncio.Event
    ):
        """ Extract all the information of the telegram item and returns the 
        appropriate Django model.

        Returns
        ----------
        item: BaseTelegramEntity
            The BaseTelegramEntity object to be updated.

        seconds: int
            The number of seconds the credential has to wait until the next request.
            Only in case of a FloodWaitError.
        
        exception: Exception
            The exception that has been raised. Only in case of a session error.
            AuthKeyError (the session expired or was broken)
        """
        seconds = 0
        try:
            input_peer_entity = await client.get_input_entity(item.unique_name)
            item.access_hash = input_peer_entity.access_hash
            item.access_auth_id = tauth_pk # Related TelegramAuth (to use the access_hash)                
            item.is_valid = True # Now the item is valid to scan
        except (ValueError, UsernameInvalidError) as e:
            # In case de room/user has disappeared (the account/group has been deleted or banned)
            item.is_deleted = True
            logger.info(
                f"{ e.__class__.__name__ }: In '{item.unique_name if item else 'NOT FOUND'}' - {e}"
            )
            await sleep_on_exception(e)
        except FloodWaitError as e:
            # In case of reached the rate limit, FloodWaitError is raised.
            flood_event.set()  # Flag to stop using the API
            seconds = e.seconds
            return None, seconds, None
        except AuthKeyError as e:
            # The session has expired or was broken during the extraction
            session_expired_event.set()
            return None, 0, e
        except RPCError as e:
            logger.error(
                f"{ e.__class__.__name__ }: Unexpected error while processing "
                f"'{item.unique_name if item else 'NOT FOUND'}' - {e}"
            )
            await sleep_on_exception(e)
            # Unexpected error, the item will be ignored. 
            return None, 0, None

        # In case of invalid name or unhandled entity, set is_valid=False
        return item, 0, None
    

    ModelClass = apps.get_model("app_telegram", model_class_name)

    # Start a transaction to avoid race conditions:
    # f.e: run this pipeline  more than once at the same time extracting the same
    # data (wasting time and requests).
    with transaction.atomic():
        items_to_access = list(
            ModelClass.objects.filter(pk__in=items_pk)
            .select_for_update(skip_locked=True)
        )

        # Extract access_hash asynchronously
        items_to_update, disable_count = asyncio.run(_run_extract_tasks(items_to_access, auth_key))

        # Finally, update the entities (with a new available access or disallowed)
        init_time = time.time()
        updated_items = ModelClass.objects.bulk_update(
            items_to_update, 
            fields=["access_hash", "access_auth", "is_valid", "is_deleted"], 
            batch_size=settings.BULK_BATCH_SIZE
        )
        logger.info(
            f"{updated_items} {model_class_name} items were updated, {disable_count} "
            f"of those were marked as deleted (in telegram) "
            f"(bulk_create_or_update: {(time.time() - init_time):.3f} secs)."
        )

        if items_to_update:
            return {
                ModelClass.__name__: {
                    "and_filter_fields": {"pk__in": [item.pk for item in items_to_update]}
                }
            }
        return {}

@shared_task(track_started=True, ignore_result=False)
def populate_seeds(seeds_pk, auth_key, token = None):
    """ Extract info of each seed item using Telegrams'API and its link.

    This function takes each "link" from `seeds` and requests all of its information 
    from Telegram using the `get_entity()` method. This is a resource-intensive API 
    call to Telegram, known as "ResolveUsername", which is limited to 200 calls 
    per day per credential.

    Additionally, an `access_hash` is obtained for each entity. This `access_hash`
    is linked to the `auth_key` credential, allowing for subsequent requests to 
    the Telegram API for additional information about the entity with significantly 
    fewer limitations (e.g., fetching messages from rooms).

    Once the seed info has been extracted from the API the collected_at field
    will turn to True.

    Parameters
    ----------
    seeds_pk: List[uuid.UUID]
        List of primary keys from SeedItem items to be populated.
        NOTE: This rows will be blocked (inside a transaction) during the process.

    auth_key: dict
        Dict with the credentials info to login into Telegram API. Required Data:
        ```
        {
            pk: UUID(...)
            name: '...'
            api_id: 123XXXX
            api_hash: "..."
            session: "....."
        }
        ``` 

    token: string
        Query token to add the relation with the item

    Returns
    ----------
    data_dict: dict
        A dictionary with all primary keys from each Model compatible with
        `app_telegram.services.telegram.items_to_index` to allow the connection 
        of these tasks in a Celery Pipeline.
    """

    async def _run_extract_tasks(seeds, auth_key, batch_size=6):
        """ To launch a task by each seed
        
        Parameters
        ----------
        seeds: List[SeedItem]
            A list of SeedItem items to be populated.
        
        auth_key: dict
            Dictionary (related to a TelegramAuth item) with the credentials to 
            init a Telegram client. It contains the following data: "pk", "session", 
            "api_id" and "api_hash".

        batch_size: int, default=6
            Max number of requests to be made asynchronously over the Telegram API. 
            It is advisable not to exceed this number to avoid early rate limits. 
        """
        result_dict = {}
        seeds_to_update = []
        try:
            async with TelegramClient(
                StringSession(auth_key.get("session", "")), 
                auth_key.get("api_id", 0), 
                auth_key.get("api_hash", "")
            ) as client:
                
                await client.start()
                
                flood_event = asyncio.Event()
                session_expired_event = asyncio.Event()
                wait_seconds = 0 # Ini floodWait to 0
                auth_exception_log = None # Which exception has been raised if session_expired_event is set
                sleep_counter = 0 # To avoid FloodWaitError
                # Max number of requests at a time = batch_size
                for i in range(0, len(seeds), batch_size):

                    # Sleep for 10secs (each 60 rooms) in order to avoid rate limits/bans
                    if sleep_counter == 60/batch_size:
                        logger.debug(
                            f"Anti-FloodWait: [POPULATE] '{auth_key.get('name', 'NOT FOUND!')}' sleeping for {SLEEP_AFTER_X_REQUESTS}s)"
                        )
                        sleep_counter = 0
                        await asyncio.sleep(SLEEP_AFTER_X_REQUESTS)

                    # each tuple contains: (Model Class, dict_item, updated seed, required_sleep, exception)
                    tuples = await asyncio.gather(
                        *[_extract_entity(client, seed, auth_key.get('pk'), flood_event, session_expired_event) 
                          for seed in seeds[i:i+batch_size]
                        ]
                    )
                    # Post-process each tuple
                    for cls, item, seed, seconds, exception in tuples:
                        if item:
                            result_dict.setdefault(cls, []).append(item)
                        if seed:
                            seeds_to_update.append(seed)
                        # Extract the highest number from all the tuples to detect a FloodWaitError 
                        # without ignoring other results. (seconds != 0 implies a FloodWaitError)
                        if not wait_seconds and seconds:
                            wait_seconds = seconds
                        if not auth_exception_log and exception:
                            auth_exception_log = exception

                    # Finally check if FloodError or AuthKeyError occurred, then, 
                    # set wait_seconds/raise and break out of the loop
                    if session_expired_event.is_set():
                        # An error ocurred with the session, this event allow to 
                        # process all the extracted entities obtained before the error
                        # without losing any information
                        raise auth_exception_log
                    if flood_event.is_set():
                        # Wait time to the Telegram Api Key
                        wait_until = timezone.now() + timezone.timedelta(seconds=wait_seconds)
                        # NOTE: Use 'aupdate' version inside async function
                        await TelegramAuth.objects.filter(
                            pk=auth_key.get('pk')
                        ).aupdate(wait_until=wait_until)
                        logger.info(
                            f"FloodWaitError: [POPULATE] Stopping... Auth '{auth_key.get('name')}' have to sleep until '{wait_until}'."
                        )
                        break
                    
                    sleep_counter += 1
                        
        except (ValueError, RuntimeError, AuthKeyError, EOFError) as e:
            # Problem with this credential, set 'is_valid' to false
            # NOTE: Use 'aupdate' version inside async function
            await TelegramAuth.objects.filter(pk=auth_key.get('pk')).aupdate(is_valid=False)
            logger.error(
                f"{e.__class__.__name__ }: Error in auth '{auth_key.get('name', 'NOT FOUND!')}' "
                f"- {e}"
            )
        
        # Finnaly, return all the results that could be obtained
        return result_dict, seeds_to_update

    async def _extract_entity(
        client: TelegramClient, 
        seed: SeedItem, 
        tauth_pk: uuid.UUID, 
        flood_event: asyncio.Event,
        session_expired_event: asyncio.Event
    ):
        """ Extract all the information of the telegram item and returns the 
        appropriate Django model.

        Returns
        ----------
        ModelClass: class
            The Django model class to be created/updated.

        item: dict
            The dictionary with the extracted information.
        
        seed: SeedItem
            The SeedItem object to be updated.

        seconds: int
            The number of seconds the credential has to wait until the next request.
            Only in case of a FloodWaitError.
        
        exception: Exception
            The exception that has been raised. Only in case of a session error.
            AuthKeyError (the session expired or was broken)
        """
        seconds = 0
        try:
            entity = await client.get_entity(seed.link)

            # First of all verify if the entity is valid: User or Channel
            # Avoid unhandled errors: UserEmpty, ChannelForbidden, ChatForbidden, ChatEmpty
            if not isinstance(entity, User) and not isinstance(entity, Channel):
                raise ValueError(f"Entity type not allowed: {entity.__class__.__name__}")

            item = {
                "tg_id": entity.id,
                "access_hash": entity.access_hash,
                "access_auth_id": tauth_pk, # Related TelegramAuth (to use the access_hash)
                "unique_name": entity.username,
                "tags": seed.tags,
                "lang": seed.lang,
                "seed_item_id": seed.pk # Related SeedItem
            }

            # Change seed flags
            seed.is_seeded = True

            if isinstance(entity, User):
                item["first_name"] = entity.first_name
                item["last_name"] = entity.last_name
                item["phone"] = entity.phone
                item["is_bot"] = entity.bot
                item["is_scam"] = entity.scam
                # Extract full information
                full_user = (await client(functions.users.GetFullUserRequest(entity))).full_user
                item["about"] = full_user.about
                return UserItem, item, seed, 0, None

            elif isinstance(entity, Channel) or isinstance(entity, Chat):
                item["link"] = seed.link
                item["title"] = entity.title
                item["created_at"] = entity.date
                # Check if channel or megagroup
                if isinstance(entity, Channel) and not entity.megagroup:
                    item["is_channel"] = True

                # Extract full information
                full_chat = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
                item["about"] = full_chat.about

                return RoomItem, item, seed, 0, None
            
            else:
                logger.info(
                    f"Seed '{seed.link if seed else 'NOT FOUND'}' contains an unhandled "
                    f"entity type '{type(entity)}'."
                )
                        
        except (ValueError, UsernameInvalidError) as e:
            logger.info(
                f"{ e.__class__.__name__ }: In '{seed.link if seed else 'NOT FOUND'}' - {e}"
            )
            await sleep_on_exception(e)
        except FloodWaitError as e:
            flood_event.set()  # Flag to stop using the API
            seconds = e.seconds
            # logger.info about FloodWaitError is already logged outside of this function
            # In case of a FloodWait, SeedItem will be ignored
            return None, {}, None, seconds, None
        except AuthKeyError as e:
            # The session has expired or was broken during the extraction
            session_expired_event.set()
            return None, {}, None, 0, e
        except RPCError as e:
            logger.error(
                f"{ e.__class__.__name__ }: Unexpected error while processing "
                f"'{seed.link if seed else 'NOT FOUND'}' - {e}"
            )
            await sleep_on_exception(e)
            # SeedItem will not be unvalidated, the exception is unexpected 
            # (may or may not be a problem with the seed)
            return None, {}, None, 0, None

        # In case of invalid name or unhandled entity, set is_valid=False
        seed.is_seeded = False
        seed.is_valid = False
        return None, {}, seed, 0, None


    # Start a transaction to avoid race conditions:
    # f.e: run this pipeline  more than once at the same time extracting the same
    # data (wasting time and requests).
    with transaction.atomic():
        seeds_to_populate = list(
            SeedItem.objects.filter(pk__in=seeds_pk)
            .select_for_update(skip_locked=True)
        )

        # Extract info from all the seeds asynchronously
        items_by_model, seeds_to_update = asyncio.run(_run_extract_tasks(seeds_to_populate, auth_key))

        # Bulk create/update for each type
        affected_items = []
        rooms_pk = []
        for model_class, items_dict in items_by_model.items():
            items, _ = model_class.bulk_create_or_update(
                items=items_dict,
                unique_key_name="tg_id",
                update_fields = items_dict[0].keys()
            )  
            affected_items += items 
            if model_class == RoomItem:
                rooms_pk += [item.pk for item in items]

        # Bind the Query to all the items involved. SeedItem are related with 
        # the Query in api.populate())
        if token:
            bulk_add_query_relationships(affected_items, token)

        # Finally, update the seeds ('is_seeded' flag to True or 'is_valid' to False)
        init_time = time.time()
        updated_items = SeedItem.objects.bulk_update(
            seeds_to_update, 
            fields=["is_seeded", "is_valid"], 
            batch_size=settings.BULK_BATCH_SIZE
        )
        logger.info(
            f"{updated_items} SeedItem items were updated "
            f"(bulk_create_or_update: {(time.time() - init_time):.3f} secs)."
        )

        if rooms_pk:
            return {
                RoomItem.__name__: {"and_filter_fields": {"pk__in": rooms_pk}}
                # Users are not indexed at the moment
            }
        return {}


####################################################
####          ITERATE OVER MESSAGES             ####
####################################################

async def extract_msgs(
    client: TelegramClient, 
    room: RoomItem, 
    max_msgs: int,
    related_msg: MessageItem = None,
):
    """ Extract new messages from `room` and update the room with the new offset 
    (if no problem occurred) or set the room as invalid (in case of a bad request).
    
    Parameters
    ----------
    client: TelegramClient
        TelegramClient object to use to extract the messages.

    room: RoomItem
        RoomItem item to extract the new messages from.
    
    max_msgs: int
        Max number of messages to extract.

    related_msg: MessageItem, default=None
        Iterate over the replies of the message, using its `reply_to_id` and `last_offset` 
        fields. If the parameter is None, the iteration will start from `room.last_offset`.

    Returns
    ----------
    msgs_list: list[MessageItem]
        List of new MessageItem items to be created.

    room_to_update: RoomItem
        RoomItem item to update with the new data (offset, last_update or is_valid).

    related_msg_to_update: MessageItem
        MessageItem item to update with the new data (offset, last_update or is_valid).
        If `related_msg` ìs not None, the new messages will not affect the `room`
        instead, the `related_msg`.  
    """
    def _choose_media_type(msg):
        if msg.media:
            if msg.web_preview or isinstance(msg.media, MessageMediaWebPage):
                return MessageItem.WEB_PAGE
            elif msg.photo:
                return MessageItem.PHOTO
            elif msg.video:
                return MessageItem.VIDEO
            elif msg.audio or msg.voice:
                return MessageItem.AUDIO
            elif msg.gif:
                return MessageItem.GIF
            elif msg.sticker:
                return MessageItem.OTHER
            elif msg.contact:
                return MessageItem.CONTACT
            elif msg.geo:
                return MessageItem.GEO
            elif msg.document:
                return MessageItem.DOC
            return MessageItem.OTHER
        return MessageItem.TEXT

    msg_list = []
    try:
        # Update the `last_scan_at` field
        room.last_scan_at = timezone.now()
        if related_msg:
            related_msg.last_scan_at = timezone.now()

        input_room = InputPeerChannel(room.tg_id, room.access_hash)
        last_offset = None
        # wait_for to avoid long waiting times (when Telegram API doesn't return the messages)
        async for msg in AsyncTimedIterable(
            iterable=client.iter_messages(
                entity=input_room, 
                reverse=True, # [1] -> First, [-1] -> Last
                limit=max_msgs, 
                reply_to=related_msg.msg_id if related_msg else None,
                offset_id= related_msg.last_offset if related_msg else room.last_offset,
                wait_time=0.250 #(sleep secs between chunks of GetHistoryRequest)
            ), 
            timeout=MAX_WAIT_TIME, 
            sentinel=TimeoutError
        ):
            
            # Check if the iteration has timed out and broken the loop
            if isinstance(msg, TimeoutError):
                logger.error(
                    f"{TimeoutError.__name__}: In '{room.link if room else 'NOT FOUND'}'"
                    f" - TimeoutError after {MAX_WAIT_TIME}s. {len(msg_list)} messages have been extracted."
                )
                await sleep_on_exception(msg)
                break

            # Ignore action message (generated by telegram app)
            if not msg.action:
                # Remove markdown notation
                clean_text = markdown_to_text(msg.text)
                # formatted text to standard ASCII
                clean_text = convert_to_standard_text(clean_text).strip()
                # Remove consecutive newlines
                clean_text = re.sub(r'\n+', '\n', clean_text)
                # Remove repeated characters like '--' or **
                clean_text = re.sub(r'([-_=+*]){2,}', '', clean_text)

                msg_list.append(  
                    MessageItem(
                        **{
                            'link': (
                                f"{room.link}/{msg.id}"
                                if not related_msg else
                                f"{room.link}/{related_msg.msg_id}?comment={msg.id}"
                            ),
                            'msg_id': msg.id,
                            'room_id': room.pk,
                            'text': msg.text,
                            # Only available views in a message from a broadcast channel has 
                            'views': msg.views,
                            # cleaned text (the text without markdown/strange symbols)
                            'annotated_text': clean_text,
                            # Remove emails, urls, @mentions and emojis for lang detection.
                            'lang': (
                                detect_lang(
                                    extract_tags(
                                        clean_text, 
                                        hashtag=False, 
                                        date=False, 
                                        time=False, 
                                        number=False,
                                    )[0] # Ignore list of matches
                                ) 
                                if clean_text else ""
                            ), 
                            'sender': msg.sender.id if msg.sender else None, 
                            'media_type': _choose_media_type(msg),
                            'is_reply': msg.is_reply,
                            'created_at': msg.date,
                            'reply_to_id': msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                            'reply_to_msg': related_msg if related_msg else None,
                        }   
                    )
                )   
                # Store the last offset
                last_offset = msg.id
    
    except ChannelPrivateError as e:
        logger.error(
            f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
        )
        # the Telegram auths works but the room is private and can't be accessed 
        # without an invite link
        room.is_private = True
        room.last_update = timezone.now()
        return [], room, None
    except (MsgIdInvalidError, MsgTooOldError) as e:
        # BadRequestError child exceptions related to the `related_msg` 
        # The message ID is invalid, reasons:
        # - the message has been deleted
        # - the message doen't have comments (more common)
        # - The message ID corresponds to one image in a multi-image post; only the first ID in the set contains the post's comments.
        logger.error(
            f"{ e.__class__.__name__ }: Message <{related_msg.msg_id if related_msg else None}> "
            f"in '{room.link}' - {e}"
        )
        related_msg.is_valid = False
        return [], None, related_msg
    except BadRequestError as e:
        logger.error(
            f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
        )
        # TelegramAuth item (auth_key) is invalid for this room's access_hash
        # or the room is invalid (removed or banned, this can be checked during
        #  `access_entities`)
        room.access_auth = None
        room.access_hash = None
        room.is_valid = False
        room.last_update = timezone.now()
        return [], room, None
    except FloodWaitError:
        # Re-raise FloodWaitException (will be handled outside of this function)
        # To avoid RPCError general exception to catch it
        raise 
    except AuthKeyError:
        # The session has expired or was broken during the extraction
        raise
    except (ValueError, RPCError) as e:
        logger.error(
            f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
        )
        await sleep_on_exception(e)

    # Only in case of new messages extracted. In case of timeout or RCPError, 
    # the function will return all message that have been already extracted until the exception.
    if msg_list:
        # update the `last_offest` with the last msg id
        if related_msg:
            related_msg.last_offset = last_offset
            # auto_now does not trigger on bulk_update(), whereupon, manually 
            related_msg.last_offset_update = timezone.now()
            return msg_list, None, related_msg
        else:
            room.last_offset = last_offset
            # auto_now does not trigger on bulk_update(), whereupon, manually 
            room.last_offset_update = timezone.now()
            room.last_update = timezone.now() 
            return msg_list, room, None
    return [], None, None


@shared_task(track_started=True, ignore_result=False)
def scan_rooms(rooms_pk, auth_key, max_msgs = 2500, update_existing_users = False, token = None):
    """ Extract info of each room: messages or users/user_bots (in groups)

    ----
    This function receives a list of primary keys of rooms instead of all
    the object's data because this date will be extracted here after a 
    'select_for_update' call inside a transaction, protecting the consistency 
    of data from possible race conditions.

    TODO: (Future) Extract videos and images

    ----

    Parameters
    ----------
    rooms_pk: list[uuid.UUID]
        List of primary keys from RoomItems.
        NOTE: This rows will be blocked (inside a transaction) during the process
        to avoid falling into possible race conditions.

    auth_key: dict
        Dict with the credentials info to login into Telegram API. Required Data:
        ```
        {
            pk: UUID(...)
            name: '...'
            api_id: 123XXXX
            api_hash: "..."
            session: "....."
        }
        ```

    max_msgs: int, default=2500
        Max number of messages they will be extracted for each room. 

    update_existing_users: bool, default=True
        Update already existing users from the extracted ones.

    token: string, default=None
        Query token to add a relation with the extracted items (Users/Messages)

    Returns
    ----------
    message_pks: List[uuid.UUID]
        List of primary keys of the new MessageItem items.
    """

    async def _run_extract_tasks(
        rooms, 
        auth_key,
        max_msgs = 2500, 
        update_existing_users = True,
        batch_size = 8
    ):
        """ To launch a task by each room and type of action
        
        NOTE: (Using access_hash is less common to have a FloodWaitError)
        
        Unlike populate pipeline, here the FloodWaitError/AuthKeyError is handled 
        outside the coroutines. This leads to a loss of information (e.g: with 6 
        coroutines, if the error occurs in the last one we will lose the data of
        all of them). 

        However, losing a couple of messages is not very expensive and allows to 
        have a simpler code than handling the error.

        Parameters
        ----------
        rooms: List[app_telegram.RoomItem]
            List of RoomItem items that are going to be scanned

        auth_key: dict
            Dictionary with the credentials to init a Telegram client. It contains
            the following data: "pk", "session", "api_id" and "api_hash".

        max_msgs: int, default=2500
            Max number of messages they will be extracted for each room. 

        update_existing_users: bool, default=True
            Update already existing users from the extracted ones.

        batch_size: int, default=8
            Max number of requests to be made asynchronously over the Telegram API. 
            It is advisable not to exceed this number to avoid early rate limits.

            Must be >= 2.

            NOTE: This function calls the Telgram API to get two types of information: 
            users and messages. So the bach_size will be split 
            (e.g. batch_size=6 => 3 asynchronous requests for users and 3 for messages).            

        Returns
        ----------
        msgs_list: list[list[MessageItem]]
            List of list with MessageItem elements, to insert create **new** messages 
            into the platform.
            NOTE: Some nested list can be empty '[]'

        rooms_to_update: list[dict]
            List of RoomItem items which last_offset and last_updated fields must be
            updated. Each dictionary
            contains the following data:

        users_dicts: list[list[dict]]
            List of list with dictionaries with the UserItem structure, to insert
            or update user items.
            NOTE: Some nested list can be empty '[]'. **MAY CONTAIN DUPLICATES**

        new_users_id_by_room_pk: list[dict]
            List of dictionaries to create new relationships between UserItems and
            RoomItems. Each dictionary contains the following data:
            {
                # RoomItem.pk: List of new users tg_id values to relate with the Room
                'RoomItem.pk': users_id 
            }
            NOTE: Some dicts can be empty '{}'
        """
        try:
            async with TelegramClient(
                StringSession(auth_key.get("session", "")), 
                auth_key.get("api_id", 0), 
                auth_key.get("api_hash", "")
            ) as client:
                
                await client.start()

                msgs_res = []
                members_res = []
                sleep_counter = 0 # To avoid FloodWaitError
                # Max number of requests at a time = batch_size (batch_size/2 for each call)
                batch_size_per_action = int(batch_size/2)
                for i in range(0, len(rooms), batch_size_per_action):

                    try:
                        # Extract new msgs, users, rooms with new offset and users_id by room to relate
                        aux_msgs_res, aux_members_res = await asyncio.gather(
                            asyncio.gather(
                                *[extract_msgs(client, room, max_msgs) 
                                for room in rooms[i:i+batch_size_per_action]
                                ]
                            ),
                            asyncio.gather(
                                *[_extract_members(client, room, auth_key.get('pk'), update_existing_users) 
                                for room in rooms[i:i+batch_size_per_action]
                                ]
                            )
                        )
                        msgs_res += [res[:-1] for res in aux_msgs_res] # Ignore `related_msg` (not used)
                        members_res += aux_members_res

                        sleep_counter += 1
                        # Sleep for 10secs (each 60 rooms) in order to avoid rate limits/bans
                        if sleep_counter == 60/batch_size_per_action:
                            logger.debug(
                                f"Anti-FloodWait: [SCAN] '{auth_key.get('name', 'NOT FOUND!')}' sleeping for {SLEEP_AFTER_X_REQUESTS}s)"
                            )
                            sleep_counter = 0
                            await asyncio.sleep(SLEEP_AFTER_X_REQUESTS)
                            
                    except FloodWaitError as e:
                        # Stop scanning...
                        # Handling FloodWaitError here is simple but some information 
                        # can be discarded (the other sibling coroutines that did not 
                        # trigger a rate limit, max 2 corutines info -> Fair enought)
                        if e.seconds <= 1800:
                            logger.info(
                                f"{ e.__class__.__name__ }: [SCAN] Less than 1800secs, sleeping {e.seconds:.3f}secs."
                            )
                            # If FloodWait is less than half and hour it will wait (instead of stop)
                            await asyncio.sleep(e.seconds)
                        else:
                            # Wait time to the Telegram Api Key
                            wait_until = timezone.now() + timezone.timedelta(seconds=e.seconds)
                            # NOTE: Use 'aupdate' version inside async function
                            await TelegramAuth.objects.filter(
                                pk=auth_key.get('pk')
                            ).aupdate(wait_until=wait_until)
                            logger.info(
                                f"{e.__class__.__name__}: [SCAN] Stopping... Auth '{auth_key.get('name')}' have to sleep until '{wait_until}'."
                            )
                            break
            
                # msgs_list rooms_to_update, users_dicts, new_users_id_by_room_pk
                return zip(*msgs_res), zip(*members_res)
        except (ValueError, RuntimeError, AuthKeyError, EOFError) as e:
            # Problem with this auth item, set 'is_valid' to false
            # NOTE: Use 'aupdate' version inside async function
            await TelegramAuth.objects.filter(pk=auth_key.get('pk')).aupdate(is_valid=False)
            logger.error(
                f"{e.__class__.__name__ }: Error in auth '{auth_key.get('name', 'NOT FOUND!')}' "
                f"- {e}"
            )
            return ([], []), ([], [])

    async def _extract_members(
        client:TelegramClient, 
        room: RoomItem, 
        tauth_pk: uuid.UUID, 
        update_existing_users=False
    ):
        # Participants can only be extracted in `channel` groups
        if not room.is_channel:
            users = []
            users_tgid = []
            try:
                input_room = InputPeerChannel(room.tg_id, room.access_hash)
                async for user in AsyncTimedIterable(
                    client.iter_participants(entity=input_room), 
                    timeout=MAX_WAIT_TIME,
                    sentinel=TimeoutError
                ):
                    # Check if the iteration has timed out and broken the loop
                    if isinstance(user, TimeoutError):
                        logger.error(
                            f"{TimeoutError.__name__}: In '{room.link if room else 'NOT FOUND'}'"
                            f" - TimeoutError after {MAX_WAIT_TIME}s. {len(users)} users have been extracted."
                        )
                        await sleep_on_exception(user)
                        break

                    # If the user already exist its going to be updated only if 
                    # update_existing_users=True (in case of some new info), but 
                    # it will not be re-related to the room.
                    flag = True
                    if user.id in [user.tg_id for user in room.members.all()]:
                        flag = update_existing_users
                    else:
                        users_tgid.append(user.id)
                    
                    if flag:
                        users.append({
                            'tg_id': user.id,
                            'unique_name': user.username,
                            'access_hash': user.access_hash,
                            # The access_hash must be related to the TelegramAuth item who created it
                            # NOTE: Duplicate? This info can also be obtained from the related room field
                            # SOL: There are users (from seeds) that don't have a room associated with them
                            # therefore, it is better to have all items configured in the same way.
                            'access_auth_id': tauth_pk, 
                            'first_name': user.first_name,
                            'last_name': user.last_name,
                            'phone': user.phone,
                            'is_bot': user.bot,
                            'is_scam': user.scam,
                            'is_admin': isinstance(
                                user.participant, 
                                (ChannelParticipantAdmin, ChannelParticipantCreator)
                            ),
                            #'about': ( # No relevant info but GetFullUserRequest = 25 secs sleep
                            #    await client(functions.users.GetFullUserRequest(user))
                            #).full_user.about,
                        })

            except FloodWaitError:
                # Re-raise FloodWaitException (will be handled outside of this function)
                # To avoid RPCError general exception to catch it
                raise 
            except AuthKeyError:
                # The session has expired or was broken during the extraction
                raise
            except (ValueError, RPCError) as e:
                logger.error(
                    f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
                )
                await sleep_on_exception(e)
            
            # In case of no error, timeout or RCPError, the function will return 
            # all users that have been already extracted.
            if users:
                # List of users to bulk them to the database
                # Dict with room pk + all its users, to bulk relationships
                return users, {room.pk: users_tgid} if users_tgid else {}
        return [], {}

    try:
        # Start a transaction to avoid race conditions:
        # f.e: run the scanning more than once at the same time extracting the same
        # data (wasting time and creating unnecessary timeouts).
        with transaction.atomic():
            # Rooms which are going to be blocked and used to extract users and msgs
            rooms_to_scan = list(
                RoomItem.objects.filter(pk__in=rooms_pk)
                .select_for_update(skip_locked=True)
                # To be able to extract members tg_id from each Room without more hits 
                # in the database O(2) 
                .prefetch_related(
                    Prefetch('members', queryset=UserItem.objects.all().only('tg_id'))
                ) 
            )

            if rooms_to_scan:
                (msgs_list, rooms_to_update), (users_dict, new_users_id_by_room_pk) = asyncio.run(
                    _run_extract_tasks(rooms_to_scan, auth_key, max_msgs, update_existing_users)
                )

                # NOTE: Using generator to avoid an extra loops (the loop to create the
                # items + the loop inside the bulk methods to iterate them)

                # Create new msgs
                init_time = time.time()
                messages_created = MessageItem.objects.bulk_create(
                    iterchain.from_iterable(msgs_list),
                    batch_size=settings.BULK_BATCH_SIZE
                )
                logger.info(
                    f"{len(messages_created)} MessageItem items were created "
                    f"(bulk_create: {(time.time() - init_time):.3f} secs)."
                )

                # Bulk Update offsets from scanned rooms with new messages
                rooms_to_update = [room for room in rooms_to_update if room] # Remove Nones
                RoomItem.objects.bulk_update(
                    rooms_to_update,
                    ['last_offset', 'last_offset_update', 'last_update', 'last_scan_at', 'is_private', 'is_valid'],
                    batch_size=settings.BULK_BATCH_SIZE
                )

                # Remove users dupicates (same users in different rooms)
                seen_user_tg_id = set()
                unique_users = []
                for user in iterchain.from_iterable(users_dict):
                    if user["tg_id"] not in seen_user_tg_id:
                        unique_users.append(user)
                        seen_user_tg_id.add(user["tg_id"])
                # Create or update Users.
                init_time = time.time()
                affected_users,_ = UserItem.bulk_create_or_update(
                    items=unique_users,
                    unique_key_name="tg_id",
                ) 
                logger.info(
                    f"{len(affected_users)} UserItem items were created/updated "
                    f"(bulk_create_or_update: {(time.time() - init_time):.3f} secs)."
                )
                affected_users_tgid_to_pk = {user.tg_id:user.pk for user in affected_users}
                # Create **new** relations between UserItems and RoomItems
                bulk_add_generic_relationships(
                    UserItem,
                    'rooms',
                    RoomItem,
                    # Dict with Room's pk as keys and a list of **new** users pk as values
                    {
                        room_pk: [
                            affected_users_tgid_to_pk[user_id] 
                            for user_id in users
                        ]
                        for room_pk, users in ChainMap(*new_users_id_by_room_pk).items()
                    },
                )

                # Bind the Query to all the Msgs/Users involved. Rooms are related with 
                # the Query in app_telegram.services.api.scan())
                if token:
                    bulk_add_query_relationships(messages_created + affected_users, token)
                
                # Return will be recieved by postprocess_msgs in the pipeline
                # (there is a celery chain)
                return [msg.pk for msg in messages_created]
    except IntegrityError as e:
        logger.error(
            f"{ e.__class__.__name__ }: {e}"
        )    
    return []

@shared_task(track_started=True, ignore_result=False)
def scan_comments(rooms_pk, auth_key, message_filters = {}, max_msgs = 2500, total_msgs = 1250000, token = None):
    """ Extract replies from all messages in each room. This pipeline is designed
    only for **channels**, not for groups. Telegram hides the replies of a message
    in a channel, so, this pipeline will extract them. 
    
    However, in groups all messages are extracted together (whether they are replies 
    or not) and it is not recommended to call this pipeline as this could lead to a 
    consistency problem (e.g. extracting duplicate messages).

    ----

    This function receives a list of primary keys of rooms instead of all
    the object's data because this date will be extracted here after a 
    'select_for_update' call inside a transaction, protecting the consistency 
    of data from possible race conditions.

    ----

    Parameters
    ----------
    rooms_pk: list[uuid.UUID]
        List of primary keys from RoomItems (Recommended only rooms with `is_channel=True`)
        NOTE: This rows will be blocked (inside a transaction) during the process
        to avoid falling into possible race conditions.

    auth_key: dict
        Dict with the credentials info to login into Telegram API. Required Data:
        ```
        {
            pk: UUID(...)
            name: '...'
            api_id: 123XXXX
            api_hash: "..."
            session: "....."
        }
        ``` 
    
    message_filters: dict, default={}
        Allows to apply filters to which messages are going to be scanned to get their replies. 
        This parameters must be an empty dict or a dictionary with the following structure:
        ```
        {
            "and_filter_fields": {},
            "list_filter_fields": {}
        }
        ```

    max_msgs: int, default=2500
        Max number of replies to be extracted for each message.

    total_msgs: int, default=1250000
        Total number of messages to be extracted globally. In order to avoid an 
        out-of-control usage of resources.

    token: string, default=None
        Query token to add a relation with the extracted items (Users/Messages)

    Returns
    ----------
    message_pks: List[uuid.UUID]
        List of primary keys of the new MessageItem items.
    """

    async def _run_extract_tasks(
        rooms, 
        auth_key,
        max_msgs = 2500, 
        total_msgs = 1250000,
        batch_size = 8,
    ):
        """ To launch a task by each room and type of action
        
        NOTE: (Using access_hash is less common to have a FloodWaitError)
        
        Unlike populate pipeline, here the FloodWaitError/AuthKeyError is handled 
        outside the coroutines. This leads to a loss of information (e.g: with 6 
        coroutines, if the error occurs in the last one we will lose the data of
        all of them). 

        However, losing a couple of messages is not very expensive and allows to 
        have a simpler code than handling the error.

        Parameters
        ----------
        rooms: List[app_telegram.RoomItem]
            List of RoomItem items that are going to be scanned

        auth_key: dict
            Dictionary with the credentials to init a Telegram client. It contains
            the following data: "pk", "session", "api_id" and "api_hash".

        max_msgs: int, default=2500
            Max number of replies to be extracted for each message.

        total_msgs: int, default=1250000
            Total number of messages to be extracted globally. In order to avoid an 
            out-of-control usage of resources

        batch_size: int, default=8
            Max number of requests to be made asynchronously over the Telegram API. 
            It is advisable not to exceed this number to avoid early rate limits.

            Must be >= 2.

            NOTE: This function calls the Telgram API to get two types of information: 
            users and messages. So the bach_size will be split 
            (e.g. batch_size=6 => 3 asynchronous requests for users and 3 for messages).            

        Returns
        ----------
        msgs_list: list[list[MessageItem]]
            List of list with MessageItem elements, to insert create **new** messages 
            into the platform.
            NOTE: Some nested list can be empty '[]'

        rooms_to_update: list[dict]
            List of RoomItem items which last_offset and last_updated fields must be
            updated. Each dictionary
            contains the following data:
        """
        try:
            async with TelegramClient(
                StringSession(auth_key.get("session", "")), 
                auth_key.get("api_id", 0), 
                auth_key.get("api_hash", "")
            ) as client:
                
                await client.start()

                msgs_res = []
                sleep_counter = 0 # To avoid FloodWaitError
                total_msgs_by_room = total_msgs // len(rooms)
                for i in range(0, len(rooms), batch_size):

                    try:
                        # Extract new msgs, updated rooms and related_msgs with new offsets 
                        aux_msgs_res = await asyncio.gather(
                            *[_iterate_messages(client, room, max_msgs, total_msgs_by_room) 
                            for room in rooms[i:i+batch_size]
                            ]
                        )
                        msgs_res += aux_msgs_res

                        sleep_counter += 1
                        # Sleep for 10secs (each 60 rooms) in order to avoid rate limits/bans
                        if sleep_counter == 60/batch_size:
                            logger.debug(
                                f"Anti-FloodWait: [SCAN] '{auth_key.get('name', 'NOT FOUND!')}' sleeping for {SLEEP_AFTER_X_REQUESTS}s)"
                            )
                            sleep_counter = 0
                            await asyncio.sleep(SLEEP_AFTER_X_REQUESTS)
                            
                    except FloodWaitError as e:
                        # Stop scanning...
                        # Handling FloodWaitError here is simple but some information 
                        # can be discarded (the other sibling coroutines that did not 
                        # trigger a rate limit, max 2 corutines info -> Fair enought)
                        if e.seconds <= 1800:
                            logger.info(
                                f"{ e.__class__.__name__ }: [SCAN] Less than 1800secs, sleeping {e.seconds:.3f}secs."
                            )
                            # If FloodWait is less than half and hour it will wait (instead of stop)
                            await asyncio.sleep(e.seconds)
                        else:
                            # Wait time to the Telegram Api Key
                            wait_until = timezone.now() + timezone.timedelta(seconds=e.seconds)
                            # NOTE: Use 'aupdate' version inside async function
                            await TelegramAuth.objects.filter(
                                pk=auth_key.get('pk')
                            ).aupdate(wait_until=wait_until)
                            logger.info(
                                f"{e.__class__.__name__}: [SCAN] Stopping... Auth '{auth_key.get('name')}' have to sleep until '{wait_until}'."
                            )
                            break
            
                # msgs_list rooms_to_update
                return zip(*msgs_res)
        except (ValueError, RuntimeError, AuthKeyError, EOFError) as e:
            # Problem with this auth item, set 'is_valid' to false
            # NOTE: Use 'aupdate' version inside async function
            await TelegramAuth.objects.filter(pk=auth_key.get('pk')).aupdate(is_valid=False)
            logger.error(
                f"{e.__class__.__name__ }: Error in auth '{auth_key.get('name', 'NOT FOUND!')}' "
                f"- {e}"
            )
            return ([], [])
        
    async def _iterate_messages(
        client: TelegramClient,
        room: RoomItem, 
        max_msgs: int,
        total_msgs: int,
    ):
        """ Iterate all messages from `room` and extract new replies from them.
        NOTE: This pipeline is designed only for channels, not for groups. Telegram
        hides the replies of a message in a channel, so, this pipeline will extract
        them.
        
        Parameters
        ----------
        client: TelegramClient
            TelegramClient object to use to extract the messages.

        room: RoomItem
            RoomItem item to extract the new messages from.
        
        max_msgs: int
            Max number of replies to be extracted for each message.

        total_msgs: int
            Total number of messages to be extracted for this room. In order to avoid an 
            out-of-control usage of resources.

        Returns
        ----------
        msgs_list: list[MessageItem]
            List of new MessageItem items to be created.

        room_to_update: RoomItem
            RoomItem item to update. None if the room wasn't updated.

        related_msg_to_update: list[MessageItem]
            MessageItem item to update (last_offset, last_offset_update, last_scan_at and is_valid).
        """

        new_msgs = []
        edited_related_msgs = []
        updated_room = None
        for message in room.prefetch_msgs:
            msgs, updated_room, updated_message  = await extract_msgs(
                client=client,
                room=room,
                max_msgs=max_msgs,
                related_msg=message,
            )
            new_msgs += msgs
            edited_related_msgs.append(updated_message)

            if len(edited_related_msgs) >= total_msgs:
                logger.info(
                    f"Stopping iteration of replies in '{room.link}' - total msgs for this room reached ({total_msgs})."
                )
                break

            if updated_room and (updated_room.is_private or not updated_room.is_valid):
                # If the room can't be accessed, stop the iteration of replies
                logger.error(
                    f"Stopping iteration of replies in '{room.link}' - room is private or not valid."
                )
                break

        return new_msgs, updated_room, edited_related_msgs

    try:
        # Start a transaction to avoid race conditions:
        # f.e: run the scanning more than once at the same time extracting the same
        # data (wasting time and creating unnecessary timeouts).
        with transaction.atomic():

            # Prepare the prefetch query to get the messages
            prefetch_msg_qs = MessageItem.objects.defer("text", "annotated_text", "media_type")
            if message_filters:
                prefetch_msg_qs = prefetch_msg_qs.filter(create_advance_filter(**message_filters))

            # Rooms which are going to be blocked and used to extract users and msgs
            rooms_to_scan = list(
                RoomItem.objects.filter(pk__in=rooms_pk)
                .select_for_update(skip_locked=True)
                .prefetch_related(
                    Prefetch(
                        "messages", 
                        # Ignore heavy fields (that are not going to be used)
                        queryset=prefetch_msg_qs,
                        to_attr='prefetch_msgs'
                    )
                ) 
                .only(
                    "pk",
                    "tg_id",
                    "link",
                    "last_offset",
                    "last_update",
                    "is_private",
                    "access_auth",
                    "access_hash",
                    "is_valid",
                )
            )

            if rooms_to_scan:
                (msgs_list, rooms_to_update, related_msgs_to_update) = asyncio.run(
                    _run_extract_tasks(rooms_to_scan, auth_key, max_msgs, total_msgs)
                )

                # Create new msgs
                init_time = time.time()
                messages_created = MessageItem.objects.bulk_create(
                    iterchain.from_iterable(msgs_list),
                    batch_size=settings.BULK_BATCH_SIZE
                )
                logger.info(
                    f"{len(messages_created)} MessageItem items were created "
                    f"(bulk_create: {(time.time() - init_time):.3f} secs)."
                )

                # Update parent msgs
                init_time = time.time()
                related_msgs_to_update = [
                    msg 
                    for msg in iterchain.from_iterable(related_msgs_to_update) 
                    if msg
                ] # Remove Nones
                MessageItem.objects.bulk_update(
                    related_msgs_to_update,
                    ['last_offset', 'last_offset_update' , 'last_scan_at', 'is_valid'],
                    batch_size=settings.BULK_BATCH_SIZE
                )
                logger.info(
                    f"{len(messages_created)} MessageItem items were updated "
                    f"(bulk_create: {(time.time() - init_time):.3f} secs)."
                )

                # Bulk Update offsets from scanned rooms with new messages
                rooms_to_update = [room for room in rooms_to_update if room] # Remove Nones
                RoomItem.objects.bulk_update(
                    rooms_to_update,
                    ['last_offset', 'last_offset_update', 'last_update', 'last_scan_at', 'is_private', 'is_valid'],
                    batch_size=settings.BULK_BATCH_SIZE
                )

                # Bind the Query to all the new Msgs. 
                if token:
                    bulk_add_query_relationships(messages_created, token)
                
                # Return will be recieved by postprocess_msgs in the pipeline
                # (there is a celery chain)
                return [msg.pk for msg in messages_created]
    except IntegrityError as e:
        logger.error(
            f"{ e.__class__.__name__ }: {e}"
        )    
    return []


# ======================================================
# =====        CELERY TASKS: DATA PROCESSORS       =====
# ======================================================

@shared_task(track_started=True, ignore_result=False)
def postprocess_msgs(msgs_pk, token = None):
    """ Messages Post-processing

    Returns
    ---------- 
    msgs_dict: dict
        Returns a dict compatible with `app_telegram.services.telegram.items_to_index`
        to allow the connection of these tasks in a Celery Pipeline.
    """
    msg_to_annotate = {}
    for msg in MessageItem.objects.filter(pk__in=msgs_pk).values('pk', 'annotated_text', 'lang'):
        # Group by lang
        msg_to_annotate.setdefault(msg["lang"], []).append(
            {
                "pk": msg["pk"], 
                "text": msg["annotated_text"]
            }
        )

    # Annotate text (create entities and relate them to MessageItems)
    # Celery Pipeline: Return will be recieved by ner_db_store
    return ner_extraction(msg_to_annotate)

    