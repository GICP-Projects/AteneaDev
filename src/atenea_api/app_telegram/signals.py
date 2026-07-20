from django.dispatch import receiver
from django.db.models.signals import pre_delete, pre_save
from app_telegram.models import TelegramAuth, RoomItem, UserItem
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)


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