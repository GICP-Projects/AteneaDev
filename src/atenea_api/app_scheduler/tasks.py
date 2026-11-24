"""Celery entry points owned by the generic processing scheduler.

Application work lives in its owning app: NER tasks are in ``app_entity`` and
Telegram scan/index tasks are in ``app_telegram``. These tasks only recover or
publish generic Job/Batch state.
"""

from celery import shared_task

from app_scheduler.services.jobs import dispatch_handoffs, dispatch_ready_batches, reconcile_jobs


@shared_task(track_started=True, ignore_result=True, acks_late=True, task_reject_on_worker_lost=True)
def dispatch_processing_job(job_id):
    return dispatch_ready_batches(job_id)


@shared_task(track_started=True, ignore_result=True, acks_late=True, task_reject_on_worker_lost=True)
def dispatch_processing_handoffs(job_id):
    return dispatch_handoffs(job_id)


@shared_task(track_started=True, ignore_result=True, acks_late=True, task_reject_on_worker_lost=True)
def reconcile_processing_jobs():
    return reconcile_jobs()
