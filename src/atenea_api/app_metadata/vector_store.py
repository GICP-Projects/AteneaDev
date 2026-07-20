import logging
import time
from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


logger = logging.getLogger(__name__)


def _build_point_structs(models, points):
    """Build Qdrant PointStruct objects from internal point dictionaries.

    Parameters
    ----------
    models:
        Qdrant HTTP models module (`qdrant_client.http.models`).

    points: list[dict]
        Point dictionaries with the following structure:
        ```
        {
            "id": ...,
            "vector": [...],
            "payload": {...},
        }
        ```

    Returns
    -------
    list
        A list of `models.PointStruct` objects ready for Qdrant upsert.
    """
    return [
        models.PointStruct(
            id=point["id"],
            vector=point["vector"],
            payload=point["payload"],
        )
        for point in points
    ]


def _upsert_batch_with_retry(
    client,
    models,
    collection_name,
    points,
    max_retries,
    retry_delay_seconds,
):
    """Upsert a single batch to Qdrant with retries and adaptive splitting.

    Parameters
    ----------
    client:
        An initialized `QdrantClient`.

    models:
        Qdrant HTTP models module (`qdrant_client.http.models`).

    collection_name: str
        Target Qdrant collection name.

    points: list[dict]
        Batch of points to upsert.

    max_retries: int
        Number of retries before considering the batch failed.

    retry_delay_seconds: int
        Base delay between retries. Delay is multiplied by the attempt number.

    Raises
    ------
    Exception
        Re-raises the last Qdrant error if the batch cannot be upserted and
        can no longer be split (batch size equals 1).

    Notes
    -----
    - If all retry attempts fail and batch size is greater than 1, the function
      splits the batch into two halves and retries each half recursively.
    - This fallback helps recover from transient transport errors and payload
      size/time pressure in Qdrant.
    """
    points = list(points)
    max_attempts = max_retries + 1
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            client.upsert(
                collection_name=collection_name,
                points=_build_point_structs(models, points),
                wait=True,
            )
            return
        except Exception as exc:
            last_error = exc
            root_error = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
            logger.warning(
                "Qdrant upsert failed for collection '%s' with %s point(s), "
                "attempt %s/%s. error=%r root=%r",
                collection_name,
                len(points),
                attempt,
                max_attempts,
                exc,
                root_error,
            )
            if attempt < max_attempts:
                time.sleep(retry_delay_seconds * attempt)

    if len(points) <= 1:
        raise last_error

    split_index = len(points) // 2
    left_points = points[:split_index]
    right_points = points[split_index:]
    logger.warning(
        "Splitting failed Qdrant upsert batch for collection '%s' into %s + %s point(s).",
        collection_name,
        len(left_points),
        len(right_points),
    )
    _upsert_batch_with_retry(
        client=client,
        models=models,
        collection_name=collection_name,
        points=left_points,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    _upsert_batch_with_retry(
        client=client,
        models=models,
        collection_name=collection_name,
        points=right_points,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )


def _import_qdrant():
    """Import Qdrant client dependencies lazily.

    Returns
    -------
    tuple
        `(QdrantClient, models)` modules from `qdrant_client`.

    Raises
    ------
    django.core.exceptions.ImproperlyConfigured
        If `qdrant-client` is not installed in the current environment.
    """
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models
    except ImportError as exc:
        raise ImproperlyConfigured(
            "qdrant-client is required to use the embeddings vector store."
        ) from exc
    return QdrantClient, models


def get_qdrant_client():
    """Create a configured Qdrant client from Django settings.

    Returns
    -------
    qdrant_client.QdrantClient
        Client configured with URL, API key and timeout from `settings.QDRANT`.
    """
    QdrantClient, _ = _import_qdrant()
    config = settings.QDRANT
    return QdrantClient(
        url=config.url,
        api_key=config.api_key,
        timeout=config.timeout,
        prefer_grpc=False,
    )


def get_collection_name(model_class_name, related_field_name):
    """Resolve the target Qdrant collection for a model/slot pair.

    Parameters
    ----------
    model_class_name: str
        Django model class name being processed.

    related_field_name: str
        Embeddings slot field name (`embeddings`, `cat_embeddings`, ...).

    Returns
    -------
    str
        Collection name from `settings.QDRANT.collections`.
    """
    if model_class_name == "CategoryItem" or related_field_name == "cat_embeddings":
        return settings.QDRANT.collections["categorization"]
    return settings.QDRANT.collections["message_search"]


def ensure_collection(collection_name, vector_size):
    """Create a Qdrant collection if it does not exist.

    Parameters
    ----------
    collection_name: str
        Target collection name.

    vector_size: int
        Expected vector size for the collection.
    """
    client = get_qdrant_client()
    _, models = _import_qdrant()
    if client.collection_exists(collection_name=collection_name):
        return

    qdrant_config = settings.QDRANT
    hnsw_config_kwargs = {}
    if qdrant_config.hnsw_m is not None:
        hnsw_config_kwargs["m"] = qdrant_config.hnsw_m
    if qdrant_config.hnsw_ef_construct is not None:
        hnsw_config_kwargs["ef_construct"] = qdrant_config.hnsw_ef_construct
    if qdrant_config.hnsw_full_scan_threshold is not None:
        hnsw_config_kwargs["full_scan_threshold"] = qdrant_config.hnsw_full_scan_threshold

    create_collection_kwargs = {}
    if hnsw_config_kwargs:
        create_collection_kwargs["hnsw_config"] = models.HnswConfigDiff(
            **hnsw_config_kwargs
        )

    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=vector_size,
            distance=models.Distance.DOT,
        ),
        **create_collection_kwargs,
    )
    logger.info("Created Qdrant collection '%s'.", collection_name)


