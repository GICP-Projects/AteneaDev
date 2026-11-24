import re
import uuid
import time
import logging
import asyncio
import sys
from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from asgiref.sync import async_to_sync, sync_to_async
#import traceback
from itertools import chain as iterchain
from collections import ChainMap
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.db.models import Prefetch
from django.db import transaction, IntegrityError, OperationalError, close_old_connections, connections
from app_base.tagger import extract_tags
from app_base.utils import (
    convert_to_standard_text, 
    markdown_to_text, 
    detect_lang,
    AsyncTimedIterable
)
from app_base.api import (
    create_advance_filter,
    bulk_add_query_relationships, 
    bulk_add_generic_relationships,
)
from app_entity.ner_state import NER_INPUT_NORMALIZER_VERSION, build_ner_input_hash
from app_scheduler.db import retry_on_deadlock
from app_scheduler.contracts import ProcessingSummary, empty_processing_summary
from app_scheduler.enums import ItemProgressState, JobStatus, TaskOutcome
from app_telegram.enums import SCAN_COMMENT_PARENT_TERMINAL_STATES
from app_telegram.models import (
    SeedItem, 
    TelegramAuth, 
    BaseTelegramEntity,
    RoomItem, 
    UserItem, 
    MessageItem
)
from app_telegram.services.room_reconciliation import (
    RoomIdentitySnapshot, RoomReconciliationError,
    reconcile_with_dependencies, ensure_message_links_available, rebase_messages,
    mark_room_metadata_pending, room_identity_changed,
)
from celery.app import shared_task
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import (
    User, 
    Channel, 
    InputChannel,
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
SLEEP_AFTER_EMPTY_COMMENT_PARENT = 5

# Checkpoint size and Telegram parallelism are independent: six rooms need up
# to twelve extraction operations, but at most eight run concurrently.
SCAN_ROOMS_PER_CHECKPOINT = 6
SCAN_MAX_CONCURRENT_OPERATIONS = 8

# Telethon's message iterator fetches at most 100 messages per API page. Comments
# use a progressive pause between pages; room-history scans keep their 250 ms
# iterator wait.
COMMENTS_GET_REPLIES_PAGE_SIZE = 100
COMMENTS_PAGE_SLEEP_INITIAL_SECONDS = 1.0
COMMENTS_PAGE_SLEEP_INCREMENT_SECONDS = 0.5
COMMENTS_PAGE_SLEEP_MAX_SECONDS = 3.0

# MAX WAIT TIME TO THE TELEGRAM API (in seconds)
MAX_WAIT_TIME = 300 # 5 mins


async def _fetch_room_about_if_changed(client, room, snapshot):
    """Read the description only after a public identity/title/link change.

    Run during extraction, before checkpoint writes. Failure must defer the
    Room rather than publish a partial history entry with an unobserved about.
    """
    if snapshot.about_observed or not snapshot.username or not room_identity_changed(room, snapshot):
        return snapshot
    try:
        response = await asyncio.wait_for(
            client(functions.channels.GetFullChannelRequest(InputChannel(room.tg_id, room.access_hash))),
            MAX_WAIT_TIME,
        )
        full_chat = getattr(response, "full_chat", None)
        if getattr(full_chat, "id", None) != room.tg_id or not hasattr(full_chat, "about"):
            raise RoomReconciliationError("room_about_unavailable")
        about = full_chat.about
        if about is not None and not isinstance(about, str):
            raise RoomReconciliationError("room_about_unavailable")
    except (FloodWaitError, AuthKeyError):
        raise
    except (ValueError, RPCError, TimeoutError, OSError) as exc:
        raise RoomReconciliationError("room_about_unavailable") from exc
    return replace(snapshot, about=about, about_observed=True)


async def _fetch_room_identity(client, room):
    entity = await asyncio.wait_for(
        client.get_entity(InputPeerChannel(room.tg_id, room.access_hash)), MAX_WAIT_TIME,
    )
    snapshot = RoomIdentitySnapshot.from_entity(entity, room.tg_id)
    if snapshot is None:
        raise RoomReconciliationError("room_metadata_unavailable")
    return await _fetch_room_about_if_changed(client, room, snapshot)


def _refresh_peer_identity(room, *, runner=None, client=None, auth_id=None):
    """Read an owner using its own credential-bound access hash."""
    auth = room.access_auth
    if not auth or not auth.is_valid or auth.is_hibernated or auth.wait_until > timezone.now():
        raise RoomReconciliationError("room_owner_unavailable")
    shared_credential = client is not None and str(auth.pk) == str(auth_id)

    async def fetch():
        if shared_credential:
            return await _fetch_room_identity(client, room)
        peer_client = TelegramClient(
            StringSession(auth.session), auth.api_id, auth.api_hash,
            receive_updates=False, flood_sleep_threshold=0,
        )
        try:
            await peer_client.connect()
            return await _fetch_room_identity(peer_client, room)
        finally:
            await peer_client.disconnect()

    try:
        return runner.run(fetch()) if runner is not None else async_to_sync(fetch)()
    except (FloodWaitError, AuthKeyError) as exc:
        if shared_credential:
            # The scan's existing handler records the credential cooldown or
            # invalidation after rolling back its active checkpoint.
            raise
        raise RoomReconciliationError("room_owner_unavailable") from exc
    except (RPCError, ValueError, TimeoutError, OSError) as exc:
        raise RoomReconciliationError("room_owner_unavailable") from exc


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


def _create_scan_message_handoffs(messages, *, target_job_id=None, source_batch_id=None):
    """Persist scan output for its child message Job in the current transaction.

    This helper deliberately lives next to the code that inserts
    ``MessageItem`` rows: there is no interval in which a committed message can
    be lost between scan and its downstream workflow.  It is a no-op for the
    direct callers of ``scan_rooms``/``scan_comments`` that do not participate
    in a ProcessingJob.
    """

    if not target_job_id or not source_batch_id or not messages:
        return []
    # Imported lazily to keep Telegram extraction usable without importing the
    # scheduler during module initialisation.
    from app_scheduler.models import ProcessingBatch, ProcessingJob
    from app_scheduler.services.jobs import create_handoffs_locked

    source_batch = ProcessingBatch.objects.select_for_update(no_key=True).get(pk=source_batch_id)
    target_job = ProcessingJob.objects.get(pk=target_job_id)
    return create_handoffs_locked(
        source_batch,
        target_job=target_job,
        item_content_type=ContentType.objects.get_for_model(MessageItem, for_concrete_model=False),
        item_ids=[str(message.pk) for message in messages],
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
def populate_seeds(seeds_pk, auth_key, token = None, source_batch_id=None, lease_token=None):
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
        `app_telegram.services.index.items_to_index` to allow the connection
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
            observed_at = timezone.now()

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
                item["_identity_observed_at"] = observed_at
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

        if source_batch_id:
            from app_scheduler.models import ProcessingBatch
            from app_scheduler.services.jobs import ensure_stage_lease_locked
            from app_telegram.job_keys import TELEGRAM_POPULATE_STAGE
            batch = ProcessingBatch.objects.select_for_update(no_key=True, of=("self",)).select_related("job").get(pk=source_batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_POPULATE_STAGE, token=lease_token)

        # Bulk create/update for each type
        affected_items = []
        rooms_pk = []
        metadata_room_ids = set()
        for model_class, items_dict in items_by_model.items():
            if model_class == RoomItem:
                items = []
                seeds_by_id = {seed.pk: seed for seed in seeds_to_update}
                for data in items_dict:
                    room_sync_ids = set()
                    try:
                        with transaction.atomic():
                            existing = RoomItem.objects.select_for_update(nowait=True).filter(tg_id=data["tg_id"]).first()
                            snapshot = RoomIdentitySnapshot(
                                data["tg_id"], data["unique_name"], data["title"],
                                data.pop("_identity_observed_at", timezone.now()),
                                about=data["about"], about_observed=True,
                            )
                            if existing is not None:
                                current, room_ids = reconcile_with_dependencies(
                                    existing, snapshot, _refresh_peer_identity,
                                )
                                room_sync_ids.update(room_ids)
                                # Identity and description share one atomic history entry.
                                rest = {key: value for key, value in data.items() if key not in {"link", "unique_name", "title", "about", "tg_id"}}
                                if snapshot.observed_at >= current.username_checked_at:
                                    metadata_changed = {
                                        key
                                        for key in ("tags", "lang", "is_channel", "created_at")
                                        if key in rest and getattr(current, key) != rest[key]
                                    }
                                    RoomItem.objects.filter(pk=current.pk).update(**rest)
                                    for key, value in rest.items():
                                        setattr(current, key, value)
                                    if metadata_changed:
                                        mark_room_metadata_pending(current, metadata_changed)
                                        room_sync_ids.add(str(current.pk))
                                items.append(current)
                            else:
                                if data["unique_name"]:
                                    try:
                                        data["link"] = RoomItem.build_public_link(data["unique_name"])
                                    except ValueError as exc:
                                        raise RoomReconciliationError("room_username_missing") from exc
                                    owner = RoomItem.objects.filter(link=data["link"]).first()
                                    if owner:
                                        _, room_ids = reconcile_with_dependencies(
                                            owner, _refresh_peer_identity(owner), _refresh_peer_identity,
                                            force=True,
                                        )
                                        room_sync_ids.update(room_ids)
                                        if RoomItem.objects.filter(link=data["link"]).exists():
                                            raise RoomReconciliationError("room_username_conflict")
                                new = RoomItem(**data, username_checked_at=snapshot.observed_at)
                                RoomItem.objects.bulk_create([new])
                                items.append(new)
                        metadata_room_ids.update(room_sync_ids)
                    except (RoomReconciliationError, OperationalError) as exc:
                        if isinstance(exc, OperationalError) and getattr(exc.__cause__, "pgcode", None) != "55P03":
                            raise
                        seeds_by_id[data["seed_item_id"]].is_seeded = False
                        logger.warning("Populate deferred room %s: %s", data["tg_id"], exc)
            else:
                items, _ = model_class.bulk_create_or_update(
                    items=items_dict, unique_key_name="tg_id", update_fields=items_dict[0].keys(),
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

        if metadata_room_ids:
            from app_telegram.services.jobs import queue_room_metadata_sync
            queue_room_metadata_sync(metadata_room_ids, source_batch_id=source_batch_id)

        if rooms_pk:
            return {
                RoomItem.__name__: {"and_filter_fields": {"pk__in": rooms_pk}}
                # Users are not indexed at the moment
            }
        return {}


####################################################
####          ITERATE OVER MESSAGES             ####
####################################################

@dataclass
class ExtractMessagesResult:
    """An extraction attempt, including data received before a Telegram error."""

    messages: list = field(default_factory=list)
    room: RoomItem | None = None
    parent: MessageItem | None = None
    fetched_count: int = 0
    completed: bool = True
    reason: str = ""
    error: Exception | None = None
    room_identity: RoomIdentitySnapshot | None = None


async def _extract_messages_result(
    client: TelegramClient, 
    room: RoomItem, 
    max_msgs: int,
    related_msg: MessageItem = None,
    check_room=False,
):
    """Extract at most ``max_msgs`` records using a client's event loop.

    Return an ``ExtractMessagesResult`` with unsaved messages, mutated offset
    objects, completion status, fetched-record count and any Telegram error.
    ``related_msg`` selects a parent's comments instead of room history.
    This function does not write to the database. Expected Telegram and timeout
    errors retain partial results; unexpected errors and cancellation propagate.
    
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
    -------
    ExtractMessagesResult
        Unsaved records and changed objects, completion/reason, received-record
        count and any expected error. Comments do not advance the room offset.
    """
    def _choose_media_type(msg):
        """Return the media category of a Telegram record without persistent effects."""
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
    result = ExtractMessagesResult(messages=msg_list)
    last_offset = None
    try:
        # Update the `last_scan_at` field
        room.last_scan_at = timezone.now()
        if related_msg:
            related_msg.last_scan_at = timezone.now()

        input_room = InputPeerChannel(room.tg_id, room.access_hash)
        # wait_for to avoid long waiting times (when Telegram API doesn't return the messages)
        async for msg in AsyncTimedIterable(
            iterable=client.iter_messages(
                entity=input_room, 
                reverse=True, # [1] -> First, [-1] -> Last
                limit=max_msgs, 
                reply_to=related_msg.msg_id if related_msg else None,
                offset_id= related_msg.last_offset if related_msg else room.last_offset,
                # Comments apply explicit progressive waits after each page;
                # room history retains its existing 250 ms inter-page wait.
                wait_time=0 if related_msg else 0.250
            ), 
            timeout=MAX_WAIT_TIME, 
            sentinel=TimeoutError()
        ):
            
            # Check if the iteration has timed out and broken the loop
            if isinstance(msg, TimeoutError):
                logger.error(
                    f"{TimeoutError.__name__}: In '{room.link if room else 'NOT FOUND'}'"
                    f" - TimeoutError after {MAX_WAIT_TIME}s. {len(msg_list)} messages have been extracted."
                )
                result.completed = False
                result.error = msg
                result.reason = "timeout"
                break

            if check_room and related_msg is None and result.room_identity is None:
                snapshot = RoomIdentitySnapshot.from_entity(getattr(msg, "chat", None), room.tg_id)
                if snapshot and snapshot.username:
                    result.room_identity = snapshot

            # Ignore action message (generated by telegram app)
            if not msg.action:
                # Remove markdown notation
                # If this normalization changes, bump
                # app_entity.ner_state.NER_INPUT_NORMALIZER_VERSION.
                clean_text = markdown_to_text(msg.text)
                # formatted text to standard ASCII
                clean_text = convert_to_standard_text(clean_text).strip()
                # Remove consecutive newlines
                clean_text = re.sub(r'\n+', '\n', clean_text)
                # Remove repeated characters like '--' or **
                clean_text = re.sub(r'([-_=+*]){2,}', '', clean_text)
                message_lang = (
                    detect_lang(
                        extract_tags(
                            clean_text,
                            hashtag=False,
                            date=False,
                            time=False,
                            number=False,
                        )[0]
                    )
                    if clean_text else ""
                )

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
                            # This is deliberately calculated at ingestion for
                            # new messages. Historical rows remain null until
                            # their next explicit NER pass, while the durable
                            # NER owner still recomputes/validates it before
                            # extraction.
                            'ner_input_hash': (
                                build_ner_input_hash(
                                    clean_text,
                                    message_lang,
                                    NER_INPUT_NORMALIZER_VERSION,
                                )
                                if clean_text and message_lang
                                else None
                            ),
                            # Remove emails, urls, @mentions and emojis for lang detection.
                            'lang': message_lang,
                            'sender': msg.sender.id if msg.sender else None, 
                            'media_type': _choose_media_type(msg),
                            'is_reply': msg.is_reply,
                            'created_at': msg.date,
                            # Story replies use MessageReplyStoryHeader, which
                            # has story_id instead of reply_to_msg_id.  They are
                            # not replies to a MessageItem, so leave the
                            # message-reply relation empty.
                            'reply_to_id': getattr(msg.reply_to, 'reply_to_msg_id', None),
                            'reply_to_msg': related_msg if related_msg else None,
                        }   
                    )
                )   
                # Store the last offset
                last_offset = msg.id
            elif related_msg:
                # Service records consume the limit without creating comments.
                # Advance past them, but never advance past a record whose
                # conversion failed before it could join the persisted unit.
                last_offset = msg.id
            result.fetched_count += 1

            # Telethon requests at most 100 messages per page. Pace comments
            # progressively between GetReplies pages, capped at three seconds.
            # This executes before the iterator asks Telegram for its next page.
            if (
                related_msg
                and result.fetched_count % COMMENTS_GET_REPLIES_PAGE_SIZE == 0
                and result.fetched_count < max_msgs
            ):
                page_number = result.fetched_count // COMMENTS_GET_REPLIES_PAGE_SIZE
                page_sleep = min(
                    COMMENTS_PAGE_SLEEP_INITIAL_SECONDS
                    + (page_number - 1) * COMMENTS_PAGE_SLEEP_INCREMENT_SECONDS,
                    COMMENTS_PAGE_SLEEP_MAX_SECONDS,
                )
                logger.info(
                    "Anti-FloodWait: [SCAN COMMENTS] '%s' sleeping for %.1fs after "
                    "GetReplies page %s (%s records) for parent %s.",
                    room.link if room else "unknown room",
                    page_sleep,
                    page_number,
                    result.fetched_count,
                    related_msg.msg_id,
                )
                await asyncio.sleep(page_sleep)
    
    except ChannelPrivateError as e:
        logger.error(
            f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
        )
        # the Telegram auths works but the room is private and can't be accessed 
        # without an invite link
        room.is_private = True
        room.last_update = timezone.now()
        result.room = room
        result.reason = "room_private"
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
        if related_msg is None:
            raise
        related_msg.is_valid = False
        result.parent = related_msg
        result.reason = type(e).__name__
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
        result.room = room
        result.reason = "room_invalid"
    except (FloodWaitError, AuthKeyError) as e:
        # Keep data already received; the caller commits it before deferring.
        result.completed = False
        result.error = e
        result.reason = type(e).__name__
    except (ValueError, RPCError) as e:
        logger.error(
            f"{ e.__class__.__name__ }: In '{room.link if room else 'NOT FOUND'}' - {e}"
        )
        result.completed = False
        result.error = e
        result.reason = type(e).__name__

    if check_room and related_msg is None:
        try:
            if result.room_identity is None and result.completed and result.error is None:
                result.room_identity = await _fetch_room_identity(client, room)
            if result.room_identity is None:
                raise RoomReconciliationError("room_metadata_unavailable")
            if not result.room_identity.username:
                room.is_private = True
                raise RoomReconciliationError("room_username_missing")
            result.room_identity = await _fetch_room_about_if_changed(client, room, result.room_identity)
            room._identity_snapshot = result.room_identity
        except (RoomReconciliationError, ValueError, RPCError, TimeoutError, OSError) as exc:
            if isinstance(exc, (FloodWaitError, AuthKeyError)):
                result.error = exc
            room._room_check_error = getattr(exc, "reason", "room_metadata_unavailable")
            result.reason = room._room_check_error
            result.messages.clear()
            result.room = None
            result.completed = False
            return result

    # Only in case of new messages extracted. In case of timeout or RCPError, 
    # the function will return all message that have been already extracted until the exception.
    if last_offset is not None:
        # update the `last_offest` with the last msg id
        if related_msg:
            related_msg.last_offset = last_offset
            # auto_now does not trigger on bulk_update(), whereupon, manually 
            related_msg.last_offset_update = timezone.now()
            result.parent = related_msg
        else:
            room.last_offset = last_offset
            # auto_now does not trigger on bulk_update(), whereupon, manually 
            room.last_offset_update = timezone.now()
            room.last_update = timezone.now() 
            result.room = room
    # A normal scan that finds no new messages is still a completed room
    # operation.  Persist ``last_scan_at`` so durable Scan batches can report
    # it as processed rather than incorrectly deferred.  Timeout/RPC paths
    # deliberately leave it untouched, preserving their partial/deferred
    # semantics for the next run.
    if result.completed and not result.reason:
        if related_msg:
            # A valid empty response means there are currently no newer
            # comments. Keep is_valid=True so future executions can scan it.
            result.parent = related_msg
        else:
            result.room = room
    return result


