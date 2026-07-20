from django.conf import settings
from django.db.models import Prefetch
from django.contrib.contenttypes.models import ContentType
from django_elasticsearch_dsl import Document, fields
from django_elasticsearch_dsl.registries import registry
from app_base.documents import lc_normalizer
from app_base.api import create_advance_filter
from app_telegram.models import RoomItem, MessageItem, UserItem
from app_entity.models import EntityItem, AnnotatedEntity


@registry.register_document
class RoomDocument(Document):
    """
    Nested vs flat: https://discuss.elastic.co/t/disadvantages-of-using-nested-type-over-flat-type-mapping/159498/3
    Denormalize relation data: https://discuss.elastic.co/t/doubt-related-using-multiple-indexes-or-join/182692/2
    
    To allow auto-completion field: https://www.elastic.co/guide/en/elasticsearch/reference/current/search-suggesters.html#completion-suggester
    """
    title = fields.TextField(
        attr='title',
        fields={
            # Subfields
            'keyword': fields.KeywordField(), # for aggregations or filters
            'suggest': fields.CompletionField(), # Completion suggester is optimized for speed.
        } 
    )
    about = fields.TextField(
        attr='about',
        fields={
            # Subfields
            'keyword': fields.KeywordField(), # for aggregations or filters
        } 
    )
    unique_name = fields.KeywordField(normalizer=lc_normalizer)
    lang = fields.KeywordField(normalizer=lc_normalizer)
    tags = fields.ListField(fields.KeywordField(normalizer=lc_normalizer))
    members = fields.ObjectField(properties={
        'unique_name': fields.KeywordField(normalizer=lc_normalizer),
    })

    class Index:
        # To use a different name depending on the enviroment (dev/prod/test)
        name = settings.ELASTICSEARCH_INDEX_NAMES[f"{__name__}.RoomDocument"]
        settings = {
            "number_of_shards": 2,  # Thousands of documents
            "number_of_replicas": 1,
        }

    class Django:
        model = RoomItem
        fields = [
            "link",
            "is_channel",
            "created_at"
        ]

        # Optional: to ensure the Model doc will be re-saved when related models are updated
        related_models = [
            UserItem,
        ]

    def get_queryset(self, and_filter_fields={}, list_filter_fields={}, apply_distinct=False):
        """Overwrite get_queryset method (which is used by the django_elasticsearch_dsl
        to extract all items to index in elasticsearch from the database).
        
        Parameters
        ----------
        and_filter_fields: dict, default={}
            Check the documentation of app_base.api.create_advance_filter
            ```
            {
                "<field>__<desired_filter>": value/s,
                ...
            }
            ```

        list_filter_fields: dict, default={}
            Check the documentation of app_base.api.create_advance_filter
            ```
            {
                "<field>__<desired_filter>": { "values": [....], "OR": True/False },
                ...
            }
            ```

        apply_distinct: boolean, default=False
            In case the filters inside `and_filter_fields` or `list_filter_fields` contain 
            related fields that may cause duplicates. This often happens with "related 
            fields" with a `Many` relationship. For example, 
            `MessageItem.objects.filter(entities__entity__name__icontains="Pedro")` 
            returns duplicates (entities is a Many-to-Many relationship field). On the 
            other hand, `MessageItem.objects.filter(room__tags__icontains="politic")` 
            is safe, as it is a One-to-Many relationship accessed through a One relationship 
            (each message has only one room).
            NOTE: `.distinct("pk")` may significantly impact performance.  
        """

        # Best query to extract any items and its relations in only one request
        if and_filter_fields or list_filter_fields:
            qs = RoomItem.objects.filter(
                create_advance_filter(and_filter_fields, list_filter_fields)
            )
        else:
            qs = RoomItem.objects
        if apply_distinct:
            qs = qs.distinct("pk")

        return qs.prefetch_related("members")

    def get_instances_from_related(self, related_instance):
        """If related_models is set, define how to retrieve the News instance(s) from the related model.
        The related_models option should be used with caution because it can lead in the index
        to the updating of a lot of items.
        """
        if isinstance(related_instance, UserItem):
            return related_instance.rooms.all().distinct("pk")
        

