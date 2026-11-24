"""Search filters for Elasticsearch and exact entity rankings in ClickHouse."""

import json
from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from elasticsearch.dsl import Q as ESQ

from app_base.api import array_tag_filter_kwargs
from app_entity.enums import from_name
from app_entity.models import EntityItem
from app_telegram.models import RoomItem
from app_telegram import clickhouse as ch


# ======================================================
# =====         MESSAGE ANALYTICS FILTERS          =====
# ======================================================


def build_message_filter(
    *, createdat_min=None, createdat_max=None, stored_since=None,
    room=None, tags=None, tag_match="any", is_reply=None,
    min_date=None, max_date=None,
):
    """Build an Elasticsearch Query DSL filter for the message analytics APIs.

    Dates, reply state and room IDs become Elasticsearch Query objects. Rooms
    and their current tags are first resolved through PostgreSQL so the filter
    keeps the same room-selection semantics as the message API. ``room_id`` is
    used in Elasticsearch because it is the stable identity of a room; tags
    are checked against PostgreSQL because the message index stores a snapshot.

    Parameters
    ----------
    createdat_min, createdat_max: date or datetime, optional
        Inclusive bounds for the message ``created_at`` field.
    stored_since: date or datetime, optional
        Inclusive lower bound for the message ``stored_date`` field.
    room: list[str], optional
        Room-name patterns interpreted with the message API's case-insensitive
        regular-expression matching.
    tags: list[str], optional
        Room tags to match using ``tag_match``.
    tag_match: {"any", "all"}, default="any"
        Whether a room must have any supplied tag or every supplied tag.
    is_reply: bool, optional
        Restrict the result to replies or non-replies.
    min_date, max_date: date or datetime, optional
        Compatibility aliases for the creation-date bounds.

    Returns
    -------
    elasticsearch.dsl.query.Query
        A composable Query DSL object. ``Search.query()`` accepts it directly;
        callers using an Elasticsearch API that expects a dictionary can call
        ``to_dict()`` at that boundary.
    """
    filters = []
    bounds = {}
    for key, value in (("gte", createdat_min or min_date), ("lte", createdat_max or max_date)):
        if value is not None:
            bounds[key] = value.isoformat() if hasattr(value, "isoformat") else value
    if bounds:
        filters.append(ESQ("range", created_at=bounds))
    if stored_since is not None:
        value = stored_since.isoformat() if hasattr(stored_since, "isoformat") else stored_since
        filters.append(ESQ("range", stored_date={"gte": value}))
    if is_reply is not None:
        filters.append(ESQ("term", is_reply=is_reply))
    if room or tags:
        # Resolve current room tags in PostgreSQL; ES stores their historical snapshot.
        rooms = RoomItem.objects.all()
        if room:
            rooms = rooms.filter(unique_name__iregex="(" + "|".join(room) + ")")
        if tags:
            rooms = rooms.filter(**array_tag_filter_kwargs("tags", tags, tag_match))
        ids = [str(pk) for pk in rooms.order_by().values_list("pk", flat=True)]
        if not ids:
            return ESQ("match_none")
        # A terms query is capped at 65,536 values, so split broad room matches.
        clauses = [
            ESQ("terms", room_id=ids[start:start + 65536])
            for start in range(0, len(ids), 65536)
        ]
        filters.append(clauses[0] if len(clauses) == 1 else ESQ(
            "bool", should=clauses, minimum_should_match=1,
        ))
    return ESQ("bool", filter=filters) if filters else ESQ("match_all")


# ======================================================
# =====          MESSAGE ENTITY RANKINGS           =====
# ======================================================


def entity_ranking_ordering(order):
    if order == "appearances":
        return ("-appearance_count", "-message_count", "entity_id")
    return ("-message_count", "-appearance_count", "entity_id")


def entity_ranking_results(rows, message_filters, *, messages_url, extra_params=None):
    """Fetch entity metadata only for this page; never aggregate message arrays."""
    entities = EntityItem.objects.in_bulk([row["entity_id"] for row in rows])
    params = dict(extra_params or {})
    for key, value in message_filters.items():
        if key in {"createdat_min", "createdat_max", "stored_since"}:
            value = timezone.localdate(value).strftime(settings.FORMAT_DATE)
        elif isinstance(value, bool):
            value = str(value).lower()
        params["tag" if key == "tags" else key] = value
    results = []
    for row in rows:
        entity = entities.get(row["entity_id"])
        if entity is None:  # Entity may have been deleted since aggregation.
            continue
        results.append({
            "entity_id": entity.pk, "name": entity.name, "type": entity.type_name,
            "message_count": row["message_count"],
            "appearance_count": row["appearance_count"],
            "messages_url": messages_url + "?" + urlencode(
                {**params, "entity": str(entity.pk)}, doseq=True,
            ),
        })
    return results


