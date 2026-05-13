import time
import asyncio
import random
import logging
from itertools import chain
from django.apps import apps
from django.conf import settings
from django.db import OperationalError
from celery.app import shared_task
from celery import group
from app_base.utils import call_service
from app_base.tagger import extract_tags
from app_base.api import create_advance_filter, bulk_add_query_relationships_with_pks
from app_entity.models import EntityItem, AnnotatedEntity
from django.contrib.contenttypes.models import ContentType


# Get an instance of a logger
logger = logging.getLogger(__name__)


async def call_ner_service(
    data, 
    max_by_request = None,
    max_parallel_requests = None,
    batch_size = 100 # Max number of requests by session
):
    """
    Calls the NER service endpoint with the provided data, handling batching and session 
    management to optimize resource usage and reduce timeouts.

    Parameters
    ----------
    data: dict
        Dict with all the text-pk dicts grouped by its lang code. 
        ```
        {    
            "es": [
                {
                    "text": ...
                    "pk": (Optional) Primary key related to the text to be appended 
                    to the returned data.
                }
                ...
            ]
            "en": [...]
            "<lang_code>": [...]
        }
        ```

    max_by_request: int, default=None
        Maximum number of items to be included in a single request to the NER service. 
        Ensures that each request stays within acceptable size limits to avoid overloading 
        the service. If not provided, the value from `settings.NER_SERVICE` is used.

    batch_size: int, optional (default=100)
        Maximum number of requests per session to manage resource usage and reduce 
        timeouts. Helps prevent exceeding memory and connection limits, ensuring smoother 
        execution.
            
    Returns
    ----------
    ret: List[dict]
    ```
        {
            "id": (Optional) Only if the pk was passed as an argument in each dict.
            "entities": [
                {
                    "name": ...
                    "type": ...
                    "start_offset": ...
                    "end_offset": ...
                }
            ]
        }
    ```
    """

    def custom_payload_iterate_func(
        data, 
        max_by_request, 
        builder, 
        builder_kwargs
    ):
        payloads = []
        for lang, data_text in data.items():
            for idx in range(0, len(data_text), max_by_request):
                payloads.append(
                    builder(
                        data_text[idx: idx + max_by_request], 
                        lang=lang,
                        **builder_kwargs
                    )
                )
        return payloads

    service_config = settings.NER_SERVICE
    max_by_request = max_by_request or service_config.max_items_by_request
    max_parallel_requests = max_parallel_requests or service_config.max_parallel_requests

    payload_builder_func=(
        lambda chunk, lang, allowed_types: 
        {
            "lang": lang,
            "types": allowed_types,
            "data": [
                {"text": item["text"], "id": item.get("pk", None)} 
                for item in chunk
            ]
        }
    )

    # Run NER service
    responses = await call_service(
        data=data,
        service_config=service_config,
        endpoint_name="ner",
        payload_builder_func=payload_builder_func,
        payload_builder_kwargs={
            "allowed_types": [t[0] for t in EntityItem.SPACY_TYPES] 
        },
        payload_iterate_func=custom_payload_iterate_func,
        max_by_request=max_by_request,
        max_parallel_requests=max_parallel_requests,
        batch_size=batch_size
    )

    # Flatten all grouped responses [....] (flatten)
    return chain.from_iterable(responses)


# ======================================================
# =====            ENTITIES CELERY TASKS           =====
# ======================================================
# =====  Celery tasks that have specific functio-  =====
# =====    -nality to extract, process and store   ===== 
# =====                entities.                   =====
# ======================================================


# ======================================================
# =====        CELERY TASKS: NER EXTRACTION        =====     
# ======================================================

