"""Metadata processing, vector synchronisation and task outcome values."""

from enum import StrEnum

from django.db.models import TextChoices


class EmbeddingSyncStatus(TextChoices):
    PENDING = "pending", "Pending"
    SYNCED = "synced", "Synced"
    FAILED = "failed", "Failed"


class MetadataProcessingStatus(TextChoices):
    """Shared ledger states for embedding and sentiment processing."""

    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    RUNNING = "running", "Running"
    RETRY_WAIT = "retry_wait", "Waiting to retry"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class MetadataTaskOutcome(StrEnum):
    EMBEDDED = "embedded"
    CLASSIFIED = "classified"