def upsert_points(collection_name, points, vector_size):
    """Upsert points into Qdrant in batches with retry and fallback splitting.

    Parameters
    ----------
    collection_name: str
        Target collection name.

    points: Iterable[dict]
        Iterable of point dictionaries:
        ```
        {
            "id": ...,
            "vector": [...],
            "payload": {...},
        }
        ```

    vector_size: int
        Vector size used to ensure/create the destination collection.
    """
    points = list(points)
    if not points:
        return

    client = get_qdrant_client()
    _, models = _import_qdrant()
    batch_size = settings.QDRANT.upsert_batch_size
    max_retries = settings.QDRANT.upsert_max_retries
    retry_delay_seconds = settings.QDRANT.upsert_retry_delay_seconds
    ensure_collection(collection_name, vector_size)
    total_batches = (len(points) + batch_size - 1) // batch_size
    for batch_index, batch_start in enumerate(range(0, len(points), batch_size), start=1):
        batch = points[batch_start:batch_start + batch_size]
        batch_time = time.time()
        logger.info(
            "Sending Qdrant upsert batch %s/%s to '%s': %s point(s).",
            batch_index,
            total_batches,
            collection_name,
            len(batch),
        )
        _upsert_batch_with_retry(
            client=client,
            models=models,
            collection_name=collection_name,
            points=batch,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )
        logger.info(
            "Qdrant upsert batch %s/%s to '%s' finished in %.3f secs.",
            batch_index,
            total_batches,
            collection_name,
            time.time() - batch_time,
        )


def delete_points(collection_name, point_ids):
    """Delete points from Qdrant by id.

    Parameters
    ----------
    collection_name: str
        Target collection name.

    point_ids: Iterable[str]
        Point ids to delete.
    """
    point_ids = [point_id for point_id in point_ids if point_id]
    if not point_ids:
        return

    client = get_qdrant_client()
    _, models = _import_qdrant()
    client.delete(
        collection_name=collection_name,
        points_selector=models.PointIdsList(points=point_ids),
        wait=True,
    )


