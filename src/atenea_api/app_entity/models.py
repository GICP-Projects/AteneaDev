import unicodedata
from django.db import models
from django.utils import timezone
from app_base.models import BaseModel, GeneralEncoder, QueryItem
from app_entity.enums import EntityType, NERStatus
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

# ==================================================================
# 1.0 - Entity Item 
# ==================================================================
class EntityItem(BaseModel):
    query_items = GenericRelation(QueryItem)

    # To avoid the creation of multiple entities with minor differences
    # f.e: España -> espana_gpe
    unique_ent = models.CharField(max_length=2060, unique=True)

    name = models.CharField(max_length=2048)

    # TYPES (From Spacy: https://github.com/explosion/spaCy/blob/a89eae928340f66c954345c56346475f6597e786/spacy/glossary.py#L327)
    # ignored types = ['TIME', 'ORDINAL', 'CARDINAL', 'WORK  OF  ART', 'FAC']
    ORG = "ORG"
    GPE = "GPE"
    PER = "PER"
    EVENT = "EVENT"
    PRODUCT = "PRODUCT"
    NORP = "NORP"
    LAW = "LAW"
    LANGUAGE = "LANGUAGE"
    LOCATION = "LOCATION"
    MONEY = "MONEY"
    DATE = "DATE"
    MISC = "MISC"
    SPACY_TYPES = [
        (ORG, "Companies, agencies, institutions."),
        (GPE, "Geopolitical entity, i.e. countries, cities, states."),
        (PER, "People, including fictional."),
        (EVENT, "Named hurricanes, battles, wars, sports events, etc."),
        (PRODUCT, "Objects, vehicles, foods, etc. (Not services.)"),
        (NORP, "Nationalities or religious or political groups."),
        (LAW, "Named documents made into laws."),
        (LANGUAGE, "Any named language."),
        (LOCATION, "Non-GPE locations, mountain ranges, bodies of water."),
        (MONEY, "Monetary values, including unit."),
        (DATE, "Absolute or relative dates or periods."),
        (MISC, "Miscellaneous named entities.")
    ]
    
    # Custom entity types
    EMAIL = "EMAIL"
    URL = "URL"
    MENTION = "MENTION"
    HASHTAG = "HASHTAG"
    EMOJI = "EMOJI"
    WALLET_ETH = "WALLET_ETH"
    WALLET_BTC = "WALLET_BTC"
    WALLET_DASH = "WALLET_DASH"
    WALLET_XMR = "WALLET_XMR"

    CUSTOM_TYPES = [
        (EMAIL, "Any type of email."),
        (URL, "Any type of URL."),
        (MENTION, "Standard social network mentions (@name)."),
        (HASHTAG, "Standard social network hashtags (#hashtag)."),
        (EMOJI, "Any type of emoji."),
        (WALLET_ETH, "Ethereum (ETH) wallet address."),
        (WALLET_BTC, "Bitcoin (BTC) wallet address."),
        (WALLET_DASH, "Dash (DASH) wallet address."),
        (WALLET_XMR, "Monero (XMR) wallet address.")
    ]
    type = models.PositiveSmallIntegerField(choices=EntityType.choices)

    class Meta:
        ordering = ["name", "type"]
        indexes = [
            models.Index(fields=["type", "id"], name="entity_type_id_idx"),
        ]

    def __str__(self):
        return f"name: {self.name}, type: {self.type_name}"

    @property
    def type_name(self):
        return EntityType.to_name(self.type)

    @type_name.setter
    def type_name(self, value):
        self.type = EntityType.from_name(value)
    
    @classmethod
    def to_unique(cls, name, type):
        """Standarized an entity data to obtain a unique string

        This method is used to retrieve the same string despite receiving the same
        name with minor variations.

            - lowercase
            - removing spaces
            - removing any type of accent (ñ->n , (à,á,ä..)->a)
            - adding lowercased type

        F.e:
            Päblo Pérez
            PablOPerez
            Páblo perez
            unique_name = pabloperez

        Parameters
        ----------
        name: string
            Target name.

        type: string
            Target type.

        Returns
        -------
        unique_str: string
            Standarized string of an entity.
        """
        unique_str = (
            unicodedata.normalize("NFD", name.replace(" ", "").lower())
            .encode("ascii", "ignore")
            .decode("utf-8")
        )

        return f"{unique_str}_{EntityType.to_name(type).lower()}"


