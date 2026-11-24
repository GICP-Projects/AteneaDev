"""Generic durable-work records.

``ProcessingJob`` deliberately knows nothing about NER, Telegram or
Elasticsearch. An owning application declares a workflow made of stage keys;
the scheduler only persists progress, capacity and leases for those stages.
"""

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType

from app_base.models import BaseModel, GeneralEncoder
from app_scheduler.contracts import empty_processing_summary, validate_processing_summary
from app_scheduler.enums import (
    BATCH_TERMINAL_STATUSES,
    JOB_ACTIVE_STATUSES,
    JOB_RETRYABLE_STATUSES,
    JOB_TERMINAL_STATUSES,
    BatchStatus,
    HandoffStatus,
    InputOutcome,
    JobMode,
    JobStatus,
)


class ProcessingJob(BaseModel):
    """A durable, application-owned processing workflow.

    ``processor`` is an opaque owner key (for example ``"entity.ner"`` or
    ``"telegram.scan.rooms"``). ``workflow`` is an ordered list of opaque stage
    keys. Neither value is interpreted by this model or by the scheduler's
    persistence layer.
    """

    # Keep legacy references as plain-string aliases to the shared enums.
    LIVE = JobMode.LIVE.value
    BACKFILL = JobMode.BACKFILL.value
    MODE_CHOICES = JobMode.choices

    COLLECTING = JobStatus.COLLECTING.value
    RUNNING = JobStatus.RUNNING.value
    PAUSED = JobStatus.PAUSED.value
    CANCELLING = JobStatus.CANCELLING.value
    CANCELLED = JobStatus.CANCELLED.value
    COMPLETED = JobStatus.COMPLETED.value
    FAILED = JobStatus.FAILED.value
    WAITING_RETRY = JobStatus.WAITING_RETRY.value
    PARTIAL = JobStatus.PARTIAL.value
    STATUS_CHOICES = JobStatus.choices
    ACTIVE_STATUSES = tuple(status.value for status in JOB_ACTIVE_STATUSES)
    TERMINAL_STATUSES = tuple(status.value for status in JOB_TERMINAL_STATUSES)
    RETRYABLE_STATUSES = tuple(status.value for status in JOB_RETRYABLE_STATUSES)

    INPUT_COMPLETE = InputOutcome.COMPLETE.value
    INPUT_PARTIAL = InputOutcome.PARTIAL.value
    INPUT_OUTCOME_CHOICES = InputOutcome.choices

    # The owner validates both fields. They remain intentionally open so new
    # processors do not require a scheduler migration merely to name a stage.
    processor = models.CharField(max_length=64)
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    workflow = models.JSONField(default=list, encoder=GeneralEncoder)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=COLLECTING)
    parent_job = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_jobs",
    )

    # Opaque processor-owned input. The scheduler only stores it so a Job is
    # inspectable and reproducible; it never builds ORM filters from it.
    filters = models.JSONField(default=dict, encoder=GeneralEncoder)
    source_snapshot_at = models.DateTimeField(null=True, blank=True)
    source_cursor = models.JSONField(default=dict, encoder=GeneralEncoder)
    source_exhausted = models.BooleanField(default=False)
    input_closed_at = models.DateTimeField(null=True, blank=True)
    # A child can complete all input it received successfully even when its
    # parent deliberately abandoned failed batches.  Keep that provenance
    # separate from the child's own processing status.
    input_outcome = models.CharField(
        max_length=16,
        choices=INPUT_OUTCOME_CHOICES,
        default=INPUT_COMPLETE,
    )
    # Failed batches remain FAILED for auditability.  This timestamp is the
    # explicit operator decision that makes them non-retryable.
    failures_abandoned_at = models.DateTimeField(null=True, blank=True)
    # A scheduled launcher snapshots its lifecycle policy here.  Manual Jobs
    # keep this object empty and therefore never receive schedule-driven
    # automatic finalisation.
    lifecycle_policy = models.JSONField(default=dict, encoder=GeneralEncoder)
    # Set when a scheduled Job enters WAITING_RETRY.  It is deliberately a
    # concrete column so the reconciler can find due Jobs without inspecting
    # JSON for every row.
    retry_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # A Job may know all its source input while still producing handoffs for a
    # child Job.  This timestamp is set once its own batches are terminal.
    source_finished_at = models.DateTimeField(null=True, blank=True)

    fingerprint = models.CharField(max_length=64, db_index=True)
    batch_size = models.PositiveIntegerField(default=500)
    max_inflight_batches = models.PositiveSmallIntegerField(default=1)

    total_items = models.PositiveBigIntegerField(default=0)
    processed_items = models.PositiveBigIntegerField(default=0)
    failed_items = models.PositiveBigIntegerField(default=0)
    deferred_items = models.PositiveBigIntegerField(default=0)
    skipped_items = models.PositiveBigIntegerField(default=0)
    total_batches = models.PositiveIntegerField(default=0)
    estimated_batches_total = models.PositiveIntegerField(null=True, blank=True, default=None)
    completed_batches = models.PositiveIntegerField(default=0)
    failed_batches = models.PositiveIntegerField(default=0)

    # Cached descendant counters make the root of a composed pipeline useful
    # to an operational dashboard without forcing recursive aggregation on
    # every API request.
    descendant_processed_items = models.PositiveBigIntegerField(default=0)
    descendant_failed_items = models.PositiveBigIntegerField(default=0)
    descendant_deferred_items = models.PositiveBigIntegerField(default=0)
    descendant_skipped_items = models.PositiveBigIntegerField(default=0)

    request_token = models.UUIDField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def can_retry_failed(self):
        """Whether this Job's own failed batches are still recoverable."""

        return bool(
            self.failed_batches
            and self.failures_abandoned_at is None
            and self.status in {*self.RETRYABLE_STATUSES, self.COLLECTING, self.RUNNING, self.PAUSED}
        )

    class Meta:
        indexes = [
            models.Index(fields=["processor", "status", "created_at"], name="app_schedul_process_45ff23_idx"),
            models.Index(fields=["mode", "status", "created_at"], name="app_schedul_mode_b0b576_idx"),
            models.Index(fields=["parent_job", "status", "created_at"], name="app_schedul_parent_08bf4b_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["processor", "fingerprint"],
                condition=Q(
                    status__in=tuple(status.value for status in JOB_ACTIVE_STATUSES)
                ),
                name="scheduler_active_processor_fingerprint",
            )
        ]