async def extract_msgs(client: TelegramClient, room: RoomItem, max_msgs: int, related_msg=None, check_room=False):
    """Adapt structured extraction to the existing ``(messages, room, parent)`` API.

    Parameters select room history or one parent's comments. Return unsaved
    messages and changed offset objects; persist nothing. FloodWait/AuthKey
    errors propagate to existing room-scan consumers, while transient errors
    retain fetched records and the existing exception delay.
    """
    result = await _extract_messages_result(client, room, max_msgs, related_msg, check_room=check_room)
    if isinstance(result.error, (FloodWaitError, AuthKeyError)):
        raise result.error
    if result.error is not None:
        await sleep_on_exception(result.error)
    return result.messages, result.room, result.parent


@shared_task(track_started=True, ignore_result=False)
def scan_rooms(
    rooms_pk,
    auth_key,
    max_msgs=2500,
    update_existing_users=False,
    token=None,
    processing_handoff_target_job_id=None,
    source_batch_id=None,
    lease_token=None,
    rooms_per_checkpoint=SCAN_ROOMS_PER_CHECKPOINT,
    max_concurrent_operations=SCAN_MAX_CONCURRENT_OPERATIONS,
    check_room=False,
):
    """Extract messages and users/user_bots (in groups) from each room.

    This function receives room primary keys instead of preloaded objects so
    each round can read and lock its RoomItems with select_for_update inside
    its own transaction, protecting the data from concurrent scans.

    Each transaction commits messages, offsets, users, relationships, handoffs
    and Batch progress together. A failure rolls back only the active round;
    earlier rounds remain committed. One Telegram client serves all rounds.

    TODO: (Future) Extract videos and images

    Parameters
    ----------
    rooms_pk: list[uuid.UUID]
        List of primary keys from RoomItems. Duplicate IDs are removed.
        NOTE: Only the current round's rows are locked during extraction and
        persistence. Locks are released when that round commits or rolls back.
        For a scheduler Batch, rooms with recorded outcomes are not rescanned.

    auth_key: dict
        Dictionary with the credentials to log into the Telegram API:
        {
            "pk": UUID(...),
            "name": "...",
            "api_id": 123XXXX,
            "api_hash": "...",
            "session": "...",
        }

    max_msgs: int, default=2500
        Maximum number of messages to extract from each room in this scan.

    update_existing_users: bool, default=False
        Update already existing users from the extracted ones.

    token: str, default=None
        Query token to relate the extracted users and messages to a Query.

    processing_handoff_target_job_id: uuid.UUID, default=None
        Downstream Job that receives handoffs for newly stored messages.

    source_batch_id: uuid.UUID, default=None
        Scheduler Batch whose room progress is saved with each checkpoint.
        When supplied, each scanned room must belong to that Batch's item_ids
        and the caller must not have an enclosing database transaction.

    lease_token: str, default=None
        Execution token for the scheduler Batch. Checked before extraction and
        again before saving so a replaced execution cannot commit stale data.

    rooms_per_checkpoint: int, default=SCAN_ROOMS_PER_CHECKPOINT (6)
        Maximum number of rooms in one independently committed round.
        Must be positive. This controls persistence frequency, not concurrency.

    max_concurrent_operations: int, default=SCAN_MAX_CONCURRENT_OPERATIONS (8)
        Maximum simultaneous message/member extraction operations.
        Each room schedules one operation for messages and one for members;
        each operation may make multiple Telegram requests while paginating.
        Must be positive. This limit is independent of checkpoint size.

    Returns
    -------
    message_pks: list[uuid.UUID]
        Primary keys of new MessageItems committed during this invocation.
        Previously committed messages from earlier attempts are not included.

    Notes
    -----
    FloodWait and authentication errors propagate to the task handler after
    the active round rolls back. Previously committed rounds remain stored
    even though this invocation raises instead of returning their IDs.
    The scheduler uses durable Batch progress to retry only pending rooms.
    """
    from app_scheduler.models import ProcessingBatch
    from app_scheduler.services.jobs import ensure_stage_lease_locked, refresh_processing_job
    from app_telegram.services.jobs import (
        TELEGRAM_SCAN_STAGE, pending_scan_room_ids,
        scan_rooms_batch_counts, scan_rooms_stage_summary,
    )

    if rooms_per_checkpoint < 1 or max_concurrent_operations < 1:
        raise ValueError("Room checkpoint size and operation concurrency must be positive")
    if source_batch_id and transaction.get_connection().in_atomic_block:
        raise RuntimeError("Durable room checkpoints require no enclosing database transaction")
    room_ids = list(dict.fromkeys(map(str, rooms_pk)))
    if not room_ids:
        return []

    async def _open_client():
        client = TelegramClient(
            StringSession(auth_key.get("session", "")),
            auth_key.get("api_id", 0), auth_key.get("api_hash", ""),
        )
        try:
            await client.__aenter__()
            await client.start()
        except BaseException:
            try:
                await client.__aexit__(*sys.exc_info())
            except BaseException:
                logger.exception("Telegram client cleanup failed during room scan startup")
            raise
        return client

    async def _run_extract_tasks(client, rooms):
        """Extract messages and members for one round without saving results.

        Parameters
        ----------
        client: TelegramClient
            Connected client shared by all rounds on the same event loop.
        rooms: list[RoomItem]
            Current round's rooms, already locked by the caller's transaction.

        Returns
        -------
        msg_results: list[tuple]
            One (messages, updated_room, related_parent) result per room, in
            input order. Messages are unsaved MessageItems; related_parent is
            unused for room history. updated_room can be None.
        member_results: list[tuple]
            One (users, new_users_by_room) result per room, in input order.
            Users are dictionaries and may repeat across rooms.
            new_users_by_room maps RoomItem primary keys to new member tg_ids.

        Notes
        -----
        Schedules two extraction operations per room, with at most
        max_concurrent_operations active at once. A six-room round schedules
        twelve operations; the default limit lets eight run concurrently.
        A failure cancels and settles sibling operations before propagating
        to the caller, which rolls back the current round's transaction.
        """
        semaphore = asyncio.Semaphore(max_concurrent_operations)

        async def extract_messages(room):
            async with semaphore:
                if check_room:
                    return await extract_msgs(client, room, max_msgs, check_room=True)
                return await extract_msgs(client, room, max_msgs)

        async def extract_members(room):
            async with semaphore:
                return await _extract_members(client, room, auth_key.get("pk"), update_existing_users)

        tasks = [asyncio.create_task(extract_messages(room)) for room in rooms]
        tasks += [asyncio.create_task(extract_members(room)) for room in rooms]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results[:len(rooms)], results[len(rooms):]

    def _extract_round(runner, client, rooms):
        # A Celery soft timeout can interrupt Runner.run outside the coroutine.
        # Keep its task so we can cancel/settle it before closing the client.
        task = runner.get_loop().create_task(_run_extract_tasks(client, rooms))

        async def await_result():
            return await task

        async def settle():
            await asyncio.gather(task, return_exceptions=True)

        try:
            return runner.run(await_result())
        except BaseException:
            task.cancel()
            runner.run(settle())
            raise

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

    def _persist_round(msg_results, member_results, rooms, runner, client):
        """Write one round's messages, offsets, users, relations and handoffs.

        The caller opens transaction.atomic before acquiring room locks and
        calls this function inside it. Returning does not commit: the caller
        still saves Batch progress before exiting that same transaction.
        """
        messages_created, rooms_to_update, accepted_members, deferred = [], [], [], {}
        metadata_room_ids = set()

        def refresh_peer(peer):
            return _refresh_peer_identity(peer, runner=runner, client=client, auth_id=auth_key.get("pk"))

        for room, result, members in zip(rooms, msg_results, member_results):
            messages, updated_room, _ = result
            if updated_room is None:
                deferred[str(room.pk)] = getattr(room, "_room_check_error", "room_not_updated")
                if room.is_private:
                    RoomItem.objects.filter(pk=room.pk).update(is_private=True)
                continue
            try:
                with transaction.atomic():
                    room_sync_ids = {str(room.pk)} if room.metadata_sync_pending_at else set()
                    if check_room and hasattr(room, "_identity_snapshot"):
                        current, room_ids = reconcile_with_dependencies(
                            room, room._identity_snapshot, refresh_peer,
                        )
                        room_sync_ids.update(room_ids)
                        room.link, room.unique_name = current.link, current.unique_name
                    rebase_messages(messages, room.link)
                    room_sync_ids.update(ensure_message_links_available(
                        messages, refresh_peer if check_room else None,
                    ))
                    created = MessageItem.objects.bulk_create(messages, batch_size=settings.BULK_BATCH_SIZE)
            except (RoomReconciliationError, OperationalError, IntegrityError) as exc:
                if isinstance(exc, OperationalError) and getattr(exc.__cause__, "pgcode", None) != "55P03":
                    raise
                if isinstance(exc, IntegrityError) and "link" not in (getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""):
                    raise
                deferred[str(room.pk)] = getattr(exc, "reason", "room_reconciliation_locked" if isinstance(exc, OperationalError) else "message_link_conflict")
                logger.warning("Room %s deferred: %s", room.tg_id, deferred[str(room.pk)])
                continue
            metadata_room_ids.update(room_sync_ids)
            messages_created.extend(created)
            rooms_to_update.append(updated_room)
            accepted_members.append(members)
        RoomItem.objects.bulk_update(
            rooms_to_update,
            ["last_offset", "last_offset_update", "last_update", "last_scan_at", "is_private", "is_valid"],
            batch_size=settings.BULK_BATCH_SIZE,
        )
        unique_users = {}
        for users, _ in accepted_members:
            for user in users:
                unique_users.setdefault(user["tg_id"], user)
        affected_users = []
        if unique_users:
            affected_users, _ = UserItem.bulk_create_or_update(
                items=list(unique_users.values()), unique_key_name="tg_id",
            )
        users_by_tg_id = {user.tg_id: user.pk for user in affected_users}
        new_members = dict(ChainMap(*(result[1] for result in accepted_members)))
        if new_members:
            bulk_add_generic_relationships(
                UserItem, "rooms", RoomItem,
                {room_pk: [users_by_tg_id[user_id] for user_id in users]
                 for room_pk, users in new_members.items()},
            )
        if token and (messages_created or affected_users):
            bulk_add_query_relationships(messages_created + affected_users, token)
        _create_scan_message_handoffs(
            messages_created, target_job_id=processing_handoff_target_job_id,
            source_batch_id=source_batch_id,
        )
        if metadata_room_ids:
            from app_telegram.services.jobs import queue_room_metadata_sync
            queue_room_metadata_sync(metadata_room_ids, source_batch_id=source_batch_id)
        return messages_created, rooms_to_update, deferred

    committed_ids = []
    rooms_since_sleep = 0
    try:
        # Runner keeps one event loop for the shared Telegram client across all rounds.
        # It bridges async extraction and sync DB commits without reconnecting per round.
        with asyncio.Runner() as runner:
            client = None
            @retry_on_deadlock
            def process_room_round(round_ids):
                nonlocal client
                with transaction.atomic():
                    batch = None
                    if source_batch_id:
                        # Check this attempt's token and inputs before API
                        # work. Pausing stops before starting another round.
                        batch = ProcessingBatch.objects.select_related("job").get(pk=source_batch_id)
                        ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_STAGE, token=lease_token)
                        if not set(round_ids).issubset(map(str, batch.item_ids)):
                            raise ValueError("Scan rooms must be explicit Batch inputs")
                        if batch.job.status == JobStatus.PAUSED:
                            return None
                        pending = set(pending_scan_room_ids(batch))
                        round_ids = [room_id for room_id in round_ids if room_id in pending]
                    if not round_ids:
                        return [], 0, 0
                    # Only this round's rooms stay locked during extraction.
                    # Acquire the Batch lock after Telegram so pause/recovery
                    # can replace the lease while API work is in progress.
                    rooms = list(
                        RoomItem.objects.filter(pk__in=round_ids)
                        .select_for_update(skip_locked=True, of=("self",))
                        .order_by("pk")
                        .prefetch_related(Prefetch("members", queryset=UserItem.objects.all().only("tg_id")))
                    )
                    changed_credentials = {
                        str(room.pk) for room in rooms
                        if source_batch_id and str(room.access_auth_id) != str(auth_key.get("pk"))
                    }
                    rooms = [room for room in rooms if str(room.pk) not in changed_credentials]
                    # 3. Fetch this round's results into memory. Reuse the
                    # connection opened by the first nonempty round.
                    if rooms:
                        if client is None:
                            client = runner.run(_open_client())
                        msg_results, member_results = _extract_round(runner, client, rooms)
                    else:
                        msg_results, member_results = [], []
                    # 4. Lock the Batch and recheck the token before any
                    # writes: recovery may have replaced this attempt
                    # while Telegram requests were in progress.
                    if batch is not None:
                        batch = (
                            ProcessingBatch.objects.select_for_update(no_key=True, of=("self",))
                            .select_related("job").get(pk=source_batch_id)
                        )
                        ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_STAGE, token=lease_token)
                    # 5. Write data, then record each room's outcome in the
                    # SAME transaction. Neither can commit independently.
                    messages_created, rooms_updated, deferred = _persist_round(
                        msg_results, member_results, rooms, runner, client,
                    )
                    if batch is not None:
                        metadata = dict(batch.processor_metadata or {})
                        progress = dict(metadata.get("room_progress", {}))
                        scanned_ids = {str(room.pk) for room in rooms_updated}
                        acquired_ids = {str(room.pk) for room in rooms}
                        recovered_by_room = {}
                        for message in messages_created:
                            room_id = str(message.room_id)
                            recovered_by_room[room_id] = recovered_by_room.get(room_id, 0) + 1
                        for room_id in round_ids:
                            scanned = room_id in scanned_ids
                            reason = "" if scanned else (
                                "credential_changed" if room_id in changed_credentials else
                                deferred.get(room_id, "room_not_updated") if room_id in acquired_ids else "room_locked_or_missing"
                            )
                            progress[room_id] = {
                                "state": (
                                    ItemProgressState.COMPLETED.value if scanned
                                    else ItemProgressState.DEFERRED.value
                                ),
                                "reason": reason,
                                "recovered_count": recovered_by_room.get(room_id, 0),
                            }
                        metadata["room_progress"] = progress
                        batch.processor_metadata = metadata
                        completed, deferred, _, _ = scan_rooms_batch_counts(batch)
                        batch.processed_count = completed
                        batch.deferred_count = deferred
                        summary = ProcessingSummary.model_validate(
                            empty_processing_summary() if batch.result_summary is None else batch.result_summary
                        )
                        summary.stages[TELEGRAM_SCAN_STAGE] = scan_rooms_stage_summary(batch)
                        batch.result_summary = summary.model_dump(mode="json")
                        batch.save(update_fields=[
                            "processor_metadata", "processed_count", "deferred_count", "result_summary", "updated_at",
                        ])
                        # Refresh Job totals only after a successful commit.
                        transaction.on_commit(lambda job_id=batch.job_id: refresh_processing_job(job_id))
                return messages_created, len(round_ids), len(rooms)

            try:
                for offset in range(0, len(room_ids), rooms_per_checkpoint):
                    # 1. Select the next round (six room IDs by default).
                    round_ids = room_ids[offset:offset + rooms_per_checkpoint]
                    # 2. Keep room locks, extracted data and Batch progress in
                    # one transaction. Success commits; an exception rolls
                    # back this round without affecting previous checkpoints.
                    outcome = process_room_round(round_ids)
                    if outcome is None:
                        break
                    messages_created, checkpoint_rooms, acquired_rooms = outcome
                    if not checkpoint_rooms:
                        continue
                    # 6. The transaction has committed and released its locks.
                    committed_ids.extend(message.pk for message in messages_created)
                    logger.info(
                        "Telegram room checkpoint committed. batch_id=%s rooms=%s messages=%s",
                        source_batch_id, checkpoint_rooms, len(messages_created),
                    )
                    rooms_since_sleep += acquired_rooms
                    if rooms_since_sleep >= 60 and offset + rooms_per_checkpoint < len(room_ids):
                        rooms_since_sleep %= 60
                        logger.info(
                            "Anti-FloodWait: [SCAN] '%s' sleeping for %ss after committed room checkpoints.",
                            auth_key.get("name", "NOT FOUND!"), SLEEP_AFTER_X_REQUESTS,
                        )
                        # Sleep after commit, without retaining database locks.
                        runner.run(asyncio.sleep(SLEEP_AFTER_X_REQUESTS))
            finally:
                # Close the shared client on success or failure. If scanning
                # already failed, cleanup must not replace the original error
                # needed by the task's FloodWait/authentication retry handler.
                if client is not None:
                    error_info = sys.exc_info()
                    try:
                        runner.run(client.__aexit__(*error_info))
                    except BaseException:
                        if error_info[0] is None:
                            raise
                        logger.exception("Telegram client cleanup failed after a room scan error")
    except AuthKeyError:
        TelegramAuth.objects.filter(pk=auth_key.get("pk")).update(is_valid=False)
        raise
    except IntegrityError:
        logger.exception("Room checkpoint persistence failed; previous checkpoints remain committed")
        raise
    return committed_ids


