"""Shared lifecycle values and execution outcomes for processing pipelines.

Enums retain the strings already stored in PostgreSQL and returned by tasks.
Model field choices use TextChoices; JSON progress and task outcomes use
StrEnum so processor applications can share values without importing models.
"""

from enum import StrEnum

from django.db.models import TextChoices


class JobMode(TextChoices):
    LIVE = "live", "Live"
    BACKFILL = "backfill", "Backfill"


class JobStatus(TextChoices):
    COLLECTING = "collecting", "Collecting input"
    RUNNING = "running", "Running"
    PAUSED = "paused", "Paused"
    CANCELLING = "cancelling", "Cancelling"
    CANCELLED = "cancelled", "Cancelled"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    WAITING_RETRY = "waiting_retry", "Waiting for retry"
    PARTIAL = "partial", "Partial"


class InputOutcome(TextChoices):
    COMPLETE = "complete", "Complete"
    PARTIAL = "partial", "Partial"


class BatchStatus(TextChoices):
    PENDING = "pending", "Ready"
    RUNNING = "running", "Running a stage"
    RETRY_WAIT = "retry_wait", "Waiting to retry"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class HandoffStatus(TextChoices):
    PENDING = "pending", "Pending"
    MATERIALIZED = "materialized", "Materialized"
    CANCELLED = "cancelled", "Cancelled"


class ItemProgressState(StrEnum):
    """Checkpoint outcome for one input; terminal states depend on its owner."""

    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"


class TaskOutcome(StrEnum):
    """Control outcomes of one delivery, separate from persisted Batch status.

    Successful operation names belong to the processor application's enums.
    A paused task may release its Batch back to BatchStatus.PENDING; a waiting
    task records BatchStatus.RETRY_WAIT while returning WAITING_RETRY.
    """

    PAUSED = "paused"
    SUPERSEDED = "superseded"
    WAITING_RETRY = "waiting_retry"
    FAILED = "failed"


JOB_ACTIVE_STATUSES = (
    JobStatus.COLLECTING,
    JobStatus.RUNNING,
    JobStatus.PAUSED,
    JobStatus.CANCELLING,
    JobStatus.WAITING_RETRY,
)
JOB_TERMINAL_STATUSES = (
    JobStatus.CANCELLED,
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.PARTIAL,
)
JOB_RETRYABLE_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.WAITING_RETRY,
)
BATCH_TERMINAL_STATUSES = (
    BatchStatus.COMPLETED,
    BatchStatus.FAILED,
    BatchStatus.CANCELLED,
)
