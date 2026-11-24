"""Display states derived from lifecycle and workflow records.

These retain the dashboard's existing output strings. RETRY is used by graph
stages, RETRYING by batch/detail presentation, and QUEUED by graph task nodes.
They describe presentation; database Batch states use app_scheduler.enums.
"""

from enum import StrEnum


class StageDisplayState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
