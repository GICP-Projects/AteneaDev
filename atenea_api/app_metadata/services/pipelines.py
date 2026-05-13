from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from app_telegram.serializers import EmbedFilterMsgSerializer
from app_metadata.models import EmbeddingsItem
from app_metadata.services.embeddings import (
    DEFAULT_EMBEDDINGS_BLOCK_SIZE,
    run_embeddings,
)
from app_metadata.services.sentiments import run_sentiment
from app_telegram.models import MessageItem 
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
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    refresh = False,
    block_size = DEFAULT_EMBEDDINGS_BLOCK_SIZE,
    token = None
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
    and_filter_fields = {
        "created_at__gte": createdat_min,
        "created_at__lte": createdat_max,
        "stored_date__gte": stored_since,
        "media_type__in": type,
        "lang__in": lang,
        "is_reply": is_reply
    }

    # Get the field name of the selected embeddings slot 
    embedding_slot_field = "embeddings"
    clean_text = False
    if slot == EmbedFilterMsgSerializer.CAT:
        embedding_slot_field = "cat_embeddings"
        clean_text = True

    list_filter_fields = {
        "room__unique_name__iexact": {"values": [r.strip() for r in room], "OR": True}
    } if room else {}

    common_kwargs = {
        "token": token,
        "model_class_name": MessageItem.__name__,
        "model_class_app_label": MessageItem._meta.app_label,
        "model_class_text_field_name": "annotated_text",
        "model_class_related_field_name": embedding_slot_field,
        "list_filter_fields": list_filter_fields,
        "apply_distinct": False, # With the current filters there is no risk of duplicates.
        "instruct": instruct,
        "clean_text": clean_text,
        "block_size": block_size,
    }

    # With refresh=True, force recalculation for all the matching items.
    if refresh:
        run_embeddings.delay(
            and_filter_fields=and_filter_fields,
            **common_kwargs
        )
        return

    # Without refresh, retry only missing embeddings, failed syncs, or pending
    # syncs older than the configured timeout.
    pending_stale_before = timezone.now() - timedelta(
        seconds=settings.EMBEDDINGS_PENDING_TIMEOUT_SECONDS
    )
    retry_batches = [
        {**and_filter_fields, f"{embedding_slot_field}__isnull": True},
        {**and_filter_fields, f"{embedding_slot_field}__sync_status": EmbeddingsItem.STATUS_FAILED},
        {
            **and_filter_fields,
            f"{embedding_slot_field}__sync_status": EmbeddingsItem.STATUS_PENDING,
            f"{embedding_slot_field}__calculated_at__lt": pending_stale_before,
        },
    ]

    for retry_filter_fields in retry_batches:
        run_embeddings.delay(
            and_filter_fields=retry_filter_fields,
            **common_kwargs
        )


# ==========================================================
# =====               SENTIMENT PIPELINES              =====
# ==========================================================

@shared_task(track_started=True)
def sentiment_classification_msg_pipeline(
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
    
    and_filter_fields = {
        # Last update range filter
        "created_at__gte": createdat_min,
        "created_at__lte": createdat_max,
        "stored_date__gte": stored_since,
        "media_type__in": type,
        "lang__in": lang,
        "is_reply": is_reply
    }

    run_sentiment.delay(
        token,
        model_class_name = MessageItem.__name__,
        model_class_app_label = MessageItem._meta.app_label,
        model_class_text_field_name = "annotated_text",
        model_class_sentiment_field_name = "sentiment",
        model_class_sentiment_model_field_name = "sentiment_model",
        and_filter_fields=and_filter_fields,
        list_filter_fields = {
            "room__unique_name__iexact": {"values": [r.strip() for r in room], "OR": True},
            "room__tags__icontains": {"values": tags, "OR": tag_match == TAG_ANY}

        },
        apply_distinct = False, # With the current filters there is no risk of duplicates.
        block_size=block_size
    )