@shared_task(track_started=True)
def run_ner(
    token, 
    model_class_name, 
    model_class_app_label, 
    model_class_text_field_name,
    and_filter_fields = {}, 
    list_filter_fields = {},
    apply_distinct = False,
    block_size = 10000
):
    """ NER Extraction Orchestrator.
    Run the Name entity recognition in celery and split the task in N subtasks to
    split the computational cost and improve performance.

    Parameters
    ----------
    token: string
        Query token to add the relation with the item

    model_class_name: str
        Contains the class name of the Django Model with the embeddings field and
        whose extracted entities are going to be related. 

    model_class_app_name: str
        Contains the django app name which contains the model class. 

    model_class_text_field_name: str
        Contains the field name of the text field in the Django Model which is going to
        be used to extract entities.
    
    and_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of app_base.api.create_advance_filter.
        ```
        {
            "<field>__<desired_filter>": value/s,
            ...
        }
        ```

    list_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of app_base.api.create_advance_filter.
        ```
        {
            "<field>__<desired_filter>": { "values": [....], "OR": True/False },
            ...
        }
        ```

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

    block_size: int, default=10000
        Number of items by celery task.
    """

    tasks = []

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)

    items_dicts = (
        ModelClass.objects.filter(
            create_advance_filter(
                and_filter_fields=and_filter_fields,
                list_filter_fields=list_filter_fields
            )
        ).exclude(lang__exact="")
        # Ordering by an unique value allow the unevaluated Queryset to be sliced 
        # (without repeating items inconsistently in each slice).
        # "Slicing an unevaluated QuerySet usually returns another unevaluated QuerySet"
        .order_by("pk", "lang")
    )

    if apply_distinct:
        items_dicts = items_dicts.distinct("pk")

    # Rest of the queryset pipe
    items_dicts = items_dicts.values("pk", "lang", model_class_text_field_name)
    
    total_items = items_dicts.count()
    items_pks = []
    logger.debug(f"Total items: {total_items}")
    start_time = time.time()
    
    text_to_annotate = {}
    items_in_block = 0
    
    # We use iterator() to prevent OFFSET/LIMIT database degradation on huge tables
    for item in items_dicts.iterator(chunk_size=block_size):
        text_to_annotate.setdefault(item["lang"], []).append(
            {
                "pk": item["pk"], 
                "text": item[model_class_text_field_name]
            }
        )
        items_pks.append(item["pk"])
        items_in_block += 1
        
        # When block is full, build the task and reset chunk containers
        if items_in_block >= block_size:
            tasks.append(
                ner_extraction.si(
                    text_to_annotate,
                ) | ner_db_store.s(
                    model_class_name,
                    model_class_app_label,
                )
            )
            end_time = time.time()
            logger.debug(f"items block. Time: {end_time - start_time}secs")
            start_time = time.time()
            
            text_to_annotate = {}
            items_in_block = 0

    # Don't forget the last subset
    if text_to_annotate:
        tasks.append(
            ner_extraction.si(
                text_to_annotate,
            ) | ner_db_store.s(
                model_class_name,
                model_class_app_label,
            )
        )

    # Clear all items
    items_dicts = []
    
    job = group(tasks)
    job.apply_async()

    # Bind the Query to all the msg items involved
    if False:#token:
        bulk_add_query_relationships_with_pks(items_pks, ContentType.objects.get_for_model(ModelClass), token)
    
    
