from datetime import datetime
from django.conf import settings
from django.utils import timezone
from rest_framework import serializers
from app_base.languages import LANGS_ISO_639_1
from django_elasticsearch_dsl_drf.serializers import DocumentSerializer
from app_base.serializers import BaseDateRangeSerializer, BulkListSerializer
from app_telegram.utils import telegram_link_normalizer
from app_telegram.models import SeedItem, TelegramAuth, MessageItem
from app_metadata.serializers import EmbeddingSerializer
from app_telegram.documents import RoomDocument, MessageDocument

# GLOBAL VARIABLES
ALL = "all"
ANY = "any"
MATCH_CHOICES = [ALL, ANY]

# ==================================================================
# 01.0 - Telegram SeedItem Serializers
# ==================================================================

class ListSeedSerializer(BulkListSerializer):
    """ Custom Bulk List serializer to handle SeedItem.
    """
    def get_unique_field_name(self):
        return "link"

class SeedItemSerializer(serializers.ModelSerializer):
    """ Serializer to handle SeedItem.

    In case of using the serializer to update an existing item, the `link` field 
    will be read_only. (partial=True or instance=... must be set)

    In case of `many=True` the serializer will return a dict with the following 
    structure:
    ```
    {
        "items": [
            { ... },
            { ... },
            ...
        ],
        "invalid_items": {
            "<link>": ["<error_message1>"],
        }
    }
    ``` 
    Check ListSeedSerializer for more details.
    """
    tags = serializers.ListField(
        child=serializers.CharField(max_length=64),
        allow_empty=True,
        required=False, 
        max_length=20 
    )

    def __init__(self, instance=None, data=..., **kwargs):
        # If instance is set (or partial=True), it means that the serializer is 
        # being used to update an existing item. In this case, the `link` can't 
        # change.
        if instance or kwargs.get("partial", False):
            self.fields["link"].read_only = True

        super().__init__(instance, data, **kwargs)

    def to_internal_value(self, data):
        # If 'link' is not in the data it must be handled in the field validation.
        if "link" in data:
            # Normalize the telegram resource link to make it unique
            link = telegram_link_normalizer(data["link"], rebuild_link=True)
            if not link:
                raise serializers.ValidationError({
                    "link": [f"Are you sure that the link '{data['link']}' points "
                    "to a valid telegram resource (Channel/Group/Bot/User)?"]
                })
            data["link"] = link
        # Validate all the data
        return super().to_internal_value(data)
   
    class Meta:
        model = SeedItem
        fields = [
            "id",
            "link",
            "title",
            "tags",
            "type",
            "lang",
            "is_seeded",
            "collected_at",
            "is_valid",
        ]
        read_only_fields = ["is_seeded", "is_valid"] #, 'collected_at'] (not necessary, is already a editable=False field) 
        list_serializer_class = ListSeedSerializer


# ==================================================================
# 01.1 - Telegram SeedItem filter Serializer
# ==================================================================

