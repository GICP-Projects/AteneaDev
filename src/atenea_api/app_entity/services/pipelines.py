from celery.app import shared_task
from app_telegram.models import MessageItem
from app_telegram.serializers import ANY as TAG_ANY
from app_entity.services.jobs import create_ner_backfill_job


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
    tags = [],
    tag_match = TAG_ANY,
    is_reply = None,
    createdat_min = None,
    createdat_max = None,
    stored_since = None,
    lang = [], 
    type = [],
    block_size = 100000,
    token = None,
    refresh = False,
    reindex = False,
    scheduler_context = None,
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

    tags: List[str], default=[]
        To filter the channels/groups according to this list of tags.

    tag_match: str, default="any"
        Determines if items should match all given tags ('all') or any of them ('any').

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
    
    # Scheduled/manual invocations use the same bounded NER-only Job as the
    # Telegram REST endpoint.
    job, _ = create_ner_backfill_job(
        filters={
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
        reindex=reindex,
        lifecycle_policy=scheduler_context,
    )
    return str(job.id)