def _scan_comment_parent(
    batch_id, lease_token, parent_id, extract_one, *, auth_id, max_msgs,
    query_token=None, target_job_id=None, cancelled=None,
):
    """Lock and commit one explicit parent as an independent progress unit.

    Parameters identify the source batch/lease, parent, synchronous bridge to
    its client's event loop, credential, per-run limit and downstream target.
    Return a state, newly stored IDs and a reason. Persist comments, offset,
    handoffs and JSON progress together. A locked parent is terminally skipped
    for this batch; a future execution can select it again.
    Extraction/database errors before commit and stale leases roll back this
    parent's transaction; earlier parents remain committed. Coordination errors
    after commit retain the recorded progress for recovery.
    An optional threading Event rejects saves interrupted by loop cancellation
    or a soft timeout, including cancellation during synchronous persistence.
    """
    from app_scheduler.models import ProcessingBatch
    from app_scheduler.services.jobs import (
        ensure_stage_lease_locked,
        refresh_processing_job,
    )

    from app_telegram.services.jobs import (
        TELEGRAM_SCAN_COMMENTS_STAGE,
        scan_comments_batch_counts,
        scan_comments_stage_summary,
        validate_scan_comments_batch,
    )

    close_old_connections()
    try:
        with transaction.atomic():
            # Only the parent is exclusively locked. The room join must not
            # serialize independent parents or block another room traversal.
            parent = (
                MessageItem.objects.select_related("room")
                .select_for_update(skip_locked=True, of=("self",))
                .filter(pk=parent_id).first()
            )
            batch = None
            progress = {}
            if batch_id:
                batch = ProcessingBatch.objects.select_related("job").get(pk=batch_id)
                validate_scan_comments_batch(batch)
                ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_COMMENTS_STAGE, token=lease_token)
                if str(parent_id) not in batch.item_ids:
                    raise ValueError("Comment parent is not an explicit batch input")
                if batch.job.status == JobStatus.PAUSED:
                    return {
                        "state": TaskOutcome.PAUSED.value,
                        "message_ids": [],
                        "reason": TaskOutcome.PAUSED.value,
                    }
                # Read progress/offset only after acquiring the parent lock.
                progress = dict((batch.processor_metadata or {}).get("parent_progress", {}).get(str(parent_id), {}))
                if progress.get("state") in SCAN_COMMENT_PARENT_TERMINAL_STATES:
                    return {"state": progress["state"], "message_ids": [], "reason": progress.get("reason", "")}

            result = None
            fetched_count = 0
            original_access_hash = parent.room.access_hash if parent is not None else None
            state, reason = ItemProgressState.PENDING, ""
            if parent is None:
                if MessageItem.objects.filter(pk=parent_id).exists():
                    # Another transaction owns this parent. Skip it for this
                    # batch without waiting or spending retries on contention.
                    state, reason = ItemProgressState.SKIPPED, "parent_locked"
                else:
                    state, reason = ItemProgressState.SKIPPED, "parent_missing"
            elif (
                parent.is_reply or not parent.is_valid or not parent.room.is_channel
                or not parent.room.is_valid or parent.room.is_private
                or parent.room.is_deleted or not parent.room.following
            ):
                state, reason = ItemProgressState.SKIPPED, "parent_ineligible"
            elif str(parent.room.access_auth_id) != str(auth_id):
                reason = "credential_changed"
            else:
                remaining = max(max_msgs - int(progress.get("fetched_count", 0)), 0)
                if remaining:
                    if cancelled is not None and cancelled.is_set():
                        raise asyncio.CancelledError()
                    result = extract_one(room=parent.room, related_msg=parent, max_msgs=remaining)
                    fetched_count = result.fetched_count
                    reason = result.reason
                    if result.completed:
                        state = ItemProgressState.SKIPPED if reason else ItemProgressState.COMPLETED
                    elif int(progress.get("fetched_count", 0)) + result.fetched_count >= max_msgs:
                        state, reason = ItemProgressState.COMPLETED, "limit_reached"
                else:
                    state, reason = ItemProgressState.COMPLETED, "limit_reached"

            if batch_id:
                # Hold the batch lock only while merging progress. Validate
                # the lease again before any write: a cancelled or replaced
                # execution must roll back its entire parent operation.
                batch = (
                    ProcessingBatch.objects.select_for_update(no_key=True, of=("self",))
                    .select_related("job").get(pk=batch_id)
                )
                ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_COMMENTS_STAGE, token=lease_token)

            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()
            created = []
            wait_until = None
            extraction_error = result.error if result is not None else None
            if result is not None:
                # Access can change while Telegram is running, since only the
                # parent was locked. Briefly lock the room at persistence to
                # validate the binding and avoid overwriting a repaired access.
                current_room = RoomItem.objects.select_for_update(of=("self",)).get(pk=parent.room_id)
                if (
                    str(current_room.access_auth_id) != str(auth_id)
                    or current_room.access_hash != original_access_hash
                ):
                    result = None
                    state, reason = ItemProgressState.PENDING, "credential_changed"
            if result is not None:
                rebase_messages(result.messages, current_room.link)
                created = MessageItem.objects.bulk_create(result.messages, batch_size=settings.BULK_BATCH_SIZE)
                if result.parent is not None:
                    # Offset writes must stay inside this PostgreSQL unit;
                    # avoid model-save indexing signals during the transaction.
                    MessageItem.objects.filter(pk=result.parent.pk).update(
                        last_offset=result.parent.last_offset,
                        last_offset_update=result.parent.last_offset_update,
                        last_scan_at=result.parent.last_scan_at,
                        is_valid=result.parent.is_valid,
                    )
                if result.room is not None:
                    RoomItem.objects.filter(pk=result.room.pk).update(
                        is_private=result.room.is_private, is_valid=result.room.is_valid,
                        access_auth_id=result.room.access_auth_id, access_hash=result.room.access_hash,
                        last_update=result.room.last_update,
                    )
                if query_token and created:
                    bulk_add_query_relationships(created, query_token)
                _create_scan_message_handoffs(created, target_job_id=target_job_id, source_batch_id=batch_id)

            # A stale room binding discards its comments, but a FloodWait still
            # belongs to the client credential that actually made the request.
            if isinstance(extraction_error, FloodWaitError):
                logger.warning(
                    "Telegram FloodWait while scanning comments. auth_id=%s parent_id=%s telegram_msg_id=%s seconds=%s",
                    auth_id,
                    parent_id,
                    parent.msg_id,
                    extraction_error.seconds,
                )
                from django.db.models import Value
                from django.db.models.functions import Greatest
                wait_until = timezone.now() + timezone.timedelta(seconds=extraction_error.seconds)
                TelegramAuth.objects.filter(pk=auth_id).update(wait_until=Greatest("wait_until", Value(wait_until)))
            elif isinstance(extraction_error, AuthKeyError):
                TelegramAuth.objects.filter(pk=auth_id).update(is_valid=False)

            if batch is not None:
                metadata = dict(batch.processor_metadata or {})
                parents = dict(metadata.get("parent_progress", {}))
                # Merge the current JSON under the short batch lock, preserving
                # progress committed concurrently by other room threads.
                parents[str(parent_id)] = {
                    **progress,
                    "state": state.value,
                    "reason": reason,
                    "fetched_count": int(progress.get("fetched_count", 0)) + (result.fetched_count if result else 0),
                    "recovered_count": int(progress.get("recovered_count", 0)) + len(created),
                }
                if wait_until:
                    parents[str(parent_id)]["wait_until"] = wait_until.isoformat()
                metadata["parent_progress"] = parents
                batch.processor_metadata = metadata
                completed, skipped, _, _ = scan_comments_batch_counts(batch)
                batch.processed_count = completed
                batch.skipped_count = skipped
                summary = ProcessingSummary.model_validate(
                    empty_processing_summary() if batch.result_summary is None else batch.result_summary
                )
                summary.stages[TELEGRAM_SCAN_COMMENTS_STAGE] = scan_comments_stage_summary(batch)
                batch.result_summary = summary.model_dump(mode="json")
                batch.save(update_fields=["processor_metadata", "processed_count", "skipped_count", "result_summary", "updated_at"])
                transaction.on_commit(lambda job_id=batch.job_id: refresh_processing_job(job_id))
            # A soft timeout can arrive during synchronous DB writes, after the
            # async request already returned. Check again before atomic commits.
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()
            return {
                "state": state.value,
                "message_ids": [message.pk for message in created],
                "fetched_count": fetched_count,
                "reason": reason,
                "defer_client": isinstance(extraction_error, (FloodWaitError, AuthKeyError)),
            }
    finally:
        # Each sync_to_async worker has its own Django connection. No connection
        # may outlive the thread's parent transaction or leak into the loop.
        connections.close_all()


