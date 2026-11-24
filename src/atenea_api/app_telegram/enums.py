"""Telegram resource states, task outcomes and terminal checkpoint rules.

Task outcomes preserve the existing Celery response strings. Room scans and
comment scans share ItemProgressState, but decide separately which recorded
outcomes finish an input within their Batch.
"""

from enum import StrEnum

from django.db.models import TextChoices

from app_scheduler.enums import ItemProgressState


class MediaStatus(TextChoices):
    PENDING = "pending", "Pending"
    DOWNLOADED = "downloaded", "Downloaded"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"
    DELETED = "deleted", "Deleted"


class ExternalUrlStatus(TextChoices):
    NOT_DOWNLOADED = "not_downloaded", "Not downloaded"
    DELETED = "deleted", "Deleted"


class MediaProgressStatus(StrEnum):
    """Redis progress for a download run, separate from one media item's state."""

    QUEUED = "queued"
    SCHEDULING = "scheduling"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class TelegramTaskOutcome(StrEnum):
    SCANNED = "scanned"
    COMMENTS_SCANNED = "comments_scanned"
    POPULATED = "populated"
    ACCESS_RECALCULATED = "access_recalculated"
    INDEXED = "indexed"
    ROOMS_INDEXED = "rooms_indexed"


SCAN_ROOM_TERMINAL_STATES = frozenset({
    ItemProgressState.COMPLETED,
    ItemProgressState.DEFERRED,
})
SCAN_COMMENT_PARENT_TERMINAL_STATES = frozenset({
    ItemProgressState.COMPLETED,
    ItemProgressState.SKIPPED,
})
