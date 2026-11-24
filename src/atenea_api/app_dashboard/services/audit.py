"""Dashboard-side audit adapter.

The API audit helper assumes every URL contains ``/api/``.  Dashboard routes
are intentionally separate, so this small adapter records the same Query
contract without manufacturing an API URL or storing credentials.
"""

from app_base.models import GeneralEncoder, Query

import json


def _json_safe(value):
    return json.loads(json.dumps(value, cls=GeneralEncoder))


def record_query(request, data=None):
    user = request.user if getattr(request.user, "is_authenticated", False) else None
    query = Query.objects.create(
        location=request.META.get("REMOTE_ADDR", "") or "",
        owner=user,
        url=request.get_full_path()[:255],
        method=request.method,
        data=_json_safe(data or {}),
    )
    return query.token.hex

