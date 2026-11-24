"""Message activity statistics backed by Elasticsearch."""

from datetime import datetime, timedelta, timezone as dt_timezone
import math

from django.conf import settings
from django.utils import timezone
from elasticsearch.dsl import Search, connections

from app_telegram.serializers import ANY as TAG_ANY


def _message_index_name():
    return settings.ELASTICSEARCH_INDEX_NAMES["app_telegram.documents.MessageDocument"]


def _build_message_search(filters):
    """Build a filtered message search using the shared analytics filter."""

    # Room/tag resolution and ES predicates are shared with entity rankings.
    from app_telegram.services.analytics import build_message_filter

    query = build_message_filter(**filters)
    client = connections.get_connection()
    if hasattr(client, "options"):
        client = client.options(max_retries=0)  # Keep the shared connection timeout.
    return Search(
        using=client,
        index=_message_index_name(),
    ).query(query).params(allow_partial_search_results=False)


def _execute_complete(search):
    response = search.execute()
    data = response.to_dict() if hasattr(response, "to_dict") else {}
    if data.get("timed_out") or data.get("_shards", {}).get("failed", 0):
        raise RuntimeError("Elasticsearch returned incomplete message statistics")
    return response


def _bucket_value(bucket, name):
    """Read fields from Elasticsearch aggregation buckets and simple test doubles."""

    if isinstance(bucket, dict):
        return bucket.get(name)
    return getattr(bucket, name)


def _period_start_from_bucket(bucket):
    key = _bucket_value(bucket, "key")
    if isinstance(key, datetime):
        period_start = key
    elif isinstance(key, str):
        period_start = datetime.fromisoformat(key.replace("Z", "+00:00"))
    else:
        period_start = datetime.fromtimestamp(float(key) / 1000, tz=dt_timezone.utc)
    if timezone.is_naive(period_start):
        period_start = timezone.make_aware(period_start, dt_timezone.utc)
    return timezone.localtime(period_start)


def _pipeline_value(bucket, name):
    pipeline = _bucket_value(bucket, name)
    if isinstance(pipeline, dict):
        return pipeline.get("value")
    return getattr(pipeline, "value", None)


def _finite_score(value):
    """Normalize unavailable ES pipeline values to 0.0."""

    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def _undated_period(search, group_by):
    """Count documents lacking ``created_at`` and prepare a pre-date histogram bucket.

    The API returns one ``period_start=None`` group for these records. An
    unbounded date histogram omits them, so this count supplies that group
    and includes it in Z-score normalization.
    Date-bounded requests do not need this extra search because null dates cannot
    match their range predicate.
    """
    metadata = search._clone()
    metadata.aggs.bucket("undated", "missing", field="created_at")
    metadata.aggs.metric("first_created_at", "min", field="created_at")
    response = _execute_complete(metadata)
    missing = getattr(response.aggregations, "undated", {"doc_count": 0})
    count = int(_bucket_value(missing, "doc_count"))
    if not count:
        return 0, None, None
    minimum = response.aggregations.first_created_at.value
    if minimum is None:
        return count, None, None
    first = timezone.localtime(datetime.fromtimestamp(minimum / 1000, tz=dt_timezone.utc))
    first = first.replace(hour=0, minute=0, second=0, microsecond=0)
    if group_by == "week":
        first -= timedelta(days=first.weekday())
    elif group_by == "month":
        first = first.replace(day=1)
    elif group_by == "year":
        first = first.replace(month=1, day=1)
    # The sentinel sorts before every real bucket, even when grouping by year.
    return count, int(minimum) - 367 * 86400000, int(first.timestamp() * 1000)


def get_message_stats(
    createdat_min=None,
    createdat_max=None,
    group_by="day",
    room=None,
    is_reply=None,
    stored_since=None,
    tags=None,
    tag_match=TAG_ANY,
    z_score=False,
):
    """Return message counts from one Elasticsearch date histogram.

    The histogram intentionally omits empty periods. The dashboard fills those
    periods with count zero after it has chosen the selected calendar range.
    Elasticsearch's ``z-score`` normalize pipeline uses population variance,
    so singleton and equal-count periods receive a zero score.
    """

    filters = {
        "createdat_min": createdat_min,
        "createdat_max": createdat_max,
        "room": room,
        "tags": tags,
        "tag_match": tag_match,
        "is_reply": is_reply,
        "stored_since": stored_since,
    }
    search = _build_message_search(filters).extra(size=0)
    undated, sentinel, first_period = (0, None, None)
    if createdat_min is None and createdat_max is None:
        undated, sentinel, first_period = _undated_period(search, group_by)
        if undated and sentinel is None:
            return [{"period_start": None, "count": undated,
                     **({"z_score": 0.0} if z_score else {})}]
    periods = search.aggs.bucket(
        "periods",
        "date_histogram",
        field="created_at",
        calendar_interval=group_by,
        time_zone=timezone.get_current_timezone_name(),
        min_doc_count=1,
        **({"missing": sentinel} if sentinel is not None else {}),
    )
    if z_score:
        periods.pipeline(
            "z_score",
            "normalize",
            buckets_path="_count",
            method="z-score",
        )

    response = _execute_complete(search)
    buckets = response.aggregations.periods.buckets
    rows = []
    undated_row = None
    for bucket in buckets:
        is_undated = first_period is not None and _bucket_value(bucket, "key") < first_period
        row = {
            "period_start": None if is_undated else _period_start_from_bucket(bucket),
            "count": int(_bucket_value(bucket, "doc_count")),
        }
        if z_score:
            row["z_score"] = _finite_score(_pipeline_value(bucket, "z_score"))
        if is_undated:
            undated_row = row
        else:
            rows.append(row)
    if undated_row is not None:
        rows.append(undated_row)  # Keep undated messages after the dated periods.
    return rows


def get_message_created_at_minimum(
    *,
    createdat_min=None,
    createdat_max=None,
    room=None,
    is_reply=None,
    stored_since=None,
    tags=None,
    tag_match=TAG_ANY,
):
    """Return the first matching creation timestamp using Elasticsearch only."""

    filters = {
        "createdat_min": createdat_min,
        "createdat_max": createdat_max,
        "room": room,
        "tags": tags,
        "tag_match": tag_match,
        "is_reply": is_reply,
        "stored_since": stored_since,
    }
    search = _build_message_search(filters).extra(size=0)
    search.aggs.metric("first_created_at", "min", field="created_at")
    response = _execute_complete(search)
    value = response.aggregations.first_created_at.value
    if value is None or not math.isfinite(float(value)):
        return None
    minimum = datetime.fromtimestamp(float(value) / 1000, tz=dt_timezone.utc)
    return timezone.localtime(minimum)