@shared_task(track_started=True)
def ner_extraction(
    data, 
    batch_size = 100, 
    custom_entities = True,
    available_lang = None
):
    """ Extract entities from text.

    Each entity will have start/end offset with the position the word appeared in
    the original text (without the annotations)

    Parameters
    ----------
    data: dict
        Dict with all the text-pk dicts grouped by its lang code. 
        ```
        {    
            "es": [
                {
                    "pk": (Optional) Primary key related to the text to be appended 
                    to the returned data.
                    "text": ...
                }
                ...
            ]
            "en": [...]
            "lang_code": [...]
        }
        ```
        The `text` and `lang` fields are required params.

    custom_entities: bool, default=True
        Extract custom entities using the `app_base.tagger` module. This entity
        types are: 'EMAIL', 'URL', 'MENTION', 'HASHTAGS' and 'EMOJI'. 
        PROS: This entities will be removed from the text sended to api-ner (so, 
        is recommended because spacy models have problems with urls or emails f.e)
        CONS: api-ner entities will probably need a re-calculation of their offsets
        because the original text differs from that used by ner.
    """

    available_lang = available_lang or settings.NER_SERVICE.languages

    entities = []
    entity_to_ann = {}
    seen_entities = set()
    # Store all the pk-original_text for any item which contains 
    # (emails, urls, mentions and hashtags or emojis) entities
    pk_to_original = {} 
    all_pk_from_data = []
    
    # First extract custom entities ('EMAIL', 'URL', 'MENTION', 'HASHTAGS' and 'EMOJI')
    if custom_entities:
        for _, items in data.items():
            for item in items:
                original_text = item["text"]
                item["text"], ann_entities = extract_tags(
                    original_text,
                    date=False,
                    time=False,
                    number=False,
                )

                # Store all the pks to send them to the following task in the pipeline
                all_pk_from_data.append(item["pk"])

                # If text have changed to be able later to re-calculate offsets
                if original_text != item["text"]:
                    pk_to_original[item["pk"].hex] = original_text
                for tag_ent_type, tag_entities in ann_entities.items():
                    for entity in tag_entities:
                        unique_ent = EntityItem.to_unique(entity["match"], tag_ent_type)
                        if len(unique_ent) <= 2048:
                            if unique_ent not in seen_entities:
                                seen_entities.add(unique_ent)
                                entities.append(
                                    {
                                        "name": entity["match"],
                                        "unique_ent": unique_ent,
                                        "type": tag_ent_type
                                    }
                                )
                            # An entity can appear multiple times
                            entity_to_ann.setdefault(unique_ent, []).append({
                                "item_pk": item["pk"], 
                                "start_offset": entity["start_offset"],
                                "end_offset": entity["end_offset"]
                            })
                        else:
                            logger.info(f"Entity {repr(entity['match'])} ignored, too long ({len(entity['match'])} characters).")
                        

    # Extract entities using API-NER microservice. If custom_entities=True the text 
    # will have removed entities (emails, urls, mentions and hashtags or emojis). 
    # NOTE: This entities are extracted and removed to help api-ner with the detection. 
    # Problem: entities' offset will be wrong because they will be calculated using 
    # the text without some data. Probably they will need to be recalculated.
    max_retries = 3
    retry_delay = 15  # seconds between retries
    results = []

    for attempt in range(1, max_retries + 1):
        try:
            # Materialize the iterator inside the try/except so that errors 
            # during iteration (e.g. broken connection mid-stream) are also caught.
            results = list(asyncio.run(
                call_ner_service(
                    data={k:data[k] for k in available_lang if k in data},
                    batch_size=batch_size
                )
            ))
            break
        except Exception as exc:
            if attempt < max_retries:
                logger.warning(
                    f"NER service request failed (attempt {attempt}/{max_retries}): "
                    f"{exc.__class__.__name__}: {exc}. Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    f"NER service unavailable after {max_retries} attempts: "
                    f"{exc.__class__.__name__}: {exc}. "
                    f"Returning only custom entities extracted so far."
                )

    #items_to_update = []
    ner_processed = 0
    ner_skipped = 0
    for data in results:
        # call_ner_service can return empty dicts, None or unexpected types from failed requests
        # TODO: If Pydantic is used to validate microservice responses, this manual
        # check could be replaced by a model validation (e.g. NerResponse.model_validate(data))
        # which would handle type/field validation in a more generic and maintainable way.
        if not isinstance(data, dict) or "id" not in data or "entities" not in data:
            ner_skipped += 1
            continue

        ner_processed += 1
        # Retrieve original text from this Message (None = The text hasn't changed)
        # In case the original text exist, api-ner entities must recalculate their offsets
        original_text = pk_to_original.get(data["id"], None)
        # To handle offset re-calculation when an entity appear multiple times
        last_end_offset = 0 
        for entity in data["entities"]:
            unique_ent = EntityItem.to_unique(entity["name"], entity["type"])
            
            # Check if this entity's offsets need to be recalculated
            start = entity["start_offset"]
            end = entity["end_offset"]
            if original_text:
                start = original_text.find(entity["name"], last_end_offset)
                end = start + len(entity["name"])
                last_end_offset = end # keeping track 

            # In case it was imposible to find the entity
            if start != -1:
                if unique_ent not in seen_entities:
                    seen_entities.add(unique_ent)
                    entities.append(
                        {
                            "name": entity["name"],
                            "unique_ent": unique_ent,
                            "type": entity["type"]
                        }
                    )
                # An entity can appear multiple times
                entity_to_ann.setdefault(unique_ent, []).append({
                    "item_pk": data["id"], 
                    "start_offset": start,
                    "end_offset": end
                })
            else:
                # NOTE: Sometimes API-NER detects entities where in the original text there 
                # were emojis in between:
                # original: the🇺🇸 United States on February 3rd, 2023.
                # ner text: the United States on February 3rd, 2023.
                # The entity 'the United States' can't be found in the original.
                logger.info(
                    f"Entity from NER service not found in text {repr(entity['name'])}. MessageItem id: {data['id']}"
                )    

    if results:
        logger.info(
            f"NER service results: {ner_processed} items processed successfully, "
            f"{ner_skipped} items skipped (invalid/failed responses)."
        )

    # Start the database operations
    return entities, entity_to_ann, all_pk_from_data


