"""Telegram credential signals and durable search/analytics synchronization.

Index receivers persist work for Telegram messages and their entity counts.
NER suppresses them while replacing annotations and uses its final INDEX stage.
"""

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet
from django.db.models.signals import post_delete, pre_delete, pre_save
from django.dispatch import receiver

from app_base.signals import elasticsearch_sync_suspended
from app_entity.models import AnnotatedEntity, EntityItem
from app_telegram.models import MessageItem, RoomItem, TelegramAuth, UserItem
from app_telegram.services.jobs import queue_index_sync


logger = logging.getLogger(__name__)


# ======================================================
# =====      TELEGRAM AUTHENTICATION SIGNALS       =====
# ======================================================


@receiver(pre_delete, sender=TelegramAuth)
def handle_telegramauth_delete(sender, instance, **kwargs):
    RoomItem.objects.filter(access_auth=instance).update(is_valid=False, access_hash=None)
    UserItem.objects.filter(access_auth=instance).update(is_valid=False, access_hash=None)


@receiver(pre_save, sender=TelegramAuth)
def handle_telegramauth_hibernate(sender, instance, **kwargs):
    try:
        # Only if it is an update (not a new instance)
        old_instance = TelegramAuth.objects.get(pk=instance.pk)
        # Check if the credential `is_hibernated` flag has changed to True
        if not old_instance.is_hibernated and instance.is_hibernated:
            # Set related entities when hibernate is set to True
            instance.is_valid = False
            num_rooms =RoomItem.objects.filter(access_auth=instance).update(
                is_valid=False, access_hash=None, access_auth=None
            )
            num_users = UserItem.objects.filter(access_auth=instance).update(
                is_valid=False, access_hash=None, access_auth=None
            )

            logger.info(
                f"TelegramAuth {instance.pk} has been hibernated. {num_rooms} RoomItem items "
                f"and {num_users} UserItem items have been set to invalid, access_hash and "
                "access_auth fields have been set to None. The instance `is_valid` field "
                "has been set to False."
            )

    except TelegramAuth.DoesNotExist:
        pass


# ======================================================
# =====    SEARCH AND ANALYTICS DELETIONS    =====
# ======================================================


def _should_queue():
    """Skip duplicate jobs while a projection owner handles the current change."""
    return not elasticsearch_sync_suspended()


@receiver(post_delete, sender=MessageItem, dispatch_uid="telegram_index_message_delete")
def message_deleted(sender, instance, using, **kwargs):
    """Queue removal of a message's search document and all its entity counts."""
    if _should_queue():
        queue_index_sync([instance.pk], operation="delete_messages", using=using)


@receiver(pre_delete, sender=EntityItem, dispatch_uid="telegram_index_entity_messages")
def entity_messages_before_delete(sender, instance, using, **kwargs):
    """Queue affected Telegram messages before their annotations disappear."""
    if not _should_queue():
        return
    content_type = ContentType.objects.db_manager(using).get_for_model(MessageItem)
    ids = (instance.annotations.using(using).filter(content_type=content_type)
           .order_by("object_id").values_list("object_id", flat=True).distinct())
    batch = []
    scheduler_batch_size = max(1, settings.INDEX_MANUAL_BATCH_SIZE)
    read_chunk_size = max(1, settings.ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE)
    for pk in ids.iterator(chunk_size=read_chunk_size):
        batch.append(pk)
        if len(batch) == scheduler_batch_size:
            queue_index_sync(batch, using=using)
            batch = []
    queue_index_sync(batch, using=using)


@receiver(post_delete, sender=EntityItem, dispatch_uid="telegram_index_entity_delete")
def entity_deleted(sender, instance, using, **kwargs):
    """Queue removal of the deleted entity's Telegram count documents."""
    if _should_queue():
        queue_index_sync([instance.pk], operation="delete_entities", using=using)


@receiver(post_delete, sender=AnnotatedEntity, dispatch_uid="telegram_index_annotation_delete")
def annotation_deleted(sender, instance, using, origin=None, **kwargs):
    """Queue rebuilding a Telegram message after a direct annotation deletion.

    Cascades from deleting a MessageItem or EntityItem have a separate handler,
    so they do not enqueue duplicate per-annotation jobs.
    """
    if not _should_queue() or instance.object_id is None:
        return
    model = origin.model if isinstance(origin, QuerySet) else type(origin)
    if model is not AnnotatedEntity:  # Item/entity cascades schedule their own batches.
        return
    content_type = ContentType.objects.db_manager(using).get_for_model(MessageItem)
    if instance.content_type_id == content_type.pk:
        queue_index_sync([instance.object_id], using=using)
