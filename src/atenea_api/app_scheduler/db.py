"""Database boundaries and row locks for durable processing.

Writers acquire a Job before its Batches. Non-key locks retain writer
exclusion without blocking foreign-key checks from upstream checkpoints.
"""

from functools import wraps
import logging
import random
import time

from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction

from app_scheduler.models import ProcessingBatch, ProcessingJob


logger = logging.getLogger(__name__)
DEADLOCK_ATTEMPTS = 3


def is_deadlock(error):
    """Recognize PostgreSQL deadlocks by SQLSTATE, never by message text."""

    cause = error
    while cause is not None:
        if getattr(cause, "pgcode", None) == "40P01":
            return True
        cause = cause.__cause__
    return False


def retry_on_deadlock(operation):
    """Retry an owned database transaction after a complete rollback.

    Apply only to database operations. Nested callers leave recovery to their
    outer transaction owner. The first commit callback records success before
    any application callback: a callback failure must not replay committed
    messages, handoffs, or checkpoints.
    """

    @wraps(operation)
    def run(*args, **kwargs):
        connection = transaction.get_connection()
        if connection.in_atomic_block or not connection.get_autocommit():
            return operation(*args, **kwargs)

        for attempt in range(1, DEADLOCK_ATTEMPTS + 1):
            committed = False

            def mark_committed():
                nonlocal committed
                committed = True

            try:
                with transaction.atomic():
                    transaction.on_commit(mark_committed)
                    result = operation(*args, **kwargs)
                return result
            except OperationalError as error:
                if committed or not is_deadlock(error) or attempt == DEADLOCK_ATTEMPTS:
                    raise
                logger.warning(
                    "PostgreSQL 40P01 in %s.%s; retrying transaction (%s/%s)",
                    operation.__module__, operation.__qualname__, attempt, DEADLOCK_ATTEMPTS,
                )
                # Sleep after atomic() has rolled back and released its locks.
                time.sleep(min(0.1 * 2 ** (attempt - 1), 1.0) + random.uniform(0.0, 0.1))

    return run


def lock_batch_and_job(batch_id, *, using=DEFAULT_DB_ALIAS):
    """Return a Batch with its current Job locked first, in one transaction.

    The initial lookup does not lock the Batch. Revalidate its Job association
    after acquiring the Job lock, and cache that locked object on the Batch.
    Checkpoint code that updates only a Batch uses a Batch-only lock instead;
    it must defer Job counter refreshes until after committing.
    """

    job_id = ProcessingBatch.objects.using(using).values_list("job_id", flat=True).get(pk=batch_id)
    job = ProcessingJob.objects.using(using).select_for_update(no_key=True).get(pk=job_id)
    batch = (
        ProcessingBatch.objects.using(using).select_for_update(no_key=True)
        .get(pk=batch_id, job_id=job_id)
    )
    batch.job = job
    return batch
