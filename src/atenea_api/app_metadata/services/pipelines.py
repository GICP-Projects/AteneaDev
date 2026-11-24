from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from app_telegram.serializers import EmbedFilterMsgSerializer
from app_metadata.services.embeddings import DEFAULT_EMBEDDINGS_BLOCK_SIZE
from app_metadata.services.jobs import create_embeddings_job, create_sentiment_job
from app_telegram.serializers import ANY as TAG_ANY
from celery.app import shared_task


# ==========================================================
# ====              EMBEDDINGS PIPELINES               =====
# ==========================================================
# ====     Functions that wrap all the functionality   =====
# ====     needed to start pipelines related to the    =====
# ====                embeddings calculations          =====
# ==========================================================


# ==========================================================
# =====        EMBEDDINGS PIPELINES: CALCULATION       =====
# ==========================================================

@shared_task(track_started=True)
def embeddings_msgs_pipeline(
    instruct = "",
    slot = "default",
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    refresh = False,
    block_size = DEFAULT_EMBEDDINGS_BLOCK_SIZE,
    token = None,
    scheduler_context = None,
):
    """ MessageItem Embeddings calculation pipeline wrapper.
    It initializes the pipeline to calculate the embeddings for the messages that 
    fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the embeddings endpoint.

    Parameters
    ----------
    instruct: str, default=""
        Guide the behaviour of the model to follow specific instructions provided, 
        influencing tone, style, and content. Default instruct contains a standard 
        behaviour.

    slot: str, default="default"
        The message structure contains different slots (fields) to store the embeddings, 
        each one for a specific task. e.g: Categorisation (category), sentiment detection 
        (sentiment), or any other task (default).

    room: List[str], default=[]
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

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field
        
    lang: List[str], default=[]
        Language codes in ISO 639-1

    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    refresh: Boolean, default=False
        Refresh existing embeddings.

    block_size: int, default=DEFAULT_EMBEDDINGS_BLOCK_SIZE
        Number of items to be read by the database at once. Also, number of 
        embeddings which will be stored in the database at once (keep in mind 
        the RAM usage).

    token: string, default=None
        UUID.hex
    """
    job, _ = create_embeddings_job(
        filters={
            "instruct": instruct,
            "slot": slot,
            "room": room,
            "tags": tags,
            "tag_match": tag_match,
            "is_reply": is_reply,
            "createdat_min": createdat_min,
            "createdat_max": createdat_max,
            "stored_since": stored_since,
            "lang": lang,
            "type": type,
            "refresh": refresh,
        },
        request_token=token,
        lifecycle_policy=scheduler_context,
    )
    return str(job.id)


# ==========================================================
# =====               SENTIMENT PIPELINES              =====
# ==========================================================

@shared_task(track_started=True)
def sentiment_classification_msg_pipeline(
    token=None,
    room = [], 
    tags = [],
    tag_match = TAG_ANY,
    lang = [], 
    type = [],
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    block_size = 500000,
    scheduler_context = None,
):
    """ Sentiment Classification pipeline wrapper.
    It initializes the pipeline to classify the sentiment of the messages that
    fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the entities extraction endpoint.

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
        Interval start date for the created_at field

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field

    block_size: int, default=100000
        Number of items to be read by the database at once.

    Returns
    ----------
    items_count: int
        Number of items to be processed.
    """
    
    job, _ = create_sentiment_job(
        filters={
            "room": room,
            "tags": tags,
            "tag_match": tag_match,
            "lang": lang,
            "type": type,
            "is_reply": is_reply,
            "createdat_min": createdat_min,
            "createdat_max": createdat_max,
            "stored_since": stored_since,
        },
        request_token=token,
        lifecycle_policy=scheduler_context,
    )
    return str(job.id)