class SimpleFilterSeedSerializer(BaseDateRangeSerializer):
    """ A serializer to filter SeedItem items.
    """
    by_resource = serializers.CharField(
        max_length=128, 
        required=False,
        help_text=(
            "Check if the seed link contains the given resource."
        )
    )
    by_title = serializers.CharField(
        max_length=128, 
        required=False,
        help_text=(
            "Check if the seed title contains the given title."
        )
    )
    tag_match = serializers.ChoiceField(
        MATCH_CHOICES,
        required=False,
        default=ANY,
        help_text=(
            "Determines if items should match all given tags or any "
            "of them. Default is 'any'."
        )
    )
    # List field: tag=...&tag=...tag=...
    tag = serializers.ListField(
        child=serializers.CharField(max_length=64, allow_blank=False),
        required=False, 
        max_length=20,
        help_text=(
            "The tag parameter accepts a list of tags (e.g., tag=value1&tag=value2). "
            "The category matching is case-insensitive and does not require "
            "an exact match, as it uses a substring search. The list is "
            "processed using an OR (by default) or AND logic (depends on "
            "cat_match parameter)."
        )
    )
    lang = serializers.ListField(
        child=serializers.ChoiceField(
            required=False, 
            choices=LANGS_ISO_639_1
        ),
        required=False, 
        help_text=(
            "Language codes in ISO 639-1 format."
        )
    )
    type = serializers.ListField(
        child=serializers.ChoiceField(required=False, choices=[type[0] for type in SeedItem.TYPE_CHOICES]),
        required=False, 
        max_length=4,
        help_text=(
            "The 'type' parameter accepts a list of media types (e.g., "
            "type=value1&type=value2) to filter which message will be processed."
        )
    )

    # Date range params
    collected_min = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval start date, to filter for any element that has been "
            "collected since the set date. date.now() >= collected-min. "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )
    collected_max = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval end date, to filter for any element that has been "
            "collected until the set date. collected-max >= date.now(). "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )

    def get_start_field_name(self):
        return "collected_min"

    def get_end_field_name(self):
        return "collected_max"

class FullFilterSeedSerializer(SimpleFilterSeedSerializer):
    """ A more complete serializer to filter SeedItem items.
    """
    is_valid = serializers.BooleanField(
        required=False,
        help_text=(
            "Filter only valid or invalid seeds. Default: None."
        )
    )
    is_seeded = serializers.BooleanField(
        required=False,
        help_text=(
            "Filter only seeded or unseeded seeds. Default: None."
        )
    )


# ==================================================================
# 01.2 - [REQUEST] Telegram Room Serializers
# ==================================================================

class FilterRoomSerializer(BaseDateRangeSerializer):
    """ These variable names must match the arguments of their respective 
    function in api.py, which will process the request.
    """

    room = serializers.ListField(
        child=serializers.CharField(max_length=128, allow_blank=False),
        required=False, 
        max_length=20,
        help_text=(
            "The 'room' parameter accepts a list of Telegram channels/groups "
            "names to filter which rooms will be scanned."
        ),
    )

    tag_match = serializers.ChoiceField(
        MATCH_CHOICES,
        required=False,
        default=ANY,
        help_text=(
            "Determines if items should match all given tags or any "
            "of them. Default is 'any'."
        )
    )
    # List field: tag=...&tag=...tag=...
    tag = serializers.ListField(
        child=serializers.CharField(max_length=64, allow_blank=False),
        required=False, 
        max_length=20,
        help_text=(
            "The tag parameter accepts a list of tags (e.g., tag=value1&tag=value2). "
            "The category matching is case-insensitive and does not require "
            "an exact match, as it uses a substring search. The list is "
            "processed using an OR (by default) or AND logic (depends on "
            "cat_match parameter)."
        )
    )
    lang = serializers.ListField(
        child=serializers.ChoiceField(
            required=False, 
            # Only allow language codes with two chars
            choices=LANGS_ISO_639_1
        ),
        required=False, 
        help_text=(
            "Language codes in ISO 639-1 format."
        )
    )
    is_channel = serializers.BooleanField(
        required=False,
        help_text=(
            "To filter only Channels (True), Groups (False) or any (None). Default=None."
        )
    )

    # Date range params
    lastup_min = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval start date, to filter for any Channel/Group that "
            "has had any update (scan) since the set date. date.now() >= lastup-min. "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )
    lastup_max = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval end date, to filter for any Channel/Group that "
            "has had any update (scan) until the set date. lastup-max >= date.now(). "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )

    def get_start_field_name(self):
        return "lastup_min"

    def get_end_field_name(self):
        return "lastup_max"


class ScanRoomSerializer(FilterRoomSerializer):
    max_msgs = serializers.IntegerField(
        required=False, 
        min_value=10, 
        max_value=50000,
        help_text="Max number of messages they will be extracted per room."
    )
    update_users = serializers.BooleanField(
        required=False,
        help_text=(
            "A flag used to determine whether users extracted from "
            "scanning Telegram channels that already exist on the platform "
            "should be updated in the database. It is False by default."
        )

    )