@shared_task(track_started=True,  max_retries=10)
def ner_db_store(
    db_data_tuple,
    model_class_name,
    model_class_app_label, 
):
    """
    Handles database operations for entities and its relations.

    This function is responsible for inserting or updating and it is designed to 
    handle OperationalErrors, such as deadlocks or timeouts, by retrying the operation. 
    The retry mechanism utilizes Celery's built-in capabilities, automatically
    reattempting the task in case of such errors.

    The function will retry the operation up to a maximum of five times (configurable),
    with a delay between retries, to manage temporary database issues effectively.

    https://stackoverflow.com/a/67767199
    
    Parameters
    ----------
    db_data_tuple: tuple
        All data from the previous task, this tuple should contains three elements:
            - List of dicts to create EntityItems
            - Dict of dicts to create AnnotatedEntities: 
            {unique_ent: {"item_pk": .. "start_offset": .. "end_offset":..}}
            - A list of ModelClass primary keys to be returned by this task.

    model_class_name: str
        Contains the class name of the Django Model with the annotated_text field and
        whose extracted entities are going to be related. 

    model_class_app_name: str
        Contains the django app name which contains the model class.

    Returns
    ---------- 
    msgs_dict: dict
        Returns a dict compatible with `app_telegram.services.telegram.items_to_index`
        to allow the connection of these tasks in a Celery Pipeline.
    """
    # Extract data from previous task
    entities, entity_to_ann, all_model_items_pks = db_data_tuple

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)
    try:
        # Create/update entities (Note: already existing entities will not have the correct pk)
        init_time = time.time()
        affected_entities = EntityItem.objects.bulk_create(
            [EntityItem(**ent) for ent in entities],
            update_conflicts=True,
            unique_fields=['unique_ent'],
            update_fields=[field.name for field in EntityItem._meta.fields if not field.unique],
            batch_size=settings.BULK_BATCH_SIZE
        )
        logger.info(
            f"{len(affected_entities)} EntityItem items were created (bulk_create: {(time.time() - init_time):.3f} secs)."
        )

        # Retrieve real items from previous entities (updated entities didn't have their real pk)
        affected_entities = EntityItem.objects.filter(
            unique_ent__in=[aff_ent.unique_ent for aff_ent in affected_entities]  
        )

        # Create relationship between ModelClass and Entity, by using AnnotatedEntity
        # Ignore duplicate exceptions
        init_time = time.time()
        len_ann_items = len(
            AnnotatedEntity.objects.bulk_create(
                [
                    AnnotatedEntity(
                        **{
                            "entity_id": ent_item.pk,
                            "start_offset": ann_ent["start_offset"],
                            "end_offset": ann_ent["end_offset"],
                            # Generic Foreignkey relation 
                            "object_id": ann_ent["item_pk"],
                            "content_type": ContentType.objects.get_for_model(ModelClass),
                        }
                    ) 
                    for ent_item in affected_entities
                    for ann_ent in entity_to_ann[ent_item.unique_ent]
                ],
                batch_size=settings.BULK_BATCH_SIZE,
                ignore_conflicts=True
            )
        )
        logger.info(
            f"{len_ann_items} AnnotatedEntity items were created (bulk_create: {(time.time() - init_time):.3f} secs)."
        )

        if all_model_items_pks:
            return {ModelClass.__name__: {"and_filter_fields": {"pk__in": all_model_items_pks}}}
        return {}
        
    except OperationalError as exc:
        # Generate a random countdown between 20 and 60 seconds 
        countdown = random.randint(20, 60)
        logger.error(f"Error in ner_database task, retrying in {countdown}secs. Traceback: {exc}")
        # Retry the task in case of a database error
        raise ner_db_store.retry(exc=exc, countdown=countdown)
