from django.apps import AppConfig


class AppTelegramConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'app_telegram'

    def ready(self):
        # Register credential and message-index synchronization handlers.
        from app_telegram import signals  # noqa: F401
