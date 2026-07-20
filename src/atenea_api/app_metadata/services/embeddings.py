import gc
import hashlib
import time
import logging
import numpy as np
import asyncio
from django.conf import settings
from django.apps import apps
from django.db import transaction
from django.db.models import F
from django.core.exceptions import FieldDoesNotExist
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from celery import group
from celery.app import shared_task
from app_base.api import get_items_pks, generic_celery_task_orchestrator, bulk_add_query_relationships_with_pks
from app_base.tagger import extract_tags
from app_metadata.models import EmbeddingsItem, CategoryItem, SimilarityCategory
from app_metadata.vector_store import (
    get_collection_name,
    retrieve_vectors_grouped,
    upsert_points,
)
from openai import AsyncOpenAI


# Get an instance of a logger
logger = logging.getLogger(__name__)

# Minimum size needed to calculate text embeddings
MIN_TEXT_LENGTH_TO_EMBED = 6
DEFAULT_EMBEDDINGS_BLOCK_SIZE = 100000


def _validate_openai_base_url(base_url):
    base_url = (base_url or "").rstrip("/")
    if not base_url.endswith("/v1"):
        raise ValueError(
            "OPENAI_EMBEDDINGS_BASE_URL must include the API versioning path "
            "(e.g. '/v1'), for example 'http://localhost:28333/v1'."
        )
    return base_url


def clean_text_to_embed(text):
    """ Clean the text to improve some embeddings capabilities. eg: zero-shot 
    categories classification.

    - Removes any URL 
    - Removes any mention
    - Removes any email
    - Removes any emoji
    """
    text, _ = extract_tags(
        text,
        hashtag=False,
        date=False,
        time=False,
        number=False,
        emoji=False
    )
    return text


def cosine_similarity_many_to_many(vecs1, vecs2, normalize=True):
    """
    Calculate the cosine similarity between two sets of vectors.

    Parameters
    ----------
    vecs1: np.ndarray 
        np.ndarray of shape (n_vectors, vector_dim) for the first set.
    vecs2: np.ndarray 
        np.ndarray of shape (m_vectors, vector_dim) for the second set.
    normalize : bool, default=True
        If True, normalize the similarity scores for each vector in vecs1 across all
        vectors in vecs2 to a range between 0 and 1. 
    Returns
    -------
    - similarities: np.ndarray 
        np.ndarray of shape (n_vectors, m_vectors) containing cosine similarity scores
        between each pair of vectors from vecs1 and vecs2.
    """
    # Normalize the vectors in each set to have unit norm
    vecs1_normalized = vecs1 / np.linalg.norm(vecs1, axis=1, keepdims=True)
    vecs2_normalized = vecs2 / np.linalg.norm(vecs2, axis=1, keepdims=True)

    # Compute the cosine similarity matrix
    similarities = np.dot(vecs1_normalized, vecs2_normalized.T)

    # Normalize the similarity scores for each vector in vecs1 if normalize is True
    if normalize:
        # Use min-max normalization to scale the scores between 0 and 1
        min_vals = np.min(similarities, axis=1, keepdims=True)
        max_vals = np.max(similarities, axis=1, keepdims=True)
        # Avoid division by zero in case all similarities for a vector are the same
        denom = np.where(max_vals - min_vals == 0, 1, max_vals - min_vals)
        similarities = (similarities - min_vals) / denom

    return similarities


