import unicodedata
from django.db import models
from app_base.models import BaseModel, QueryItem
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
        (DATE, "Absolute or relative dates or periods.")
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
    type = models.CharField(max_length=15, choices=SPACY_TYPES+CUSTOM_TYPES)

    class Meta:
        ordering = ["name", "type"]

    def __str__(self):
        return f"name: {self.name}, type: {self.type}"
    
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
        name = name

        unique_str = (
            unicodedata.normalize("NFD", name.replace(" ", "").lower())
            .encode("ascii", "ignore")
            .decode("utf-8")
        )

        return f"{unique_str}_{type.lower()}"


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
