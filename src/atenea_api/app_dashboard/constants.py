"""Presentation constants for the dashboard.

The dashboard deliberately keeps domain identifiers out of the normal UI.  The
maps in this module are the single place where those identifiers are translated
to labels that make sense to an operator.
"""

from django.utils.translation import gettext_lazy as _
from app_scheduler.enums import BatchStatus, JobStatus

JOB_STATUS_META = {
    JobStatus.COLLECTING: {"label": _("Collecting"), "tone": "info", "icon": "↓", "animated": True},
    JobStatus.RUNNING: {"label": _("Running"), "tone": "running", "icon": "▶", "animated": True},
    JobStatus.PAUSED: {"label": _("Paused"), "tone": "paused", "icon": "Ⅱ", "animated": False},
    JobStatus.CANCELLING: {"label": _("Cancelling"), "tone": "warning", "icon": "×", "animated": True},
    JobStatus.COMPLETED: {"label": _("Completed"), "tone": "success", "icon": "✓", "animated": False},
    JobStatus.FAILED: {"label": _("Failed"), "tone": "error", "icon": "!", "animated": False},
    JobStatus.WAITING_RETRY: {"label": _("Waiting for retry"), "tone": "retry", "icon": "↻", "animated": False},
    JobStatus.PARTIAL: {"label": _("Partial"), "tone": "warning", "icon": "!", "animated": False},
    JobStatus.CANCELLED: {"label": _("Cancelled"), "tone": "muted", "icon": "×", "animated": False},
}

BATCH_STATUS_META = {
    BatchStatus.PENDING: {"label": _("Pending"), "tone": "muted", "icon": "○", "animated": False},
    BatchStatus.RUNNING: {"label": _("Running"), "tone": "running", "icon": "▶", "animated": True},
    BatchStatus.RETRY_WAIT: {"label": _("Retrying"), "tone": "retry", "icon": "↻", "animated": False},
    BatchStatus.COMPLETED: {"label": _("Completed"), "tone": "success", "icon": "✓", "animated": False},
    BatchStatus.FAILED: {"label": _("Failed"), "tone": "error", "icon": "!", "animated": False},
    BatchStatus.CANCELLED: {"label": _("Cancelled"), "tone": "muted", "icon": "×", "animated": False},
}

PIPELINE_LABELS = {
    "telegram.room_metadata.sync": _("Synchronize room metadata"),
    "entity.ner.backfill": _("Detect entities"),
    "metadata.embeddings": _("Generate embeddings"),
    "metadata.sentiment": _("Analyze sentiment"),
    "telegram.scan.rooms": _("Collect Telegram messages"),
    "telegram.scan.comments": _("Collect comments"),
    "telegram.access.rooms": _("Recover room access"),
    "telegram.populate": _("Populate Telegram resources"),
    "telegram.index.messages": _("Index messages"),
    "telegram.process_messages": _("Process messages"),
}

STAGE_LABELS = {
    "telegram.room_metadata.sync.write": _("Update room names in search"),
    "telegram.scan.rooms.collect_messages": _("Collect messages"),
    "telegram.scan.comments.collect_comments": _("Collect comments"),
    "telegram.access.rooms.resolve_hashes": _("Resolve room access"),
    "telegram.index.messages.write": _("Index messages"),
    "telegram.populate.seeds.resolve": _("Resolve resources"),
    "telegram.index.rooms_from_seeds": _("Index resources"),
    "entity.ner.extract": _("Detect entities"),
    "entity.ner.store": _("Store entities"),
    "metadata.embeddings.generate_and_sync": _("Generate embeddings"),
    "metadata.sentiment.classify_and_store": _("Analyze sentiment"),
}

PIPELINE_FIELD_LABELS = {
    "check_room": _("Check room information"),
    "room": _("Rooms"),
    "tags": _("Tags"),
    "tag_match": _("Tag matching"),
    "lang": _("Languages"),
    "langs": _("Languages"),
    "type": _("Message types"),
    "types": _("Message types"),
    "is_reply": _("Replies only"),
    "stored_since": _("Stored since"),
    "createdat_min": _("Created from"),
    "createdat_max": _("Created until"),
    "block_size": _("Block size"),
    "max_msgs": _("Maximum messages"),
    "update_users": _("Update users"),
    "is_channel": _("Is a channel"),
    "lastup_min": _("Updated from"),
    "lastup_max": _("Updated until"),
    "by_resource": _("By resource"),
    "by_title": _("By title"),
    "collected_min": _("Collected from"),
    "collected_max": _("Collected until"),
    "refresh": _("Refresh existing data"),
    "reindex": _("Reindex results"),
    "instruct": _("Instruction"),
    "slot": _("Storage slot"),
}

PIPELINE_FIELD_HELP_TEXTS = {
    "check_room": _(
        "Compare the username, public link and title with Telegram and record any changes. "
        "The description is checked only when one of those changes. "
        "Message links and search data are updated only when affected."
    ),
}

HIDDEN_PARAMETER_FIELDS = {"block_size", "token", "room_sync_version"}
ADVANCED_PARAMETER_FIELDS = {
    "createdat_min",
    "createdat_max",
    "stored_since",
    "collected_min",
    "collected_max",
    "lastup_min",
    "lastup_max",
    "refresh",
    "reindex",
    "instruct",
    "slot",
    "update_users",
}

JOB_ACTION_LABELS = {
    "pause": _("Pause"),
    "resume": _("Resume"),
    "cancel": _("Cancel"),
    "retry-failed": _("Retry failed"),
    "retry-pending": _("Retry pending processing"),
    "discard-failed": _("Discard failed and continue"),
}


def pipeline_label(key):
    return PIPELINE_LABELS.get(key, str(key).replace(".", " ").replace("_", " ").capitalize())


def stage_label(key):
    return STAGE_LABELS.get(key, str(key).rsplit(".", 1)[-1].replace("_", " ").capitalize())


def job_status_meta(status):
    return JOB_STATUS_META.get(status, {"label": _("Unknown status"), "tone": "muted", "icon": "?", "animated": False})


def batch_status_meta(status):
    return BATCH_STATUS_META.get(status, {"label": _("Unknown status"), "tone": "muted", "icon": "?", "animated": False})