async def call_embed_service(
    data, 
    instruct,
    max_by_request = None,
    max_parallel_requests = None,
):
    """
    Calls an OpenAI-compatible embeddings endpoint using the official OpenAI client.

    Parameters
    ----------
    data: List[dict]
        List of dicts with texts. 
        ```
        [
            {
                "text_to_embed": ...
                ...
            },
            ...
        ]
        ```

    instruct: str
        The instruction added to each text for the embeddings calculation.

    max_by_request: int
        Maximum number of items per embeddings request.

    max_parallel_requests: int
        Maximum number of requests executed in parallel.
            
    Returns
    ----------
    model_name: str
        Name of the model used to calculate the embeddings.

    version: str
        Version of the model used to calculate the embeddings. OpenAI-compatible
        embeddings APIs do not expose a separate version, so it defaults to "N/A".

    embeddings: List[List[float]]
        List of vectors with the embeddings of each text.
        ```
        [
            [0.34344, 0.234234, ...],
            [0.54654, 0.876434, ...],
            ... 
        ]
        ``` 
    """

    service_config = settings.OPENAI_EMBEDDINGS
    max_by_request = max_by_request or service_config.max_items_by_request
    max_parallel_requests = max_parallel_requests or service_config.max_parallel_requests
    client = AsyncOpenAI(
        api_key=service_config.api_key or "none",
        base_url=_validate_openai_base_url(service_config.base_url),
        timeout=service_config.timeout,
    )

    async def _embed_chunk(chunk):
        inputs = [
            (f"Instruct: {instruct}\nQuery: " if instruct else "") + item["text_to_embed"]
            for item in chunk
        ]

        request_kwargs = {
            "model": service_config.model,
            "input": inputs,
            "encoding_format": "float",
        }
        if service_config.dimensions is not None:
            request_kwargs["dimensions"] = service_config.dimensions

        try:
            response = await client.embeddings.create(**request_kwargs)
            return {
                "model": response.model or service_config.model,
                "embeddings": [item.embedding for item in response.data],
            }
        except Exception as exc:
            logger.exception("Embeddings request failed for %s items: %s", len(chunk), exc)
            return {
                "model": service_config.model,
                "embeddings": [None] * len(chunk),
            }

    chunks = [
        data[i:i + max_by_request]
        for i in range(0, len(data), max_by_request)
    ]

    model_name = service_config.model
    version = "N/A"
    total_embeddings = []
    try:
        total_batches = (len(chunks) + max_parallel_requests - 1) // max_parallel_requests
        for batch_index, batch_start in enumerate(range(0, len(chunks), max_parallel_requests), start=1):
            batch_chunks = chunks[batch_start:batch_start + max_parallel_requests]
            batch_size = sum(len(chunk) for chunk in batch_chunks)
            logger.info(
                "Sending embeddings batch %s/%s to provider: %s request(s), %s text(s).",
                batch_index,
                total_batches,
                len(batch_chunks),
                batch_size,
            )
            batch_time = time.time()
            responses = await asyncio.gather(*[_embed_chunk(chunk) for chunk in batch_chunks])
            successful_embeddings = sum(
                1 for result in responses for embedding in result["embeddings"] if embedding is not None
            )
            failed_embeddings = batch_size - successful_embeddings
            logger.info(
                "Embeddings batch %s/%s finished in %.3f secs: %s ok, %s failed.",
                batch_index,
                total_batches,
                time.time() - batch_time,
                successful_embeddings,
                failed_embeddings,
            )
            for result in responses:
                if result.get("model"):
                    model_name = result["model"]
                total_embeddings.extend(result["embeddings"])
    finally:
        await client.close()

    return model_name, version, total_embeddings


def _model_has_field(model_class, field_name):
    try:
        model_class._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def _get_embedding_source_data(model_class, model_class_text_field_name, model_class_related_field_name, items_pks):
    payload_text_field_name = (
        "text" if _model_has_field(model_class, "text") else model_class_text_field_name
    )
    values_fields = ["pk"]
    values_kwargs = {
        "text_to_embed": F(model_class_text_field_name),
        "payload_text": F(payload_text_field_name),
        "existing_embedding_pk": F(f"{model_class_related_field_name}_id"),
    }

    optional_fields = ("lang", "media_type", "is_reply")
    for field_name in optional_fields:
        if _model_has_field(model_class, field_name):
            values_fields.append(field_name)

    if _model_has_field(model_class, "room"):
        values_kwargs["room_unique_name"] = F("room__unique_name")

    return list(
        model_class.objects.filter(pk__in=items_pks)
        .select_for_update(skip_locked=True)
        .values(*values_fields, **values_kwargs)
    )