@shared_task(track_started=True, ignore_result=False)
def scan_comments(
    parent_ids, auth_key, max_msgs=2500, token=None,
    processing_handoff_target_job_id=None, source_batch_id=None, lease_token=None,
):
    """Scan only the supplied parent-message IDs using one Telegram client.

    Parameters supply immutable parent IDs, credentials, a per-parent/per-run
    limit, optional query relation and durable batch/lease/handoff target.
    Return the IDs of comments committed in this attempt. Persist each parent's
    comments, offset and batch progress independently. Room traversals may be
    scheduled concurrently, but parent requests sharing this credential are
    serialized and spaced by ``SLEEP_AFTER_X_REQUESTS`` or, when a parent
    returns no records, ``SLEEP_AFTER_EMPTY_COMMENT_PARENT``. Database, client
    and stale-lease errors propagate after sibling work settles.
    """
    parent_ids = list(dict.fromkeys(map(str, parent_ids)))
    rooms_by_parent = {
        str(parent_id): str(room_id)
        for parent_id, room_id in MessageItem.objects.filter(pk__in=parent_ids).values_list("id", "room_id")
    }
    grouped = {}
    for parent_id in parent_ids:
        grouped.setdefault(rooms_by_parent.get(parent_id, "missing"), []).append(parent_id)
    if not grouped:
        return []
    cancelled = Event()

    async def _run_extract_tasks():
        """Own the client loop and await every independent transaction thread.

        Return newly committed comment IDs; no ORM work runs on this loop.
        Callback cancellation propagates into atomic parent transactions.
        """
        stop = asyncio.Event()
        semaphore = asyncio.Semaphore(8)
        parent_request_lock = asyncio.Lock()
        remaining_parents = len(parent_ids)
        async with TelegramClient(
            StringSession(auth_key.get("session", "")),
            auth_key.get("api_id", 0), auth_key.get("api_hash", ""),
            flood_sleep_threshold=0,
        ) as client:
            await client.start()

            async def _extract_one(**kwargs):
                """Use parent/room/limit kwargs to return an unsaved extraction result.

                Run on this client's loop; persist nothing and propagate
                unexpected errors or cancellation to the transaction thread.
                """
                return await _extract_messages_result(client=client, **kwargs)

            # Construct AsyncToSync on this loop so transaction threads submit
            # Telethon calls back here. thread_sensitive=False gives each room
            # an independent connection and transaction while it holds a parent.
            extract_one = async_to_sync(_extract_one)

            async def _iterate_messages(ids):
                """Scan explicit IDs sequentially in one room and return stored IDs.

                Commit each parent via its thread bridge; pace all room groups
                sharing this credential and stop on pause/flood. Unexpected
                errors and lease rejection propagate after rollback.
                """
                nonlocal remaining_parents
                async with semaphore:
                    message_ids = []
                    for parent_id in ids:
                        if stop.is_set():
                            break
                        async with parent_request_lock:
                            if stop.is_set():
                                break
                            outcome = await sync_to_async(
                                _scan_comment_parent, thread_sensitive=False, executor=parent_workers,
                            )(
                                source_batch_id, lease_token, parent_id, extract_one,
                                auth_id=auth_key.get("pk"), max_msgs=max_msgs,
                                query_token=token, target_job_id=processing_handoff_target_job_id,
                                cancelled=cancelled,
                            )
                            message_ids.extend(outcome["message_ids"])
                            if outcome["state"] == TaskOutcome.PAUSED or outcome.get("defer_client"):
                                stop.set()
                            else:
                                remaining_parents -= 1
                                if remaining_parents:
                                    sleep_seconds = (
                                        SLEEP_AFTER_X_REQUESTS
                                        if outcome.get("fetched_count", 0)
                                        else SLEEP_AFTER_EMPTY_COMMENT_PARENT
                                    )
                                    logger.info(
                                        "Anti-FloodWait: [SCAN COMMENTS] '%s' sleeping for %ss after parent %s.",
                                        auth_key.get("name", "NOT FOUND!"),
                                        sleep_seconds,
                                        parent_id,
                                    )
                                    await asyncio.sleep(sleep_seconds)
                    return message_ids

            tasks = [asyncio.create_task(_iterate_messages(ids)) for ids in grouped.values()]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException:
                # A soft timeout or cancellation must settle the async bridge
                # before the client closes. Uncommitted parent units roll back.
                cancelled.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return list(iterchain.from_iterable(results))

    # Django has no async transactions in 4.2 or 6.x (as of 6.1). Run each
    # parent's synchronous ORM transaction in a thread so database waits do not
    # block Telethon's scan loop.
    # Shutdown joins transaction threads after cancellation.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="telegram-comments") as parent_workers:
        try:
            return asyncio.run(_run_extract_tasks())
        except BaseException:
            cancelled.set()
            raise
