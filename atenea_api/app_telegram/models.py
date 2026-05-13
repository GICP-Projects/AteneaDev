from django.db import models
from django.utils import timezone
from django.core.validators import URLValidator
from django.contrib.postgres.fields import ArrayField
from django.contrib.contenttypes.fields import GenericRelation
from phonenumber_field.modelfields import PhoneNumberField
from app_base.models import BaseModel, QueryItem, LanguageField
from app_entity.models import AnnotatedEntity
from app_metadata.models import EmbeddingsItem, SimilarityCategory


# ==================================================================
# 0.0 - Telegram Seed Item (pre-<Channels/Groups/Users/Bots>)
# ==================================================================
class SeedItem(BaseModel):
    """ Seed item (Chat, Group, Bot or User)
        New items will be stored here until the scheduled task with the Telegram's 
        API extracts all the desired information and the Seeditem is transformed 
        to a 'complet' item (RoomItem, UserItem). After that, the is_seeded flag 
        will be turned to true.
    """
    query_items = GenericRelation(QueryItem)

    # SEED TYPES IN TELEGRAM
    GROUP_TYPE = "GROUP"
    CHANNEL_TYPE = "CHANNEL"
    BOT_TYPE = "BOT"
    USER_TYPE = "USER"
    TYPE_CHOICES = [
        (GROUP_TYPE, "Telegram Group"), 
        (CHANNEL_TYPE, "Telegram Channel"), 
        (BOT_TYPE, "Telegram Bot"),
        (USER_TYPE, "Telegram User")
    ]

    link = models.URLField(
        validators=[URLValidator()],
        max_length=256,
        unique=True,
        help_text=(
            "The link of the Telegram resource (Channel/Group/User/Bot). It must "
            "be a valid Telegram link (t.me/...)."
        )
    )
    title = models.CharField(
        max_length=256, 
        blank=True, 
        null=True,
        help_text=(
            "A custom title for the resource. It will only be used here to identify "
            "the seeds."
        )
    )
    tags = ArrayField(
        models.CharField(max_length=64), 
        blank=True, 
        default=list,
        help_text="List of tags to organize the seeds."
    ) 
    type = models.CharField(max_length=16, choices=TYPE_CHOICES, null=True)
    lang = LanguageField(blank=True, null=True)

    is_seeded = models.BooleanField(
        default=False,
        help_text=(
            "Indicates if the seed has been already populated or not. This field "
            "is used to avoid populating the same seed multiple times."
        )
    )
    collected_at = models.DateTimeField(
        default=timezone.now, 
        editable=False,
        help_text="Timestamp from which this seed has been collected."
    )

    # Handle Seeds with errors (bad link)
    is_valid = models.BooleanField(
        default=True, 
        help_text=(
            "Indicates if the seed is valid or not. If the seed has errors (wrong "
            "or invalid `link`) or doesn't correspond to a Group/Channel/Bot/User it "
            "will be set to False."
        )
    )


# ==================================================================
# 1.0 - Telegram Keys Item (SuperGroups/Groups/Channels)
# ==================================================================
class TelegramAuth(BaseModel):
    name = models.CharField(max_length=32, unique=True)
    phone = PhoneNumberField(blank=True)
    
    # Auth required info
    api_id = models.PositiveBigIntegerField()
    api_hash = models.CharField(max_length=255)
    session = models.TextField()

    is_valid = models.BooleanField(
        default=True,
        help_text=(
            "Indicates if the Telegram API credential is valid or not. If the Telegram "
            "API credential has errors (wrong `api_id`, `api_hash` or broken session) it "
            "will be set to False. Important: Even if it is false, all the related access_hash "
            "will still be related to this credential until is deleted or hibernated."
        )
    )

    is_hibernated = models.BooleanField(
        default=False,
        help_text=(
            "Indicates if the Telegram API credential has been hibernated or not. "
            "This flag allows to avoid deleting the credential but it will be invalidated "
            "and all the related access_hash will be deleted (they must be re-calculated "
            "with another credential)."
        )
    )

    # Number of times this api-key was used 
    counter = models.PositiveIntegerField(default=0)

    # Timestamp from which this credential becomes available
    wait_until = models.DateTimeField(
        default=timezone.now,
        help_text=(
            "Timestamp from which this credential becomes available for making "
            "ResolveUsername requests (e.g. get_input_entity or get_entity to "
            "extract the access_hash from a user/group/channel). "
            "Scanning of new messages will still be allowed."
        )
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['api_id','api_hash'], 
                # Why not `session`: Although you can create more than one `session` 
                # token, they are linked (e.g. sharing rate limit)
                name="unique_keys"
            )
        ]