# ==================================================================
# 2.0 - Annotated Entity Item 
# ==================================================================
class AnnotatedEntity(BaseModel):
    """ A model to relate an item with text to its entities and metadata (offset).
    """
    entity = models.ForeignKey(EntityItem, on_delete=models.CASCADE, related_name="annotations")
    start_offset = models.PositiveIntegerField(null=True)  
    end_offset = models.PositiveIntegerField(null=True)

    # Foreign key to any item with an annotated_text field to relate.
    content_type = models.ForeignKey( # Stores the ModelClass content type of the item
        ContentType, on_delete=models.CASCADE, null=True
    )
    object_id = models.UUIDField(null=True) # Stores the primary key of the item
    content_object = GenericForeignKey("content_type", "object_id") 

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['entity', 'content_type', 'object_id', 'start_offset'], 
                name='unique_annotated_entity'
            )
        ]
        indexes = [
            # NER replacement deletes by the generic target, while the
            # uniqueness index starts with entity_id and cannot serve that
            # lookup efficiently.
            models.Index(
                fields=['content_type', 'object_id'],
                name='annotated_ct_object_idx',
            ),
        ]


# ==================================================================
# 3.0 - Durable NER state for any annotatable item
# ==================================================================
class NERState(models.Model):
    """Current NER ownership and result state for an annotatable object.

    It deliberately uses the same generic-object shape as ``AnnotatedEntity``:
    NER belongs to ``app_entity``, while Telegram is merely the first producer
    of text-bearing objects.  This is a current-state/claim ledger, not an
    event history; ``ProcessingJob`` and ``ProcessingBatch`` keep operational
    history separately. ``claimed_batch`` is durable ownership, whereas
    ``lease_until`` describes only a currently queued/running attempt.
    """

    CLAIMED = NERStatus.CLAIMED.value
    EXTRACTING = NERStatus.EXTRACTING.value
    EXTRACTED = NERStatus.EXTRACTED.value
    STORING = NERStatus.STORING.value
    STORED = NERStatus.STORED.value
    COMPLETED = NERStatus.COMPLETED.value
    RETRY_WAIT = NERStatus.RETRY_WAIT.value
    FAILED = NERStatus.FAILED.value
    STATUS_CHOICES = NERStatus.choices

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")
    processed_input_hash = models.CharField(max_length=64, blank=True, default="")
    claimed_input_hash = models.CharField(max_length=64, blank=True, default="")
    # SHA-256 hexadecimal fingerprint of the NER metadata and processing component versions
    ner_version = models.CharField(max_length=64, blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=CLAIMED)
    claimed_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_ner_states",
    )
    lease_until = models.DateTimeField(null=True, blank=True)
    retry_after = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    last_job = models.ForeignKey(
        "app_scheduler.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_ner_states",
    )
    last_batch = models.ForeignKey(
        "app_scheduler.ProcessingBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_ner_state_batches",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_ner_state_content_object",
            )
        ]
        indexes = [
            # Short, stable names keep the migration portable across supported
            # database backends.
            models.Index(fields=["status", "lease_until"], name="nerstate_status_lease_idx"),
            models.Index(fields=["ner_version", "processed_at"], name="nerstate_version_done_idx"),
        ]


class NERBatchResult(models.Model):
    """Temporary extraction output owned by the NER domain.

    Scheduler deliberately persists only generic workflow state.  This payload
    survives a storage retry (notably a PostgreSQL deadlock) and is deleted in
    the same successful transaction that stores annotations.
    """

    batch = models.OneToOneField(
        "app_scheduler.ProcessingBatch",
        on_delete=models.CASCADE,
        related_name="ner_result",
    )
    payload = models.JSONField(encoder=GeneralEncoder)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
