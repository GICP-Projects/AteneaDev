from django.db import models
from django.utils import timezone
from django.contrib.contenttypes.fields import GenericRelation, GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from app_base.models import BaseModel, QueryItem


# ==================================================================
# 0.0  Embeddings Item 
# ==================================================================
class EmbeddingsItem(BaseModel):
    STATUS_PENDING = "pending"
    STATUS_SYNCED = "synced"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SYNCED, "Synced"),
        (STATUS_FAILED, "Failed"),
    ]

    query_items = GenericRelation(QueryItem)

    model = models.CharField(max_length=256)
    version = models.CharField(max_length=20) # e.g: 1.0.0 | 1.0.0-alpha | 2.1 | N/A

    instruct = models.TextField(blank=True, null=True)
    collection_name = models.CharField(max_length=128, blank=True, null=True)
    point_id = models.CharField(max_length=64, blank=True, null=True, unique=True)
    vector_dim = models.PositiveIntegerField(blank=True, null=True)
    text_hash = models.CharField(max_length=64, blank=True, null=True)
    sync_status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING
    )
    sync_error = models.TextField(blank=True, null=True)
    synced_at = models.DateTimeField(blank=True, null=True)
    calculated_at = models.DateTimeField(default=timezone.now, editable=False, null=True)

# ==================================================================
# 0.1 Base Embed Item 
# ==================================================================
class BaseEmbedItem(BaseModel):
    """ Base model for all the EmbedItem models (CategoryItem, ...).
    """
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField()
    # auto_now does not trigger on update()/bulk_update() only in save()
    last_update = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ==================================================================
# 1.0 - Category Item 
# ==================================================================
class CategoryItem(BaseEmbedItem):
    query_items = GenericRelation(QueryItem)
    embeddings = models.OneToOneField(
        EmbeddingsItem,
        on_delete=models.SET_NULL, # Set null this field when embedding is deleted
        related_name="category",
        null=True
    )

# ==================================================================
# 1.1 - Similarity Category Item 
# ==================================================================
class SimilarityCategory(BaseModel):

    category = models.ForeignKey(
        CategoryItem,
        on_delete=models.CASCADE,
        related_name="similarities",
        null=True
    )
    similarity = models.FloatField(default=0)
    calculated_at = models.DateTimeField(default=timezone.now, editable=False, null=True)

    # Foreign key to any item with an list of categories field to relate.
    content_type = models.ForeignKey( # Stores the ModelClass content type of the item
        ContentType, on_delete=models.CASCADE, null=True
    )
    object_id = models.UUIDField(null=True) # Stores the primary key of the item
    content_object = GenericForeignKey("content_type", "object_id") 

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['category','content_type', 'object_id'], 
                name='unique_similarity_category'
            )
        ]