class ProcessingBatch(BaseModel):
    """One immutable source slice travelling through a Job workflow."""

    PENDING = BatchStatus.PENDING.value
    RUNNING = BatchStatus.RUNNING.value
    RETRY_WAIT = BatchStatus.RETRY_WAIT.value
    COMPLETED = BatchStatus.COMPLETED.value
    FAILED = BatchStatus.FAILED.value
    CANCELLED = BatchStatus.CANCELLED.value
    STATUS_CHOICES = BatchStatus.choices
    TERMINAL_STATUSES = tuple(status.value for status in BATCH_TERMINAL_STATUSES)

    job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="batches")
    ordinal = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)

    # The scheduler treats these as opaque UUID-like values. The owner knows
    # which model they identify, allowing the same Batch abstraction to serve
    # Messages, rooms or future metadata records.
    item_ids = models.JSONField(default=list, encoder=GeneralEncoder)
    item_count = models.PositiveIntegerField(default=0)
    item_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="processing_batches",
    )
    # Resource ownership is scoped to one Job.  Telegram uses this to keep
    # batches for one credential sequential without serialising other Jobs.
    resource_key = models.CharField(max_length=128, blank=True, default="")
    recover_on_lease_expiry = models.BooleanField(default=True)

    current_stage = models.CharField(max_length=128, blank=True, default="")
    completed_stages = models.JSONField(default=list, encoder=GeneralEncoder)
    stage_attempts = models.JSONField(default=dict, encoder=GeneralEncoder)

    processor_metadata = models.JSONField(default=dict, encoder=GeneralEncoder)
    processor_version = models.CharField(max_length=64, blank=True, default="")
    result_summary = models.JSONField(
        default=empty_processing_summary,
        encoder=GeneralEncoder,
        validators=[validate_processing_summary],
    )

    processed_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    deferred_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    source_handoff = models.ForeignKey(
        "ProcessingHandoff",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materialized_batches",
    )
    queued_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    run_after = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["job_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["job", "ordinal"], name="scheduler_job_batch_ordinal")
        ]
        indexes = [
            models.Index(fields=["job", "status", "ordinal"], name="app_schedul_job_id_8bf0c6_idx"),
            models.Index(fields=["job", "resource_key", "status"], name="app_schedul_job_res_9d1590_idx"),
            models.Index(fields=["status", "queued_at"], name="app_schedul_status_aecfa3_idx"),
            models.Index(fields=["lease_until"], name="app_schedul_lease_u_0e44ee_idx"),
        ]


class ProcessingHandoff(BaseModel):
    """Durable, generic output from one Job into another.

    The row stores only opaque object IDs.  Source and target applications own
    the meaning of those objects; Scheduler owns materialisation, capacity and
    auditability.  Handoffs are deliberately chunked before being persisted.
    """

    PENDING = HandoffStatus.PENDING.value
    MATERIALIZED = HandoffStatus.MATERIALIZED.value
    CANCELLED = HandoffStatus.CANCELLED.value
    STATUS_CHOICES = HandoffStatus.choices

    source_batch = models.ForeignKey(
        ProcessingBatch,
        on_delete=models.CASCADE,
        related_name="outgoing_handoffs",
    )
    target_job = models.ForeignKey(
        ProcessingJob,
        on_delete=models.CASCADE,
        related_name="incoming_handoffs",
    )
    item_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="processing_handoffs",
    )
    item_ids = models.JSONField(default=list, encoder=GeneralEncoder)
    item_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    target_batch = models.ForeignKey(
        ProcessingBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materialized_from_handoff",
    )
    materialized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["target_job", "status", "created_at"], name="app_schedul_handof_1f2d51_idx"),
            models.Index(fields=["source_batch", "target_job"], name="app_schedul_handof_4b0941_idx"),
        ]