# ==================================================================
# 01.3 - [REQUEST] Telegram Msg Serializers
# ==================================================================

class SimpleMessageFilterSerializer(BaseDateRangeSerializer):

    room = serializers.ListField(
        child=serializers.CharField(max_length=128, allow_blank=False),
        required=False, 
        max_length=20,
        help_text=(
            "The 'room' parameter accepts a list of Telegram channels/groups "
            "names to filter from which rooms their messages will be processed."
        )
    )

    is_reply = serializers.BooleanField(
        required=False,
        help_text=(
            "Filters messages that are replies to another message."
        )
    )

    # Stored date filter
    stored_since = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval start date, to filter for any message that "
            "has been stored in the platform since the set date. "
            "date.now() >= stored-since. "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )

    # Date range params
    createdat_min = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval start date, to filter for any message that "
            "has been created since the set date. date.now() >= createdat-min. "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )
    createdat_max = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval end date, to filter for any message that "
            "has been created until the set date. createdat-max >= date.now(). "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )

    def get_start_field_name(self):
        return "createdat_min"

    def get_end_field_name(self):
        return "createdat_max"

    def validate_stored_since(self, value):
        # Make aware
        dt_value = datetime.combine(value, datetime.min.time()) 
        return timezone.make_aware(dt_value)

class FilterMsgSerializer(SimpleMessageFilterSerializer):

    MEDIA_TYPES = [type[0] for type in MessageItem.TYPE_CHOICES]
    type = serializers.ListField(
        child=serializers.ChoiceField(required=False, choices=MEDIA_TYPES),
        required=False, 
        help_text=(
            "The 'type' parameter accepts a list of media types (e.g., "
            "type=value1&type=value2) to filter which message will be processed."
        )
    )

    lang = serializers.ListField(
        child=serializers.ChoiceField(
            required=False, 
            # Only allow language codes with two chars
            choices=LANGS_ISO_639_1
        ),
        required=False, 
        help_text=(
            "Language codes in ISO 639-1 format."
        )
    )

    block_size = serializers.IntegerField(
        required=False, 
        min_value=10000, 
        max_value=1000000,
        help_text=(
            "The size of each block of items to be processed in memory from "
            "the DB at a time. It directly affects RAM usage"
        )
    )

    # Stored date filter
    stored_since = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Interval start date, to filter for any message that "
            "has been stored in the platform since the set date. "
            "date.now() >= stored-since. "
            f"Default: disabled. Format: {settings.FORMAT_DATE}."
        )
    )
    
class EmbedFilterMsgSerializer(FilterMsgSerializer):
    instruct = serializers.CharField(
        max_length=128, 
        required=False,
        help_text=(
            "Guide the behaviour of the model to follow specific instructions "
            "provided, influencing tone, style, and content. Default: Empty"
        )
    )

    DEFT = "default"
    CAT = "category"
    SLOT_CHOICES = [DEFT, CAT]
    slot = serializers.ChoiceField(
        SLOT_CHOICES,
        required=False,
        default=DEFT,
        help_text=(
            "The message structure contains different slots (fields) to store "
            "the embeddings, each one for a specific task. e.g: Categorisation (category), or any other task (default)."
        )
    )

    refresh = serializers.BooleanField(
        default=False, 
        required=False,
        help_text=(
            "Refresh existing embeddings. Default=False."
        )
    )

