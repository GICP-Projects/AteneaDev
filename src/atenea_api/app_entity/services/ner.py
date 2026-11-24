import time
import asyncio
import logging
import uuid
from contextlib import nullcontext
from itertools import chain
from django.apps import apps
from django.conf import settings
from django.db import transaction
from app_base.utils import call_service
from app_base.tagger import extract_tags
from app_entity.models import EntityItem, AnnotatedEntity
from app_entity.enums import EntityType
from django.contrib.contenttypes.models import ContentType
from app_base.signals import suspend_elasticsearch_sync


# Increment when custom-entity extraction behavior changes, including rules
# delegated to app_base.tagger.extract_tags.
NER_CUSTOM_ENTITIES_VERSION = "v1"


# Get an instance of a logger
logger = logging.getLogger(__name__)


async def call_ner_service(
    data, 
    max_by_request = None,
    max_parallel_requests = None,
    batch_size = 100, # Max number of requests by session
    timeout_seconds = None,
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

    filtered_data = {}
    skipped_empty_texts = 0
    for lang, items in data.items():
        valid_items = []
        for item in items:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                valid_items.append(item)
            else:
                skipped_empty_texts += 1
        if valid_items:
            filtered_data[lang] = valid_items

    if skipped_empty_texts:
        logger.info(
            "Skipped %s NER inputs with empty or invalid text before batching.",
            skipped_empty_texts,
        )

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
        data=filtered_data,
        service_config=service_config,
        endpoint_name="ner",
        payload_builder_func=payload_builder_func,
        payload_builder_kwargs={
            "allowed_types": [t[0] for t in EntityItem.SPACY_TYPES] 
        },
        payload_iterate_func=custom_payload_iterate_func,
        max_by_request=max_by_request,
        max_parallel_requests=max_parallel_requests,
        batch_size=batch_size,
        timeout_seconds=timeout_seconds,
    )

    # Flatten all grouped responses [....] (flatten)
    return chain.from_iterable(responses)


class NerExtractionError(RuntimeError):
    """The API-NER output cannot safely be persisted as a complete batch."""


def _ner_item_key(value):
    """Normalize UUID forms returned by Celery/JSON/API-NER for comparisons."""

    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return str(value)


