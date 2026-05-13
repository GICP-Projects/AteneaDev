from celery.app import shared_task
from app_telegram.models import MessageItem
from app_entity.services.ner import run_ner


# ==========================================================
# ====               ENTITIES PIPELINES                =====
# ==========================================================
# ====     Functions that wrap all the functionality   =====
# ====   needed to start pipelines related to entities =====
# ====            extraction and management            =====
# ==========================================================


# ==========================================================
# ====         ENTITIES PIPELINES: EXTRACTION          =====
# ==========================================================

@shared_task(track_started=True)
def ner_extraction_msgs_pipeline(
    room = [], 
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 100000,
    token = None
):
    """ MessageItem NER extraction pipeline wrapper.
    It initializes the pipeline to extract the entities from the messages that 
    fits the filter.

    NOTE: Required wrapper to allow running this task with celery beat, in addition to 
    maintaining the entities extraction endpoint.

    Parameters
    ----------
    room: List[str], default=[]
        List of channel/group names from which messages are to be extracted. By 
        default, the list will be empty and this filter will be ignored.

    is_reply: bool, default=None
        Filter messages that are replies to another message.
        
    createdat_min: datetime.datetime, default=None
        Interval start date for the created_at field.

    createdat_max: datetime.datetime, default=None
        Interval start date for the created_at field.

    stored_since: datetime.datetime, default=None
        Interval start date for the stored_date field.
        
    lang: List[str], default=[]
        Language codes in ISO 639-1.

    type: List[str], default=[]
        Filter messages according to this list of media types. By default, the 
        list will be empty and this filter will be ignored.

    block_size: int, default=100000
        Number of items to be read by the database at once. Also, number of 
        entities which will be stored in the database at once (keep in mind 
        the RAM usage).

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

    run_ner.delay(
        token,
        MessageItem.__name__,
        MessageItem._meta.app_label,
        "annotated_text",
        and_filter_fields=and_filter_fields,
        list_filter_fields = {
            "room__unique_name__iexact": {"values": [r.strip() for r in room], "OR": True}
        },
        apply_distinct = False, # With the current filters there is no risk of duplicates.
        block_size=block_size
    )