"""Elasticsearch delete signals and scoped suppression during NER replacement."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import models
from django_elasticsearch_dsl.signals import BaseSignalProcessor


logger = logging.getLogger(__name__)
_suspend_elasticsearch_sync = ContextVar("suspend_elasticsearch_sync", default=False)


@contextmanager
def suspend_elasticsearch_sync():
    """Suppress delete-triggered Elasticsearch work in the current context.

    NER replaces annotation rows in bulk and performs one projection afterward;
    scheduling one delete job per annotation would duplicate that work.
    """
    token = _suspend_elasticsearch_sync.set(True)
    try:
        yield
    finally:
        _suspend_elasticsearch_sync.reset(token)


def elasticsearch_sync_suspended():
    """Return whether the current execution context is suppressing ES signals."""
    return _suspend_elasticsearch_sync.get()


def _uses_durable_message_projection(sender):
    """Identify models whose ES deletes are handled by durable projection jobs."""
    return sender._meta.label_lower in {
        "app_telegram.messageitem", "app_entity.entityitem", "app_entity.annotatedentity",
    }


class OnlyDeleteSignalES(BaseSignalProcessor):
    """Real-time signal processor which only will observe deletes actions for
    Elasticsearch index syncronization.

    Allows for observing when deletes fire and automatically updates the
    search engine index appropriately.
    """

    def __init__(self, connections):
        self._logged_signal_activations = set()
        super().__init__(connections)

    def setup(self):
        # Listen to all model deletes.
        models.signals.post_delete.connect(self.handle_delete)

        # Use to manage related objects update
        models.signals.pre_delete.connect(self.handle_pre_delete)

    def handle_pre_delete(self, sender, instance, **kwargs):
        skipped = (
            _suspend_elasticsearch_sync.get()
            or _uses_durable_message_projection(sender)
        )
        started_at = time.perf_counter()
        if skipped:
            # NER or durable projection owns this model's follow-up work.
            self._log_signal_activation(
                "pre_delete",
                sender,
                skipped=True,
                elapsed=time.perf_counter() - started_at,
            )
            return None
        # TODO: Replace per-row synchronization with a bulk update.
        try:
            return super().handle_pre_delete(sender, instance, **kwargs)
        finally:
            self._log_signal_activation(
                "pre_delete",
                sender,
                skipped=False,
                elapsed=time.perf_counter() - started_at,
            )

    def handle_delete(self, sender, instance, **kwargs):
        skipped = (
            _suspend_elasticsearch_sync.get()
            or _uses_durable_message_projection(sender)
        )
        started_at = time.perf_counter()
        if skipped:
            # Avoid an immediate per-object ES write; the owning job batches it.
            self._log_signal_activation(
                "post_delete",
                sender,
                skipped=True,
                elapsed=time.perf_counter() - started_at,
            )
            return None
        try:
            return super().handle_delete(sender, instance, **kwargs)
        finally:
            self._log_signal_activation(
                "post_delete",
                sender,
                skipped=False,
                elapsed=time.perf_counter() - started_at,
            )

    def _log_signal_activation(self, signal_name, sender, *, skipped, elapsed):
        model_label = sender._meta.label
        activation = (signal_name, model_label, skipped)
        if activation in self._logged_signal_activations:
            return
        self._logged_signal_activations.add(activation)
        logger.info(
            "Elasticsearch signal activated: %s for %s "
            "(synchronization: %s; signal: %.3f secs).",
            signal_name,
            model_label,
            "skipped" if skipped else "executed",
            elapsed,
        )

    def teardown(self):
        # Teardown all signals.
        models.signals.post_delete.disconnect(self.handle_delete)
        models.signals.pre_delete.disconnect(self.handle_pre_delete)
