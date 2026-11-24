from django.db import models
from django.utils import timezone
from django.contrib.contenttypes.fields import GenericRelation, GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from app_base.models import BaseModel, QueryItem
from app_metadata.enums import EmbeddingSyncStatus, MetadataProcessingStatus


# ==================================================================
# 0.0  Embeddings Item 
# ==================================================================
class EmbeddingsItem(BaseModel):
    STATUS_PENDING = EmbeddingSyncStatus.PENDING.value
    STATUS_SYNCED = EmbeddingSyncStatus.SYNCED.value
    STATUS_FAILED = EmbeddingSyncStatus.FAILED.value
    STATUS_CHOICES = EmbeddingSyncStatus.choices

    PROCESSING_PENDING = MetadataProcessingStatus.PENDING.value
    PROCESSING_CLAIMED = MetadataProcessingStatus.CLAIMED.value
    PROCESSING_RUNNING = MetadataProcessingStatus.RUNNING.value
    PROCESSING_RETRY_WAIT = MetadataProcessingStatus.RETRY_WAIT.value
    PROCESSING_COMPLETED = MetadataProcessingStatus.COMPLETED.value
    PROCESSING_FAILED = MetadataProcessingStatus.FAILED.value
    PROCESSING_STATUS_CHOICES = MetadataProcessingStatus.choices

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
    # ``sync_status`` remains Qdrant-specific.  These fields are the durable
    # job ownership ledger and deliberately live with the embeddings domain.
    processing_status = models.CharField(
        max_length=16,
        choices=PROCESSING_STATUS_CHOICES,
        default=PROCESSING_PENDING,
    )
    claimed_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_embedding_states",
    )
    lease_until = models.DateTimeField(blank=True, null=True)
    retry_after = models.DateTimeField(blank=True, null=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    processing_error = models.TextField(blank=True, default="")
    last_job = models.ForeignKey(
        "app_scheduler.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_embedding_states",
    )
    last_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_embedding_state_batches",
    )

    class Meta:
        indexes = [
            models.Index(fields=["processing_status", "lease_until"], name="embedstate_status_lease_idx"),
        ]


class SentimentState(models.Model):
    """Current durable-processing state for a text item's sentiment.

    It mirrors ``NERState`` instead of placing message state in
    ``app_telegram``: sentiment is metadata-domain work and may later attach to
    non-Telegram text objects as well.
    """

    PENDING = MetadataProcessingStatus.PENDING.value
    CLAIMED = MetadataProcessingStatus.CLAIMED.value
    RUNNING = MetadataProcessingStatus.RUNNING.value
    RETRY_WAIT = MetadataProcessingStatus.RETRY_WAIT.value
    COMPLETED = MetadataProcessingStatus.COMPLETED.value
    FAILED = MetadataProcessingStatus.FAILED.value
    STATUS_CHOICES = MetadataProcessingStatus.choices

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")
    processed_input_hash = models.CharField(max_length=64, blank=True, default="")
    claimed_input_hash = models.CharField(max_length=64, blank=True, default="")
    sentiment_version = models.CharField(max_length=256, blank=True, default="")
    processed_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    claimed_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_sentiment_states",
    )
    lease_until = models.DateTimeField(blank=True, null=True)
    retry_after = models.DateTimeField(blank=True, null=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    last_job = models.ForeignKey(
        "app_scheduler.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_sentiment_states",
    )
    last_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_sentiment_state_batches",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_sentiment_state_content_object",
            )
        ]
        indexes = [
            models.Index(fields=["status", "lease_until"], name="sentstate_status_lease_idx"),
            models.Index(fields=["sentiment_version", "processed_at"], name="sentstate_version_done_idx"),
        ]

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