def _build_embedding_payload(item, embed_item, model_class_name, model_class_related_field_name):
    payload = {
        "embedding_id": str(embed_item.pk),
        "source_pk": str(item["pk"]),
        "source_model_class": model_class_name,
        "slot": model_class_related_field_name,
        "text": item.get("payload_text") or item["text_to_embed"],
        "model": embed_item.model,
        "version": embed_item.version,
        "instruct": embed_item.instruct or "",
        "calculated_at": (
            embed_item.calculated_at.isoformat() if embed_item.calculated_at else None
        ),
    }

    for field_name in ("lang", "media_type", "room_unique_name", "is_reply"):
        value = item.get(field_name)
        if value is not None:
            payload[field_name] = value

    return payload


# ======================================================
# =====     CELERY TASKS: EMBEDDINGS MANAGERS      =====     
# ======================================================

@shared_task(track_started=True)
def run_embeddings(
    token, 
    model_class_name, 
    model_class_app_label, 
    model_class_text_field_name,
    model_class_related_field_name,
    and_filter_fields = {},
    list_filter_fields = {},
    exclude_filter_fields = None,
    apply_distinct = False,
    instruct = "",
    clean_text=False,
    block_size = DEFAULT_EMBEDDINGS_BLOCK_SIZE,
    chunk_size = None,
    thread_count = None,
    minimum_text_length = MIN_TEXT_LENGTH_TO_EMBED
):
    """ Embedding Calculation Orchestrator.
    Run the embeddings calculation in celery and split the task in N subtasks to
    split the computational cost and improve performance.

    NOTE: All tasks are executed one by one not in a group (the number of parallel
    tasks will dependen on the embed queue size).

    Each text (after being cleaned) must be longer than 'MIN_TEXT_LENGTH_TO_EMBED'
    characters in order to calculate its embeddings.

    Parameters
    ----------
    token: string
        Query token to add the relation with the item

    model_class_name: str
        Contains the class name of the Django Model with the text field that is 
        going to be used to calculate the embeddings and and to which it will be 
        related.

    model_class_app_name: str
        Contains the django app name which contains the model class. 

    model_class_text_field_name: str
        Contains the field name of the text field in the Django Model which is going to
        be used to calculate the embeddings.

    model_class_related_field_name: str
        Contains the field name of the embeddings related field in the Django Model 

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

    exclude_filter_fields: dict, default=None
        Dict with the Django ORM filters used to exclude items from the processing
        queryset before splitting the work into celery subtasks.

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
        
    instruct: str, default=""
        The instruction added to each text for the embeddings calculation.

    clean_text: bool, default=False
        Clean text by removing emails, urls and mentions.

    block_size: int, default=500000
        Number of items to be read by the database at once. Also, number of items
        whose embeddings will be stored in the database at once (keep in mind the
        RAM usage).
    
    chunk_size: int, default=None
        Maximum number of items to be included in a single embeddings request. If
        not provided, the value from `settings.OPENAI_EMBEDDINGS` is used.

    thread_count: int, default=None
        Max number of parallel requests at the same time. If not provided, the value
        from `settings.OPENAI_EMBEDDINGS` is used.
        
    minimum_text_length: int, default=MIN_TEXT_LENGTH_TO_EMBED
        Minimum size needed for each text to calculate text embeddings
    """

    service_config = settings.OPENAI_EMBEDDINGS
    chunk_size = chunk_size or service_config.max_items_by_request
    thread_count = thread_count or service_config.max_parallel_requests

    generic_celery_task_orchestrator(
        model_class_name=model_class_name,
        model_class_app_label=model_class_app_label,
        celery_task=calculate_embeddings,
        celery_task_kwargs={
            "model_class_text_field_name": model_class_text_field_name,
            "model_class_related_field_name": model_class_related_field_name,
            "clean_text": clean_text,
            "minimum_text_length": minimum_text_length,
            "instruct": instruct,
            "chunk_size": chunk_size, 
            "thread_count": thread_count
        },
        and_filter_fields=and_filter_fields,
        list_filter_fields=list_filter_fields,
        exclude_filter_fields={
            f"{model_class_text_field_name}__exact": "",
            **(exclude_filter_fields or {}),
        },
        apply_distinct=apply_distinct,
        block_size=block_size,
        token=token
    )


