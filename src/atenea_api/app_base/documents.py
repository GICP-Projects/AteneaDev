import time
import logging
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from django_elasticsearch_dsl import fields
from django_elasticsearch_dsl.registries import registry
from elasticsearch_dsl.field import Field, DenseVector
from elasticsearch_dsl import normalizer
from app_base.api import bulk_add_query_relationships_with_pks

# Get an instance of a logger
logger = logging.getLogger(__name__)


# ==================================================================
#                  Django Elasticsearch new fields
# ==================================================================

class Flattened(Field):
    """Custom elasticsearch-dsl field (The previous lib don't include this field yet).
    """
    name = "flattened"

class FlattenedField(fields.DEDField, Flattened):
    """Custom django-elasticsearch-dsl field. (The previous lib don't include this field yet).

    Wiki: https://www.elastic.co/guide/en/elasticsearch/reference/8.4/flattened.html
    """

    def __init__(self, fields_to_add=[], **kwargs):
        super().__init__(**kwargs)
        # Object fields to flatten
        self._fields_to_add = fields_to_add

    def _get_data_from_object(self, obj, existing_fields):
        # dict adding all existing fields
        if not self._fields_to_add:
            # all fields (ignoring Django meta info)
            return {
                k: v for k, v in obj.__dict__.items() if k in existing_fields
            }

        # If only add some fields to the ES doc
        data = {}
        for field in self._fields_to_add:
            if field not in existing_fields:
                raise ValueError(
                    "Field {} doesn't belong to the {} model (or "
                    "it is a Many_to_many/ForeignKey field, which is not supported). "
                    "Please, add a valid one".format(
                        field, obj.__class__.__name__
                    )
                )

            data[field] = obj.__dict__[field]

        return data

    def get_value_from_instance(self, instance, field_value_to_ignore=None):
        """If instance is a m-t-m relationships the list of objects retrieved
        should be from the same Django Model, not generic allowed.
        """
        objs = super().get_value_from_instance(instance, field_value_to_ignore)

        if objs is None:
            # objs = None
            return None
        elif not objs:
            # objs = [] or Queryset([])
            return []

        try:
            is_iterable = bool(iter(objs))
            obj = objs[0]
        except TypeError:
            # is not a list
            obj = objs
            is_iterable = False

        # Get existing fields name
        # foreign and many_to_many fields will be ignored (not supported)
        existing_fields = set.intersection(
            set(obj.__dict__.keys()),
            {field.name for field in obj._meta.fields},
        )

        # While dicts are iterable, they need to be excluded here so their full 
        # data is indexed
        if is_iterable and not isinstance(objs, dict):
            data = [
                self._get_data_from_object(obj, existing_fields)
                for obj in objs
            ]

        else:
            data = self._get_data_from_object(obj, existing_fields)

        return data


class AnnotatedText(Field):
    """
    Custom elasticsearch-dsl field for the annotated-text plugin
    """
    _param_defs = {
        "fields": {"type": "annotated_text", "hash": True},
    }
    name = "annotated_text"

class AnnotatedTextField(fields.DEDField, AnnotatedText):
    """
    Custom django-elasticsearch-dsl field for the annotated-text plugin
    """
    pass


class DenseVectorField(fields.DEDField, DenseVector):
    """Custom django-elasticsearch-dsl field. (The previous lib don't include this field yet).
    """
    def __init__(self, attr=None, dims=None, **kwargs):
        """ If dims is not specified, it will be set to the length of the first 
        vector added to the field.  
        """
        super().__init__(attr=attr, dims=dims, **kwargs)


# ==================================================================
#                      Elasticsearch normalizers
# ==================================================================

# Simple keyword field normalizer 
lc_normalizer = normalizer(
    "lc_normalizer",
    filter=["lowercase", "asciifolding", "trim"]
)


# ==================================================================
#                      Elasticsearch normalizers
# ==================================================================