class ScanRepliesMsgSerializer(FilterMsgSerializer):

    tag_match = serializers.ChoiceField(
        MATCH_CHOICES,
        required=False,
        default=ANY,
        help_text=(
            "Determines if items should match all given tags or any "
            "of them. Default is 'any'."
        )
    )
    # List field: tag=...&tag=...tag=...
    tag = serializers.ListField(
        child=serializers.CharField(max_length=64, allow_blank=False),
        required=False, 
        max_length=20,
        help_text=(
            "The tag parameter accepts a list of tags (e.g., tag=value1&tag=value2). "
            "The category matching is case-insensitive and does not require "
            "an exact match, as it uses a substring search. The list is "
            "processed using an OR (by default) or AND logic (depends on "
            "cat_match parameter)."
        )
    )   

    max_msgs = serializers.IntegerField(
        required=False, 
        min_value=10, 
        max_value=5000,
        help_text="Max number of message replies they will be extracted per message."
    ) 

    total_msgs = serializers.IntegerField(
        required=False, 
        default=1250000,
        min_value=10000, 
        max_value=1250000,
        help_text=(
            "Total number of message replies to be extracted globally. Caution: "
            "Setting a higher value may lead to significant resource consumption. "
            "Use with caution to avoid overloading the system. Default=1.250.000"
        )
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remove the block_size field
        self.fields.pop("block_size", None)


# ==================================================================
# 01.4 - [RESPONSE] Telegram Msg Serializers
# ==================================================================

class MessageDetailSerializer(serializers.ModelSerializer):
    room = serializers.CharField(source='room.unique_name')

    class Meta:
        model = MessageItem
        fields = [
            'link', 'msg_id', 'room', 'sender', 'media_type', 'text', 
            'created_at', 'lang', 'views', 'is_reply', 'reply_to_id'
        ]

class MessageVectorSerializer(serializers.ModelSerializer):
    room = serializers.CharField(source='room.unique_name')
    embeddings = EmbeddingSerializer(read_only=True)

    class Meta:
        model = MessageItem
        fields = ['link', 'room', 'created_at', 'embeddings']


# ==================================================================
# 02.0 - [REQUEST] Telegram Search Serializers
# ==================================================================

class MessageAISearchSerializer(serializers.Serializer):
    q = serializers.CharField(
        required=True,
        help_text="Search query."
    )
    instruct = serializers.CharField(
        required=False,
        help_text=(
            "Guide the behaviour of the model to follow specific instructions "
            "provided, influencing tone, style, and content. Default instruct "
            "contains a standard behaviour."
        )
    )
    empty = serializers.BooleanField(
        default=False, 
        required=False,
        help_text=(
            "If instruct is not set and empty=True, the instruct will not "
            "be used (instead of use a default prompt)."
        )
    )


# ==================================================================
# 02.1 - Telegram Search Serializers
# ==================================================================

class RoomDocumentSerializer(DocumentSerializer):
    class Meta:
        document = RoomDocument

        fields = (
            'link',
            'title',
            'about',
            'tags',
            'lang'
        )


class MessageDocumentSerializer(DocumentSerializer):
    link = serializers.SerializerMethodField()
    #def __init__(self, *args, **kwargs):
    #    self._field_mapping[AnnotatedTextField] = CharField
    #    super().__init__(*args, **kwargs)

    class Meta:
        document = MessageDocument

        fields = (
            'link',
            #'annotated_text',
            'text',
            'tags',
            'entities',
            'media_type',
            'created_at',
            'views',
            'is_reply',
        )

    def get_link(self, obj: MessageDocument):
        # Rebuild the link on the fly.
        # Note: 'obj' here is the result from Elasticsearch (a hit object), not the Django model.
        # It's not a MessageDocument but shares the same structure.
        try:
            return f"https://t.me/{obj.room_name}/{obj.msg_id}"
        except AttributeError:
            return None


# ==================================================================
# 03.0 - [REQUEST] Create/Update a TelegramAuth items
# ==================================================================

class TelegramAuthSerializer(serializers.ModelSerializer):
    session = serializers.CharField(write_only=True)
    class Meta:
        model = TelegramAuth
        fields = '__all__'
        read_only_fields = ['counter', 'wait_until']
        
# ==================================================================
# 03.1 - [REQUEST] Filters to retrieve TelegramAuth items
# ==================================================================

class FilterTelegramAuthSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=128, 
        required=False,
        help_text="Filter TelegramAuth items by the name."
    )