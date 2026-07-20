import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _


# local imports
from .managers import CustomUserManager


class User(AbstractUser):
    """basic user model with email and name"""
    id = models.UUIDField(default=uuid.uuid4, primary_key=True)
    email = models.EmailField(("email address"), unique=True)
    username = models.CharField(
        max_length=25,
        help_text=_(
            "Required. 25 characters or fewer. Letters, digits and @/./+/-/_ only."
        ),
        validators=[AbstractUser.username_validator],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username",]

    objects = CustomUserManager()

    def __str__(self):
        return self.email


class Profile(models.Model):
    """public user profile with avatar, bio and such"""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    phone = models.CharField(max_length=15, default="", blank=True)
    is_verified = models.BooleanField(default=False)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """creates a new user profile when user is created"""
    if created:
        Profile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """saves user profile when user is saved"""
    instance.profile.save()