def update_index(
    token, 
    document, 
    and_filter_fields = {},
    list_filter_fields = {},
    apply_distinct = False,
    block_size = 500000, 
    action = "index",
    es_chunk_size = 2500, 
    thread_count = 8
):
    """ Updates (index or delete) the Elasticsearch index for the given queryset 
    in chunks.
    
    QuerySets with large amounts of data can cause memory issues if loaded all 
    at once for indexing. Therefore, we divide the data into memory-manageable 
    chunks and then index those chunks in smaller chunks.

    Parameters
    ----------
    query_pk: uuid.UUID
        PK of the QueryItem to which the items will be related.

    document: django_elasticsearch_dsl.Document
        An instance of the Elasticsearch document class that defines the structure of the indexed data.

    and_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of app_base.api.create_advance_filter.
        ```
        {
            "<field>__<desired_filter>": value/s,
            ...
        }
        ```

    list_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of app_base.api.create_advance_filter.
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
        
    block_size: int
        The size of each block of items to be processed in memory from the DB and 
        indexed at a time.

    action: str, default="index"
        Elasticsearch bulk actions. Available: "index" or "delete".
        https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-bulk.html
        
    es_chunk_size: int
        The number of items of each request to Elasticsearch.
        It can lead to a memory error caused by exceeding the configured size in 
        our Elasticsearch cluster. e.g:
        ```
            ApiError(429, 'circuit_breaking_exception', 'Data too large, data for 
            [<http_request>] would be ... which is larger than the limit of ... 
        ```
        In this case, reduce the concurrent requests to the cluster and/or the 
        number of items sent.

    thread_count: int
        The number of threads to use for parallel indexing of the chunks on the 
        Elasticsearch cluster.
    """
    # Use transaction.atomic to disable Django's auto-commit mode, preventing the cursor 
    # from materializing all results on the server and instead using a server-side cursor 
    # for efficient iteration over large querysets.
    # https://stackoverflow.com/a/76261653
    with transaction.atomic():
        queryset = document.get_queryset(
            and_filter_fields, 
            list_filter_fields, 
            apply_distinct
        )
        total_items = queryset.count()
        logger.debug(f"Total items: {total_items}")
        items_counter = 0
        chunk_actions = [] # list of ES actions to bulk
        # Using iterator(chunk_size=...) to avoid having the whole query cached 
        # at the end of indexing (leading to OOM errors on large amounts of data) 
        # Read: https://docs.djangoproject.com/en/4.2/ref/models/querysets/#iterator
        init_time = start_time = time.time()
        for item in queryset.iterator(chunk_size=block_size):
            chunk_actions.append(document._prepare_action(item, action=action))
            items_counter += 1
            # Chunk completed or last chunk
            if len(chunk_actions) >= block_size or items_counter == total_items:
                chunk_len = len(chunk_actions)
                logger.info(
                    f"Extracted {len(chunk_actions)} items "
                    f"(iterator & _prepare_action: {(time.time() - start_time):.3f}secs)."
                )
                
                index_init = time.time()
                document.parallel_bulk(
                    chunk_actions,
                    chunk_size = es_chunk_size,
                    thread_count = thread_count,
                    refresh = False,
                )
                logger.info(
                    f"Indexed {chunk_len} items (index: {(time.time() - index_init):.3f}secs)."
                )

                # Reset chunk
                chunk_actions = []
                start_time = time.time()

        
        # Finally refresh index (index has been indexed using refresh=Faslse)
        index = registry.get_indices(models=[document.Django.model]).pop()
        index.refresh()
        
        logger.info(
            f"{total_items} {document.Django.model.__name__} items were indexed "
            f"in the '{document.Index.name}' index (full: {(time.time() - init_time):.3f} secs)."
        )

        # Relates the chunk to the Query before relaseing its memory
        if token:
            pks = list(queryset.values_list("pk", flat=True))
            bulk_add_query_relationships_with_pks(
                pks, 
                ContentType.objects.get_for_model(document.Django.model), 
                token
            ) 