@shared_task(track_started=True)
def run_categorizer(
    token, 
    model_class_name, 
    model_class_app_label, 
    model_class_embeddings_field_name,
    and_filter_fields = {},
    list_filter_fields = {},
    apply_distinct = False,
    block_size = 500000,
    chunk_size = 50000
):
    """ Categorization with embeddings Orchestrator.
    Run the embeddings categorizer in celery and split the task in N subtasks, each
    one with `block_size` items to split the computational cost and improve performance.
    The tasks use the `calculate_categories` function.

    NOTE: All tasks are chained and excuted sequentially not in parallel.

    In order to avoid race conditions, each item will be blocked (by using transactions)
    during the cosine similarity with the categories's embeddings and the bulking
    of SimilarityCategory items into the DB.

    Parameters
    ----------
    token: string
        Query token to add the relation with the item

    model_class_name: str
        Contains the class name of the Django Model with the text field that is 
        going to be used to calculate the embeddings and and to which it will be 
        related.

    model_class_app_name: str
        Contains the django app name which contains the model class. 

    model_class_related_field_name: str
        Contains the field name of the embeddings related field in the Django Model 

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
               
    block_size: int, default=500000
        Number of items to be read by the database at once. Also, number of 
        SimilarityCategory items that will be stored in the database at once 
        (keep in mind the RAM usage).
    
    chunk_size: int, default=50000
        Number of embeddings to be read by the database at once. Also, number
        of embeddings which cosine similarity will be calcultated at once with
        with each category embedding. Check the documentation of `calculate_categories`
        for this parameter more details about RAM usage. 
    """
    # Get model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)

    items_pks, total_items = get_items_pks(
        ModelClass,
        and_filter_fields,
        list_filter_fields,
        apply_distinct
    )

    categories_data = list(
        CategoryItem.objects.filter(
            embeddings__point_id__isnull=False,
            embeddings__collection_name__isnull=False,
            embeddings__sync_status=EmbeddingsItem.STATUS_SYNCED,
        ).values(
            "pk",
            embedding_point_id=F("embeddings__point_id"),
            embedding_collection_name=F("embeddings__collection_name"),
        )
    )

    if categories_data:
        tasks = []
        logger.debug(f"Total items: {total_items}")
        # Split the work load in tasks with an specific block_size 
        start_time = time.time()
        for i in range(0, total_items, block_size):
            tasks.append(
                calculate_categories.si(
                    items_pks=list(items_pks[i:i+block_size]),
                    category_data = categories_data,
                    model_class_name=model_class_name,
                    model_class_app_label=model_class_app_label,
                    model_class_embeddings_field_name=model_class_embeddings_field_name,
                    chunk_size=chunk_size
                )
            )

            end_time = time.time()
            logger.debug(f"items[{i}:{i+block_size}]. Time: {end_time - start_time}secs")

            # Reset start time
            start_time = time.time()

        if tasks:
            logger.info(f"The embeddings calculation has been split into {len(tasks)} subtasks.")
            group(tasks).apply_async()
    else:
        logger.info(
            f"Currently there is no '{CategoryItem.__name__}' items to be able to '{calculate_categories.__name__}' the "
            f" {total_items} {model_class_name} items."
        )

    # Finally, bind the Query to all the items involved
    if token:
        bulk_add_query_relationships_with_pks(items_pks, ContentType.objects.get_for_model(ModelClass), token)


# ======================================================
# =====    CELERY TASKS: EMBEDDINGS PR0CESSORS     =====     
# ======================================================