# ==================================================================
# 2.0 - Telegram Base Entity Item (SuperGroups/Groups/Channels)
# ==================================================================
class BaseTelegramEntity(BaseModel):
    """ Base model for all Telegram entities (Users/Bots/Channels/Groups).
    This model is used to add common fields.

    The related_name for this model will be: `model_class_name (in lowercase) + s_related`
    e.g: roomitems_related  or useritems_related
    """
    query_items = GenericRelation(QueryItem)

    # Common main info
    tg_id = models.BigIntegerField(unique=True) # https://stackoverflow.com/questions/52344466/telethon-ids-uniqueness
    unique_name = models.CharField(max_length=256, blank=True, null=True)
    about = models.TextField(blank=True, null=True)

    # Info form SeedItem
    tags = ArrayField(models.CharField(max_length=64), blank=True, default=list) 
    lang = LanguageField(blank=True, null=True) 
    seed_item = models.ForeignKey(
        SeedItem,
        on_delete=models.CASCADE,
        related_name="%(class)ss_related", # e.g roomitems_related | %(class)s = roomitem 
        null=True
    )

    # Handle entity access 
    # tg_id + access_hash avoids expensive `get_entity` requests. https://stackoverflow.com/a/75087110
    access_hash = models.BigIntegerField(
        default=None, 
        null=True,
        help_text=(
            "Telegram API access hash. This hash is used to retrieve the entity "
            "from the Telegram API. It is generated by the Telegram API and "
            "depends on the Telegram credential `access_auth`."
        )
    ) 
    access_auth = models.ForeignKey(
        TelegramAuth,
        on_delete=models.SET_NULL,
        related_name="%(class)ss_related", # e.g roomitems_related | %(class)s = roomitem 
        null=True,
        help_text=(
            "Telegram API credential used to extract the entity. The `access_hash` "
            "field depends on this credential."
        )
    )
    # if TelegramAuth Model launches a pre-delete signal, the `is_valid` will be set to False (see `signal.py`)
    is_valid = models.BooleanField(
        default=True, 
        help_text=(
            "Indicates if the entity can be accesed from the API. If the entity "
            "has errors (wrong `tg_id`, `access_hash` or no credentials related "
            "to it) it will be set to False."
        )
    )

    is_deleted = models.BooleanField(
        default=False, 
        help_text=(
            "Indicates if the entity has been deleted or suspended. This indicator "
            "avoids trying to process unreachable entities."
        )
    )

    class Meta:
        abstract = True


# ==================================================================
# 2.1 - Telegram entity Room Item (SuperGroups/Groups/Channels)
# ==================================================================
class RoomItem(BaseTelegramEntity):

    # Room main info
    link = models.URLField(
        validators=[URLValidator()],
        max_length=2048,
        unique=True,
    )
    unique_name = models.CharField(max_length=256, blank=True, null=True) # If None = Private Channel and link = invitiation
    title = models.CharField(max_length=256, blank=True)

    # Room meta-info
    is_channel =  models.BooleanField(
        default=False,
        help_text=(
            "Indicates whether the room is a channel or a group. Channels are used for "
            "broadcasting messages to large audiences, where only admins can post. Groups "
            "are for conversation among members, where all members can participate."
        )
    )
    created_at = models.DateTimeField(null=True)
    last_offset = models.IntegerField(default=0) # Last offset is the last message.id
    
    # auto_now does not trigger on update()/bulk_update() only in save()
    last_offset_update = models.DateTimeField(
        default=None,
        null=True,
        help_text=(
            "Datetime of the last time a new message was scanned for this room. "
            "if None, the room wasn't scanned."
        )
    )
    last_update = models.DateTimeField(
        auto_now=True,
        help_text=(
            "Datetime of the last time any field of the room has been updated."
        )
    )
    last_scan_at = models.DateTimeField(
        default=None,
        null=True,
        help_text=(
            "Datetime of the last time this room was scanned. "
            "if None, the room wasn't scanned."
        )
    )

    is_private = models.BooleanField(
        default=False,
        help_text=(
            "Indicates if the group/channel is private. Private groups can only be "
            "accessed by invite link, while public groups can be accessed by anyone."
            "(Another reason may be that the credential was banned from it)"
        )
    )

    # Allow/disallow scan
    following = models.BooleanField(
        default=True,
        help_text=(
            "Indicates if the group/channel wants to be scanned. Allows manual deselecting "
            "of those that are no longer of interest"
        )   
    )


# ==================================================================
# 2.2 - Telegram entity User Item (Bots/Users)
# ==================================================================
class UserItem(BaseTelegramEntity):

    # User/Bot main info
    unique_name = models.CharField(max_length=64, null=True)
    first_name = models.CharField(max_length=64, blank=True, null=True)
    last_name = models.CharField(max_length=64, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)

    # User/Bot meta-info
    rooms = models.ManyToManyField(
            RoomItem, related_name="members"
    )   

    is_admin = models.BooleanField(default=False)
    is_bot =  models.BooleanField(default=False)
    is_scam = models.BooleanField(default=False)