def _ranking_ordering(ordering):
    allowed = {entity_ranking_ordering("messages"), entity_ranking_ordering("appearances")}
    ordering = tuple(ordering or entity_ranking_ordering("messages"))
    if ordering not in allowed:
        raise ValueError("Unsupported entity ranking order")
    return ordering


def build_entity_ranking_query(
    message_filters, *, entity_type=None, entity_name=None, ordering=None,
    position=None, reverse=False, limit=61,
):
    """Return parameterized SQL and bindings; None means an empty PG selection."""
    ordering = _ranking_ordering(ordering)
    if not 1 <= int(limit) <= 10001:
        raise ValueError("Invalid entity ranking limit")
    filters = dict(message_filters or {})
    clauses = []
    params = {"limit": int(limit)}
    for name, column, operator in (
        ("createdat_min", "created_at", ">="),
        ("createdat_max", "created_at", "<="),
        ("stored_since", "stored_date", ">="),
    ):
        value = filters.get(name)
        if value is None:
            value = filters.get({"createdat_min": "min_date", "createdat_max": "max_date"}.get(name, ""))
        if value is not None:
            params[name] = value.isoformat() if hasattr(value, "isoformat") else str(value)
            clauses.append(f"{column} {operator} parseDateTime64BestEffort({{{name}:String}}, 6, 'UTC')")
    if filters.get("is_reply") is not None:
        clauses.append("is_reply = {is_reply:Bool}")
        params["is_reply"] = filters["is_reply"]
    room = filters.get("room")
    tags = filters.get("tags") or filters.get("tag")
    if room or tags:
        rooms = RoomItem.objects.all()
        if room:
            rooms = rooms.filter(unique_name__iregex="(" + "|".join(room) + ")")
        if tags:
            rooms = rooms.filter(**array_tag_filter_kwargs("tags", tags, filters.get("tag_match", "any")))
        params["rooms"] = [str(pk) for pk in rooms.order_by().values_list("pk", flat=True)]
        if not params["rooms"]:
            return None, params
        clauses.append("room_id IN {rooms:Array(UUID)}")
    if entity_type:
        types = entity_type if isinstance(entity_type, (list, tuple, set)) else [entity_type]
        params["types"] = [int(from_name(value)) for value in types]
        clauses.append("type IN {types:Array(Int16)}")
    if entity_name:
        params["entities"] = [str(pk) for pk in EntityItem.objects.filter(
            name__icontains=entity_name,
        ).order_by().values_list("pk", flat=True)]
        if not params["entities"]:
            return None, params
        clauses.append("entity_id IN {entities:Array(UUID)}")

    # UUID native order in ClickHouse differs from Elasticsearch keyword order.
    def expression(field):
        return "toString(entity_id)" if field == "entity_id" else field

    having = ["message_count > 0"]
    if position is not None:
        values = json.loads(position) if isinstance(position, str) else list(position)
        if len(values) != len(ordering):
            raise ValueError("Invalid entity ranking cursor position")
        branches = []
        for index, order in enumerate(ordering):
            field = order.lstrip("-")
            kind = "String" if field == "entity_id" else "Int64"
            params[f"cursor{index}"] = str(UUID(str(values[index]))) if kind == "String" else int(values[index])
            prior = [
                f"{expression(ordering[i].lstrip('-'))} = {{cursor{i}:{'String' if ordering[i].lstrip('-') == 'entity_id' else 'Int64'}}}"
                for i in range(index)
            ]
            operator = "<" if order.startswith("-") != reverse else ">"
            branches.append("(" + " AND ".join([
                *prior, f"{expression(field)} {operator} {{cursor{index}:{kind}}}",
            ]) + ")")
        having.append("(" + " OR ".join(branches) + ")")
    order_by = ", ".join(
        f"{expression(order.lstrip('-'))} {'DESC' if order.startswith('-') != reverse else 'ASC'}"
        for order in ordering
    )
    query = (
        "SELECT entity_id, sum(toInt64(sign)) AS message_count, "
        f"sum(toInt64(sign) * count) AS appearance_count FROM {ch.table_name()} "
        f"WHERE {' AND '.join(clauses) if clauses else '1'} GROUP BY entity_id "
        f"HAVING {' AND '.join(having)} ORDER BY {order_by} LIMIT {{limit:UInt64}}"
    )
    return query, params


def entity_ranking(message_filters, *, client=None, **kwargs):
    query, params = build_entity_ranking_query(message_filters, **kwargs)
    if query is None:
        return []
    with ch.client_session(client) as client:
        return [
            {"entity_id": UUID(str(row["entity_id"])),
             "message_count": int(row["message_count"]),
             "appearance_count": int(row["appearance_count"])}
            for row in ch.query_with_ids(client, query, parameters=params).named_results()
        ]