@shared_task(track_started=True)
def calculate_embeddings(
    items_pks, 
    model_class_name,
    model_class_app_label,
    model_class_text_field_name,
    model_class_related_field_name,
    clean_text=False,
    minimum_text_length = MIN_TEXT_LENGTH_TO_EMBED,
    instruct = "", 
    chunk_size = None, 
    thread_count = None
):
    """ Extracts all the texts, retrieve their embeddings and create list of 
    dictionaries with the necessary information to store them (not in this function) 
    in the database and relate them with each item. 

    Parameters
    ----------
    data: List[dict]
        List of primary keys for calculating their embeddings (of the 
        `model_class_text_field_name` field)

    instruct: str
        The instruction added to each text for the embeddings calculation.

    chunk_size: int, default=None
        Maximum number of items to be included in a single embeddings request. If
        not provided, the value from `settings.OPENAI_EMBEDDINGS` is used.

    thread_count: int, default=None
        Max number of parallel requests at the same time. If not provided, the value
        from `settings.OPENAI_EMBEDDINGS` is used.
    """

    service_config = settings.OPENAI_EMBEDDINGS
    chunk_size = chunk_size or service_config.max_items_by_request
    thread_count = thread_count or service_config.max_parallel_requests

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)

    # Start a transaction to avoid race conditions:
    # f.e: run this task more than once at the same with same items (wasting time 
    # and RAM usage).
    collection_name = get_collection_name(model_class_name, model_class_related_field_name)
    prepare_start = time.time()
    with transaction.atomic():
        data = _get_embedding_source_data(
            ModelClass,
            model_class_text_field_name,
            model_class_related_field_name,
            items_pks,
        )

        prepared_data = []
        for item in data:
            text = item["text_to_embed"] or ""
            if clean_text:
                text = clean_text_to_embed(text)
            if len(text) < minimum_text_length:
                continue
            prepared_data.append({**item, "text_to_embed": text})
        data = prepared_data

        if not data:
            logger.info("No valid %s items found to calculate embeddings.", ModelClass.__name__)
            return

        logger.info(
            "Prepared %s %s items for embeddings in %.3f secs.",
            len(data),
            ModelClass.__name__,
            time.time() - prepare_start,
        )

        existing_embeddings = EmbeddingsItem.objects.in_bulk(
            [
                item["existing_embedding_pk"]
                for item in data
                if item["existing_embedding_pk"]
            ]
        )
        existing_embedding_ids = list(existing_embeddings.keys())
        embeds_to_create = []
        items_related = []
        records_to_process = []
        calculation_time = timezone.now()
        for item in data:
            existing_embedding_pk = item["existing_embedding_pk"]
            embed_item = existing_embeddings.get(existing_embedding_pk)
            is_new_embed = embed_item is None
            if is_new_embed:
                embed_item = EmbeddingsItem()
                embed_item.model = service_config.model
                embed_item.version = "N/A"
                embed_item.instruct = instruct
                embed_item.collection_name = collection_name
                embed_item.vector_dim = None
                embed_item.text_hash = None
                embed_item.sync_status = EmbeddingsItem.STATUS_PENDING
                embed_item.sync_error = None
                embed_item.synced_at = None
                embed_item.calculated_at = calculation_time
                embed_item.point_id = embed_item.point_id or str(embed_item.pk)
                embeds_to_create.append(embed_item)
                items_related.append(
                    ModelClass(
                        **{
                            "pk": item["pk"],
                            f"{model_class_related_field_name}_id": embed_item.pk,
                        }
                    )
                )

            records_to_process.append((item, embed_item))

        ini_time = time.time()
        logger.info(
            "Creating %s pending EmbeddingsItem rows for %s.",
            len(embeds_to_create),
            ModelClass.__name__,
        )
        num_created = len(
            EmbeddingsItem.objects.bulk_create(
                embeds_to_create,
                batch_size=settings.BULK_BATCH_SIZE,
            )
        )
        create_time = time.time() - ini_time

        init_time = time.time()
        num_pending_updates = 0
        if existing_embedding_ids:
            logger.info(
                "Updating %s existing EmbeddingsItem rows to pending for %s.",
                len(existing_embedding_ids),
                ModelClass.__name__,
            )
            num_pending_updates = EmbeddingsItem.objects.filter(
                pk__in=existing_embedding_ids
            ).update(
                instruct=instruct,
                collection_name=collection_name,
                sync_status=EmbeddingsItem.STATUS_PENDING,
                sync_error=None,
                synced_at=None,
                calculated_at=calculation_time,
            )
        pending_update_time = time.time() - init_time

        # Update the items by adding the relationships
        ini_time = time.time()
        num_updates = 0
        if items_related:
            logger.info(
                "Relating %s %s rows with their pending EmbeddingsItem rows.",
                len(items_related),
                ModelClass.__name__,
            )
            num_updates = ModelClass.objects.bulk_update(
                items_related, 
                [model_class_related_field_name],
                batch_size=settings.BULK_BATCH_SIZE
            )
        updt_time = time.time() - ini_time

    embed_start = time.time()
    model_name, version, embeddings = asyncio.run(
        call_embed_service(
            data=data,
            instruct=instruct,
            max_by_request=chunk_size,
            max_parallel_requests=thread_count
        )
    )
    logger.info(
        "Embeddings provider returned %s vectors for %s %s items in %.3f secs.",
        len(embeddings),
        len(data),
        ModelClass.__name__,
        time.time() - embed_start,
    )

    points_to_upsert = []
    embeds_with_vectors = []
    failed_provider_ids = []
    for (item, embed_item), embedding_vector in zip(records_to_process, embeddings):
        if embedding_vector:
            embed_item.model = model_name
            embed_item.version = str(version)
            embed_item.instruct = instruct
            embed_item.collection_name = collection_name
            embed_item.point_id = embed_item.point_id or str(embed_item.pk)
            embed_item.vector_dim = len(embedding_vector)
            embed_item.text_hash = hashlib.sha256(
                item["text_to_embed"].encode("utf-8")
            ).hexdigest()
            embed_item.sync_status = EmbeddingsItem.STATUS_PENDING
            embed_item.sync_error = None
            embed_item.synced_at = None
            embed_item.calculated_at = calculation_time
            embeds_with_vectors.append(embed_item)
            points_to_upsert.append(
                {
                    "id": embed_item.point_id,
                    "vector": embedding_vector,
                    "payload": _build_embedding_payload(
                        item,
                        embed_item,
                        model_class_name,
                        model_class_related_field_name,
                    ),
                }
            )
        else:
            failed_provider_ids.append(embed_item.pk)

    if embeds_with_vectors:
        update_vectors_start = time.time()
        logger.info(
            "Updating vector metadata for %s EmbeddingsItem rows.",
            len(embeds_with_vectors),
        )
        EmbeddingsItem.objects.bulk_update(
            embeds_with_vectors,
            [
                "model",
                "version",
                "instruct",
                "collection_name",
                "point_id",
                "vector_dim",
                "text_hash",
                "sync_status",
                "sync_error",
                "synced_at",
                "calculated_at",
            ],
            batch_size=settings.BULK_BATCH_SIZE,
        )
        logger.info(
            "Updated metadata for %s EmbeddingsItem rows in %.3f secs.",
            len(embeds_with_vectors),
            time.time() - update_vectors_start,
        )

    if failed_provider_ids:
        EmbeddingsItem.objects.filter(pk__in=failed_provider_ids).update(
            sync_status=EmbeddingsItem.STATUS_FAILED,
            sync_error="Embeddings provider request failed.",
        )
        logger.warning(
            "%s EmbeddingsItem rows failed before Qdrant upsert because the embeddings provider returned no vector.",
            len(failed_provider_ids),
        )

    if not points_to_upsert:
        logger.info("No embeddings were generated for %s items.", ModelClass.__name__)
        return

    upsert_start = time.time()
    synced_ids = [point["payload"]["embedding_id"] for point in points_to_upsert]
    try:
        upsert_points(
            collection_name=collection_name,
            points=points_to_upsert,
            vector_size=len(points_to_upsert[0]["vector"]),
        )
        EmbeddingsItem.objects.filter(pk__in=synced_ids).update(
            sync_status=EmbeddingsItem.STATUS_SYNCED,
            sync_error=None,
            synced_at=timezone.now(),
        )
    except Exception as exc:
        EmbeddingsItem.objects.filter(pk__in=synced_ids).update(
            sync_status=EmbeddingsItem.STATUS_FAILED,
            sync_error=str(exc),
        )
        raise

    logger.info(
        "%s EmbeddingsItem items were created (bulk_create: %.3f secs), %s existing "
        "EmbeddingsItem rows were marked pending (update: %.3f secs), %s %s items were related (bulk_update: "
        "%.3f secs) and %s points were synced to Qdrant (sync: %.3f secs).",
        num_created,
        create_time,
        num_pending_updates,
        pending_update_time,
        num_updates,
        ModelClass.__name__,
        updt_time,
        len(points_to_upsert),
        time.time() - upsert_start,
    )