# ==================================================================
# 3.0 - Telegram Message Item 
# ==================================================================
class MessageItem(BaseModel):
    query_items = GenericRelation(QueryItem)

    link = models.URLField( # Unique -> t.me/room/msg_id
        validators=[URLValidator()],
        max_length=2048,
        unique=True,
        help_text=(
            "The link of the message. It must be a valid Telegram link (t.me/...). "
            "If the message is a reply and the room is a channel, the `msg_id` works "
            "differently and appears in the link in the format: "
            "`https://t.me/<room.unique_name>/<reply_to_msg.msg_id>?comment=<msg_id>`"
        )
    )
    msg_id = models.BigIntegerField() # msg id is only unique inside each room
    room = models.ForeignKey(
        RoomItem,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    # Many times user can't be returned by the room so its UserItem will not exist
    # this is a simplest aproach to keep saving the authority of the message
    sender = models.BigIntegerField(default=None, null=True) 
    #sender = models.ForeignKey(
    #    UserItem,
    #    on_delete=models.CASCADE,
    #    related_name="messages",
    #)

    # MEDIA TYPES IN TELEGRAM MESSAGE
    DOC = "DOCUMENT"
    WEB_PAGE = "WEBPAGE"
    PHOTO = "PHOTO"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    GIF = "GIF"
    CONTACT = "CONTACT"
    GEO = "GEO"
    TEXT = "ONLY_TEXT"
    OTHER = "OTHER"
    TYPE_CHOICES = [
        (WEB_PAGE, "Web page"), 
        (DOC, "Document media"), 
        (PHOTO, "Photo media"),
        (VIDEO, "Video media"),
        (AUDIO, "Audio media"),
        (GIF, "GIF media"),
        (CONTACT, "Contact media"),
        (GEO, "Geolocation"),
        (TEXT, "Only text"),
        (OTHER, "Other"),
    ]
    media_type = models.CharField(max_length=16, choices=TYPE_CHOICES)

    text = models.TextField(blank=True, null=True)
    
    # Contains a cleaned version of the text. Entities start/end offsets point to
    # this field (That's why the fieldname is 'annotated_text')
    annotated_text = models.TextField(
        blank=True, 
        null=True,
        help_text=(
            "Contains a cleaned version of the text. Entities start/end "
            "offsets point to this field (That's why it is 'annotated'"
        )
    )

    entities = GenericRelation(AnnotatedEntity)

    # EMBEDDINGS
    embeddings = models.OneToOneField(
        EmbeddingsItem,
        on_delete=models.SET_NULL, # Set null this field when embedding is deleted,
        related_name="message_item",
        null=True
    )
    cat_embeddings = models.OneToOneField(
        EmbeddingsItem, # Embeddings with the task to detect its categories
        on_delete=models.SET_NULL, # Set null this field when embedding is deleted,
        related_name="message_cat_item",
        null=True
    ) 

    # Metadata
    categories = GenericRelation(SimilarityCategory)
    SENTIMENT_CHOICES = [
        (0, 'negative'),
        (1, 'neutral'),
        (2, 'positive'),
    ]
    sentiment = models.SmallIntegerField(
        choices=SENTIMENT_CHOICES, 
        null=True, 
        default=None
    )
    sentiment_model = models.CharField(max_length=256, null=True, default=None)

    # Extra info
    created_at = models.DateTimeField(null=True)
    stored_date = models.DateTimeField(default=timezone.now, editable=False, null=True)
    lang = LanguageField(blank=True) 
    views = models.IntegerField(
        default=None, 
        null=True,
        help_text=(
            "Number of views of the message. Only available for message from channels."
        )
    )

    # Father message info
    is_reply = models.BooleanField(
        default=False,
        help_text=(
            "Indicates whether the message is a reply to another message. "
            "If the message is a reply and the room is a channel, the `msg_id` works "
            "differently and appears in the link in the format: "
            "`https://t.me/<room.unique_name>/<reply_to_msg.msg_id>?comment=<msg_id>`"
        )
    )
    reply_to_id = models.BigIntegerField(
        default=None, 
        null=True,
        help_text="The message id which is replied to." 
    )
    reply_to_msg = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="replies",
        null=True,
        help_text=(
            "The message which is replied to. This foreign key will only be available "
            "for replies to messages in channels, not groups."
        )
    )

    # Extract responses info
    is_valid = models.BooleanField(
        default=True, 
        help_text=(
            "Indicates if the message was accessible through the API (e.g., for responses). "
            "Set to False if the message was deleted, can no longer be accessed, "
            " or lacked a comments/replies section."
        )
    )
    last_offset = models.IntegerField(default=0) # Last offset is the last message.id
    last_offset_update = models.DateTimeField(
        default=None,
        null=True,
        help_text=(
            "Datetime of the last time a new reply was scanned for this message. "
            "if None, the message wasn't scanned."
        )
    )
    last_scan_at = models.DateTimeField(
        default=None,
        null=True,
        help_text=(
            "Datetime of the last time this message was scanned. "
            "if None, the message wasn't scanned."
        )
    )

    class Meta:
        indexes = [
            models.Index(fields=['created_at', 'id']),
        ]