@registry.register_document
class MessageDocument(Document):

    #annotated_text = AnnotatedTextField(attr='annotated_text') Ignore annotations for the moment
    msg_id = fields.LongField()
    text = fields.TextField(attr='annotated_text')
    lang = fields.KeywordField(normalizer=lc_normalizer)
    media_type = fields.KeywordField(normalizer=lc_normalizer)
    tags = fields.ListField(
        fields.KeywordField(
            attr='room.tags',
            normalizer=lc_normalizer
        )
    )
    entities = fields.KeywordField(
        multi=True,
        normalizer=lc_normalizer
    )
    room_name = fields.KeywordField(
        attr='room.unique_name', 
        normalizer=lc_normalizer 
    )
    room_about = fields.TextField(attr='room.about')

    class Index:
        # To use a different name depending on the enviroment (dev/prod/test)
        name = settings.ELASTICSEARCH_INDEX_NAMES[f"{__name__}.MessageDocument"]
        settings = {
            "number_of_shards": 5,  # Millions of documents
            "number_of_replicas": 1,
            "refresh_interval": "60s", # Increased to reduce the frequency of index refreshes and improve performance.
        }

    class Django:
        model = MessageItem
        fields = [
            "created_at",
            "stored_date",
            "views",
            "is_reply",
            "reply_to_id",
        ]

        # Optional: to ensure the Model doc will be re-saved when related models are updated
        related_models = [
            RoomItem,
            EntityItem,
            AnnotatedEntity
        ]

    def prepare_entities(self, instance):
        """
        Prepare the 'entities' field data.

        First of all, instance will be checked to search for the prefetch field
        'prefetch_entities' in order to avoid more DB calls. 
        """
        # Note: This ann_entities may contain repeated elements, that's way we
        # use `.distinct("entity__pk")`
        if hasattr(instance, 'prefetch_entities'):
            ann_entities = instance.prefetch_entities
        else:
            ann_entities = instance.entities.all().distinct("entity__pk")

        entities = []
        for ann_entity in ann_entities:
            entities.append(ann_entity.entity.name)
        return entities

    def get_queryset(
        self, 
        and_filter_fields = {}, 
        list_filter_fields = {}, 
        apply_distinct = False
    ):
        """Overridden `.get_queryset()` method  (which is used by the django_elasticsearch_dsl
        to extract all items to index in elasticsearch from the database).

        This method will improve the performance by using select_related and
        prefetch_related Django methods to extract many-to-many an foreign relationships
        in far fewer requests.
            - Select_related() makes a SQL inner join and extract all ForeignKeysField
            in the same request.
            - Prefetch_related() makes only 1 requests per Many-to-many field

        Parameters
        ----------
        and_filter_fields: dict, default={}
            Check the documentation of app_base.api.create_advance_filter
            ```
            {
                "<field>__<desired_filter>": value/s,
                ...
            }
            ```

        list_filter_fields: dict, default={}
            Check the documentation of app_base.api.create_advance_filter
            ```
            {
                "<field>__<desired_filter>": { "values": [....], "OR": True/False },
                ...
            }
            ```

        apply_distinct: boolean, default=False
            In case the filters inside `and_filter_fields` or `list_filter_fields` contain 
            related fields that may cause duplicates. This often happens with "related 
            fields" with a `Many` relationship. For example, 
            `MessageItem.objects.filter(entities__entity__name__icontains="Pedro")` 
            returns duplicates (entities is a Many-to-Many relationship field). On the 
            other hand, `MessageItem.objects.filter(room__tags__icontains="politic")` 
            is safe, as it is a One-to-Many relationship accessed through a One relationship 
            (each message has only one room).
            NOTE: `.distinct("pk")` may significantly impact performance.     
        """

        # Step 1: Fetch the ContentType for MessageItem
        messageitem_content_type = ContentType.objects.get_for_model(MessageItem)

        # Step 2: Query AnnotatedEntity objects related to MessageItems and prefetch related EntityItems
        annotated_entities_prefetch = Prefetch(
            'entities',  # This is the related name from MessageItem to AnnotatedEntity
            queryset=(
                AnnotatedEntity.objects.filter(content_type=messageitem_content_type)
                .prefetch_related('entity')
                .distinct("entity__pk")
            ),  # This joins AnnotatedEntity to EntityItem
            to_attr='prefetch_entities'  
        )

        # Best query to extract any items and its relations in only one request
        if and_filter_fields or list_filter_fields:
            qs = MessageItem.objects.filter(
                create_advance_filter(and_filter_fields, list_filter_fields)
            )
        else:
            qs = MessageItem.objects

        if apply_distinct:
            qs = qs.distinct("pk")

        qs = qs.select_related('room').prefetch_related(annotated_entities_prefetch)
        return qs
    
    def get_instances_from_related(self, related_instance):
        """If related_models is set, define how to retrieve the Message instance(s) 
        from the related model (to ask for an update in the msg index).
        
        The related_models option should be used with caution because it can lead 
        to many items being updated in the index.

        f.e: An item from RoomItem changes its tag, this function helps to 
        retrieve all the message items related to this item and update its documents
        in the elastic index.
        """
        if isinstance(related_instance, RoomItem):
            return related_instance.messages.all()
        elif isinstance(related_instance, EntityItem):
            # The query retrieves a unique list of MessageItem objects that are 
            # related to a given entity through the intermediate model AnnotatedEntity. 
            return [
                ann_ent.content_object 
                for ann_ent in related_instance.annotations.filter(
                    content_type=ContentType.objects.get_for_model(MessageItem)
                ).distinct("object_id")
            ]
        elif isinstance(related_instance, AnnotatedEntity):
            return [related_instance.content_object]