def extract_ner_data(
    data, 
    batch_size = 100, 
    custom_entities = True,
    available_lang = None,
    strict = False,
    timeout_seconds = None,
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

    # ``extract_tags`` mutates the text value while removing custom entities.
    # Never mutate the caller input: durable tasks can retry the same payload.
    data = {
        lang: [dict(item) for item in items]
        for lang, items in data.items()
    }

    entities = []
    entity_to_ann = {}
    seen_entities = set()
    # Store all the pk-original_text for any item which contains 
    # (emails, urls, mentions and hashtags or emojis) entities
    pk_to_original = {} 
    all_pk_from_data = [
        item["pk"]
        for items in data.values()
        for item in items
    ]
    
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

                # If text have changed to be able later to re-calculate offsets
                if original_text != item["text"]:
                    pk_to_original[_ner_item_key(item["pk"])] = original_text
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
    expected_api_ids = {
        _ner_item_key(item["pk"])
        for lang, items in data.items()
        if lang in available_lang
        for item in items
        if isinstance(item.get("text"), str) and item["text"].strip()
    }

    for attempt in range(1, max_retries + 1):
        try:
            # Materialize the iterator inside the try/except so that errors 
            # during iteration (e.g. broken connection mid-stream) are also caught.
            results = list(asyncio.run(
                call_ner_service(
                    data={k:data[k] for k in available_lang if k in data},
                    batch_size=batch_size,
                    # A durable batch owns one global HTTP slot. One request at
                    # a time inside that slot keeps the configured bound real.
                    max_parallel_requests=1 if strict else None,
                    timeout_seconds=timeout_seconds,
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
                    f"{'Failing the durable batch.' if strict else 'Returning only custom entities extracted so far.'}"
                )
                if strict:
                    raise NerExtractionError("NER service unavailable") from exc

    if strict:
        result_by_id = {}
        for result in results:
            if not isinstance(result, dict):
                raise NerExtractionError("NER service returned an invalid result item")
            if "id" not in result or not isinstance(result.get("entities"), list):
                raise NerExtractionError("NER service returned an incomplete result item")
            result_id = _ner_item_key(result["id"])
            if result_id in result_by_id:
                raise NerExtractionError(f"NER service returned duplicate id {result_id}")
            result_by_id[result_id] = result
        if set(result_by_id) != expected_api_ids:
            raise NerExtractionError(
                "NER service returned a partial response "
                f"(expected={len(expected_api_ids)}, received={len(result_by_id)})"
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
        original_text = pk_to_original.get(_ner_item_key(data["id"]), None)
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


def store_ner_results(
    db_data_tuple,
    model_class_name,
    model_class_app_label,
    *,
    replace_message_ids=None,
    atomic=True,
):
    """
    Handles database operations for entities and its relations.

    Retry ownership belongs to ``app_entity.tasks.store_ner_batch``. Keeping
    this function undecorated makes it usable inside that stage's transaction.
    
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
        Returns a dict compatible with `app_telegram.services.index.items_to_index`
        to allow the connection of these tasks in a Celery Pipeline.
    """
    # Extract data from previous task
    entities, entity_to_ann, all_model_items_pks = db_data_tuple

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)
    context = transaction.atomic() if atomic else nullcontext()
    with context:
        replacement_ids = list(replace_message_ids or [])
        requested_ids = list(chain(
            all_model_items_pks,
            replacement_ids,
            (
                annotation["item_pk"]
                for annotations in entity_to_ann.values()
                for annotation in annotations
            ),
        ))
        existing_ids = set(
            ModelClass._default_manager
            .filter(pk__in=requested_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not existing_ids:
            return {}
        # Generic annotations have no database FK to their source. Skip items
        # deleted since extraction instead of creating orphan annotations.
        # JSON IDs are strings; database PKs are UUID objects when compared here.
        entity_to_ann = {
            unique_ent: [
                annotation for annotation in annotations
                if uuid.UUID(str(annotation["item_pk"])) in existing_ids
            ]
            for unique_ent, annotations in entity_to_ann.items()
        }
        all_model_items_pks = [
            item_id for item_id in all_model_items_pks
            if uuid.UUID(str(item_id)) in existing_ids
        ]

        # Create/update entities (Note: already existing entities will not have the correct pk)
        init_time = time.time()
        normalized_entities = {
            entity["unique_ent"]: {
                **entity,
                "type": EntityType.from_name(entity["type"]),
            }
            for entity in entities
        }
        sorted_unique_entities = sorted(normalized_entities)
        logger.info(
            f"{len(normalized_entities)} unique EntityItem items were normalized "
            f"(normalize/sort: {(time.time() - init_time):.3f} secs)."
        )

        init_time = time.time()
        entity_instances = [
            EntityItem(**normalized_entities[unique_ent])
            for unique_ent in sorted_unique_entities
        ]
        logger.info(
            f"{len(entity_instances)} EntityItem instances were prepared "
            f"(object preparation: {(time.time() - init_time):.3f} secs)."
        )

        init_time = time.time()
        affected_entities = EntityItem.objects.bulk_create(
            entity_instances,
            update_conflicts=True,
            unique_fields=['unique_ent'],
            update_fields=["name"],
            batch_size=settings.BULK_BATCH_SIZE
        )
        logger.info(
            f"{len(affected_entities)} EntityItem items were created (bulk_create: {(time.time() - init_time):.3f} secs)."
        )
        del entity_instances

        # Retrieve real items from previous entities (updated entities didn't have their real pk)
        init_time = time.time()
        affected_entities = list(
            EntityItem.objects.filter(
                unique_ent__in=sorted_unique_entities
            ).order_by("unique_ent")
        )
        logger.info(
            f"{len(affected_entities)} EntityItem items were retrieved by unique_ent filter "
            f"(filter: {(time.time() - init_time):.3f} secs)."
        )

        content_type = ContentType.objects.get_for_model(ModelClass)

        # A reprocess replaces NER's current annotation set. Callers that do
        # not specify IDs keep the additive/idempotent primitive behaviour.
        if replace_message_ids is not None:
            init_time = time.time()
            delete_queryset = AnnotatedEntity.objects.filter(
                content_type=content_type,
                object_id__in=replacement_ids,
            )
            with suspend_elasticsearch_sync():
                deleted_count, deleted_by_type = delete_queryset.delete()
            logger.info(
                f"{deleted_count} AnnotatedEntity items were deleted "
                f"(bulk_delete: {(time.time() - init_time):.3f} secs; "
                f"breakdown: {deleted_by_type})."
            )

        # Create relationship between ModelClass and Entity, by using
        # AnnotatedEntity.  A deterministic lock/upsert order sharply reduces
        # the deadlock pattern caused by concurrent EntityItem upserts.
        init_time = time.time()
        annotations_to_create = []
        for ent_item in affected_entities:
            for ann_ent in sorted(
                entity_to_ann.get(ent_item.unique_ent, []),
                key=lambda ann: (
                    str(ann["item_pk"]),
                    ann.get("start_offset") if ann.get("start_offset") is not None else -1,
                    ann.get("end_offset") if ann.get("end_offset") is not None else -1,
                ),
            ):
                annotations_to_create.append(
                    AnnotatedEntity(
                        entity_id=ent_item.pk,
                        start_offset=ann_ent["start_offset"],
                        end_offset=ann_ent["end_offset"],
                        object_id=ann_ent["item_pk"],
                        content_type=content_type,
                    )
                )
        logger.info(
            f"{len(annotations_to_create)} AnnotatedEntity instances were prepared "
            f"(object preparation: {(time.time() - init_time):.3f} secs)."
        )

        init_time = time.time()
        created_annotations = AnnotatedEntity.objects.bulk_create(
            annotations_to_create,
            batch_size=settings.BULK_BATCH_SIZE,
            ignore_conflicts=True
        )
        len_ann_items = len(created_annotations)
        logger.info(
            f"{len_ann_items} AnnotatedEntity items were created (bulk_create: {(time.time() - init_time):.3f} secs)."
        )

        if all_model_items_pks:
            return {ModelClass.__name__: {"and_filter_fields": {"pk__in": all_model_items_pks}}}
        return {}
