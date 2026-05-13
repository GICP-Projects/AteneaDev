from django.apps import AppConfig


class AppMetadataConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'app_metadata'

    def ready(self):
        # Register signal handlers
        import app_metadata.signals 