def _extract_vector(record):
    """Extract vector data from a Qdrant retrieve/search record.

    Parameters
    ----------
    record:
        Qdrant record object that may store vectors in either `vector` or
        `vectors` attribute.

    Returns
    -------
    list[float] | None
        Extracted vector or `None` when not present.
    """
    vector = getattr(record, "vector", None)
    if vector is None:
        vector = getattr(record, "vectors", None)
        if isinstance(vector, dict):
            vector = next(iter(vector.values()), None)
    return vector


def retrieve_vectors(collection_name, point_ids):
    """Retrieve vectors and payloads from Qdrant by point id.

    Parameters
    ----------
    collection_name: str
        Target collection name.

    point_ids: Iterable[str]
        Point ids to retrieve.

    Returns
    -------
    dict[str, dict]
        Mapping by point id:
        ```
        {
            "<point_id>": {
                "id": "<point_id>",
                "vector": [...],
                "payload": {...},
            },
            ...
        }
        ```
    """
    point_ids = [point_id for point_id in point_ids if point_id]
    if not point_ids:
        return {}

    client = get_qdrant_client()
    records = client.retrieve(
        collection_name=collection_name,
        ids=point_ids,
        with_payload=True,
        with_vectors=True,
    )
    return {
        str(record.id): {
            "id": str(record.id),
            "vector": _extract_vector(record),
            "payload": getattr(record, "payload", {}) or {},
        }
        for record in records
    }


def retrieve_vectors_grouped(references):
    """Retrieve vectors grouped by `(collection, point_id)` references.

    Parameters
    ----------
    references: Iterable[tuple[str, str]]
        Sequence of `(collection_name, point_id)` references.

    Returns
    -------
    dict[tuple[str, str], list[float]]
        Mapping from `(collection_name, point_id)` to vector values.
    """
    grouped_ids = defaultdict(list)
    for collection_name, point_id in references:
        if collection_name and point_id:
            grouped_ids[collection_name].append(point_id)

    resolved = {}
    for collection_name, point_ids in grouped_ids.items():
        for point_id, data in retrieve_vectors(collection_name, point_ids).items():
            resolved[(collection_name, point_id)] = data["vector"]
    return resolved


def build_match_filter(field, value):
    """Build an exact match filter for Qdrant query operations.

    Parameters
    ----------
    field: str
        Payload field name.

    value:
        Exact value to match.

    Returns
    -------
    qdrant_client.http.models.FieldCondition
        Qdrant field condition object.
    """
    _, models = _import_qdrant()
    return models.FieldCondition(
        key=field,
        match=models.MatchValue(value=value),
    )


def search_points(collection_name, query_vector, limit=10, exact_filters=None):
    """Run vector search in Qdrant with optional exact payload filters.

    Parameters
    ----------
    collection_name: str
        Target collection name.

    query_vector: list[float]
        Query embedding vector.

    limit: int, default=10
        Maximum number of points to return.

    exact_filters: dict, default=None
        Exact payload matches using `{field: value}` pairs.

    Returns
    -------
    list
        Qdrant scored points. The concrete type depends on the client version.
    """
    client = get_qdrant_client()
    _, models = _import_qdrant()
    exact_filters = exact_filters or {}
    conditions = [
        build_match_filter(field, value)
        for field, value in exact_filters.items()
        if value is not None
    ]
    query_filter = models.Filter(must=conditions) if conditions else None
    response = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        limit=limit,
        query_filter=query_filter,
        search_params=_build_search_params(models),
        with_payload=True,
    )
    return getattr(response, "points", response)


def _build_search_params(models):
    """Build optional Qdrant search params from settings.

    Returns
    -------
    qdrant_client.http.models.SearchParams | None
        Search params configured via `.env` or `None` to use Qdrant defaults.
    """
    qdrant_config = settings.QDRANT
    search_params_kwargs = {}
    if qdrant_config.search_hnsw_ef is not None:
        search_params_kwargs["hnsw_ef"] = qdrant_config.search_hnsw_ef
    if qdrant_config.search_exact is not None:
        search_params_kwargs["exact"] = qdrant_config.search_exact

    if not search_params_kwargs:
        return None

    return models.SearchParams(**search_params_kwargs)
