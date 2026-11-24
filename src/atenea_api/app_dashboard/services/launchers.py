"""Direct adapters from the dashboard to the existing Job launchers."""

from app_scheduler.registry import (
    get_scheduled_pipeline,
    normalize_pipeline_parameters,
)
from app_telegram.services import api as telegram_api
from django.utils.translation import gettext as _


LAUNCHERS = {
    "entity.ner.backfill": telegram_api.msg_ner,
    "metadata.embeddings": telegram_api.msg_embed,
    "metadata.sentiment": telegram_api.msg_sentiment,
    "telegram.scan.rooms": telegram_api.room_scanning,
    "telegram.scan.comments": telegram_api.msg_scanning_comments,
    "telegram.access.rooms": telegram_api.room_access,
    "telegram.populate": telegram_api.seed_populate,
    "telegram.index.messages": telegram_api.msg_index,
}


def _launcher_parameters(pipeline_key, parameters):
    pipeline = get_scheduled_pipeline(pipeline_key)
    normalized = normalize_pipeline_parameters(pipeline, parameters or {})
    # The public scheduler contract uses plural aliases for the comments
    # pipeline. Its application-owned compatibility launcher uses its original
    # singular argument names.
    if pipeline_key == "telegram.scan.comments":
        normalized["lang"] = normalized.pop("langs", [])
        normalized["type"] = normalized.pop("types", [])
    return normalized


def _job_id_from_result(result):
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        result = result.get("job_id") or result.get("id")
    if result is None:
        return None
    return str(getattr(result, "id", result))


def launch_pipeline(pipeline_key, parameters, request_token=None):
    """Validate and launch a registered pipeline through its owning service."""

    launcher = LAUNCHERS.get(pipeline_key)
    if launcher is None:
        raise ValueError(_("Unknown or non-schedulable operation."))
    kwargs = _launcher_parameters(pipeline_key, parameters)
    result = launcher(token=request_token, **kwargs)
    job_id = _job_id_from_result(result)
    if not job_id:
        raise RuntimeError(_("The operation did not return a durable Job."))
    return job_id
