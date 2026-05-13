from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import CategoryItem

@receiver(post_save, sender=CategoryItem)
def trigger_embedding_calculation(sender, instance, created, **kwargs):
    """ Trigger embeddings calculation when a CategoryItem is created or updated.
    """
    from app_metadata.services.embeddings import run_embeddings
    run_embeddings.delay(
        kwargs.get("token"), 
        instance._meta.model_name, 
        instance._meta.app_label,
        "description",
        "embeddings",
        and_filter_fields={"pk__in": [instance.pk]},
        minimum_text_length=1
    )