@shared_task(track_started=True)
def calculate_categories(
    items_pks, 
    category_data,     
    model_class_name,
    model_class_app_label, 
    model_class_embeddings_field_name,
    chunk_size = 50000,
    compensator_i = 2,
    top_k = 5,
    normalize = True
):
    """
    Calculate similarity categories for given items based on their embeddings.
    Processes items in chunks to optimize memory usage and applies optional 
    normalization and compensation to similarity scores.

    Parameters
    ----------
    items_pks: list[uuid.UUID]
        The items from this primary keys *MUST* have an EmbeddingsItem object in 
        its `model_class_embeddings_field_name` field.

    category_data: list[dict]
        Category references with the Qdrant point metadata needed to retrieve each
        category vector from the vector store.
        ```
        [
                {
                    "pk": ...,
                    "embedding_point_id": ...,
                    "embedding_collection_name": ...,
                },
                ...
        ]
        ``` 

    model_class_name: str
        Contains the class name of the Django Model with the text field that is 
        going to be used to calculate the embeddings and and to which it will be 
        related.

    model_class_app_name: str
        Contains the django app name which contains the model class. 

    model_class_related_field_name: str
        Contains the field name of the embeddings related field in the Django Model 

    chunk_size: int, default=50000
        Number of embeddings references to resolve from Qdrant at once. This also
        defines the size of each cosine-similarity batch in memory.

    top_k: int, default=5
        Create only the top 5 SimilarityCategory for each item ignoring the rest.
        if top_k is 0 or less all similarities are going to be used.

    compensator_i: int, default=2
        An index that triggers the application of a compensation (or penalization) factor
        to the similarity scores. Scores up to and including this index are not modified,
        while scores beyond this index are progressively penalized by a decreasing factor,
        starting from 1.0 down to a minimum of 0.05, with a decrement of 0.05 for each step.
        A value of -1 (default) means no compensation is applied.
        eg:
        ```
            compensator_i = 2
            >>> multiplier = [1, 1, 1, 0.95, 0.9, 0.85, ...]
        ```
    normalize: boolean, default=True
        Normalises the similarity of the categories for each element between [0-1].
    """

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)
    ContentTypeOfModel = ContentType.objects.get_for_model(ModelClass)

    category_vectors = retrieve_vectors_grouped(
        [
            (item["embedding_collection_name"], item["embedding_point_id"])
            for item in category_data
        ]
    )
    resolved_categories = [
        {
            "pk": item["pk"],
            "vector": category_vectors.get(
                (item["embedding_collection_name"], item["embedding_point_id"])
            ),
        }
        for item in category_data
    ]
    resolved_categories = [
        item for item in resolved_categories
        if item["vector"] is not None
    ]
    if not resolved_categories:
        logger.warning("No category embeddings are available in Qdrant.")
        return

    np_categories = np.asarray([item["vector"] for item in resolved_categories])
    # Upgrades the most similar cats with a multiplier if powerup is set to True.
    # (and gradually penalises the rest)
    category_counter = len(resolved_categories)
    multiplier = [1 for _ in range(category_counter)]
    if compensator_i >= 0:
        multiplier = (
            [1 for _ in range(compensator_i + 1)] + 
            [max(0.05, 1.0 - 0.05*i) for i in range(category_counter - compensator_i)]
        )
    # Check top_k is deactivated
    if top_k <= 0:
        top_k = category_counter # get all of them

    # Start a transaction to avoid race conditions:
    # f.e: run this task more than once at the same with same items (wasting time 
    # and RAM usage).
    with transaction.atomic():
        # Message embeddings to calculate their categories
        items_data = (
            # This items must have embeddings in the field model_class_embeddings_field_name
            ModelClass.objects.filter(
                pk__in=items_pks, 
                # Ensure Non-nullable: This transforms the join into an inner join
                # avoiding a `psycopg2.errors.FeatureNotSupported: FOR UPDATE cannot be applied to the nullable side of an outer join`
                **{f"{model_class_embeddings_field_name}__isnull": False}
            )
            .select_for_update(skip_locked=True)
            .select_related(model_class_embeddings_field_name)
            .values(
                "pk",
                embedding_collection_name=F(
                    f"{model_class_embeddings_field_name}__collection_name"
                ),
                embedding_point_id=F(f"{model_class_embeddings_field_name}__point_id"),
            )
        )

        # Initialize required data
        similaritites_items = []
        chunk_pks = []
        chunk_refs = []
        total_items = items_data.count()
        chunk_counter = 0
        total_counter = 0
        for item in items_data.iterator(chunk_size=chunk_size):
            # Collect data per chunk
            chunk_pks.append(item["pk"])
            chunk_refs.append(
                (item["embedding_collection_name"], item["embedding_point_id"])
            )
            chunk_counter += 1
            total_counter += 1
            if chunk_counter >= chunk_size or total_counter == total_items:
                vectors_by_ref = retrieve_vectors_grouped(chunk_refs)
                resolved_chunk_pks = []
                chunk_embeds = []
                for item_pk, item_ref in zip(chunk_pks, chunk_refs):
                    vector = vectors_by_ref.get(item_ref)
                    if vector is None:
                        continue
                    resolved_chunk_pks.append(item_pk)
                    chunk_embeds.append(np.array(vector))

                if not chunk_embeds:
                    chunk_pks = []
                    chunk_refs = []
                    chunk_counter = 0
                    continue

                # Calculate similarities
                similarities = cosine_similarity_many_to_many(
                    np.asarray(chunk_embeds),
                    np_categories,
                    normalize=normalize
                )
                # Reset chunk
                chunk_embeds = []

                # Store similarities into the items 
                for item_i, item_sims in enumerate(similarities):
                    item_pk = resolved_chunk_pks[item_i]
                    for cat_i, power_up in zip(item_sims.argsort()[::-1][:top_k], multiplier):
                        similaritites_items.append(
                            SimilarityCategory(
                                **{
                                    "category_id": resolved_categories[cat_i]["pk"],
                                    "similarity": item_sims[cat_i]*power_up,
                                    # Generic Foreignkey relation 
                                    "object_id": item_pk,
                                    "content_type": ContentTypeOfModel,
                                }
                            )
                        )
                # Reset chunk
                similarities = []
                chunk_pks = []
                chunk_refs = []
                chunk_counter = 0
                gc.collect()

        # Remove previous similarities
        SimilarityCategory.objects.filter(object_id__in=items_pks).delete()

        # Create new ones
        init_time = time.time()
        total_creates = len(SimilarityCategory.objects.bulk_create(
            similaritites_items, 
            ignore_conflicts=True
        ))
        update_time = time.time() - init_time

        logger.info(
            f"{total_creates} SimilarityCategory items were created from categorising "
            f"{len(items_pks)} {model_class_name} items with {category_counter} categories " 
            f"(bulk_create: {update_time:.3f} secs)."
        )
