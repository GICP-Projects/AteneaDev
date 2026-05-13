import operator
from django.db.models.query import QuerySet
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q as Q_DJ
from django.apps import apps
from celery import group
from app_base.models import QueryItem
from app_base.utils import hash_json
from functools import reduce
import logging
import time


# Get an instance of a logger
logger = logging.getLogger(__name__)


# ===============================================================
# ========                Query filters                  ========
# ===============================================================

def create_advance_filter(and_filter_fields={}, list_filter_fields={}):
    """Create an advanced filter for complex query building in Django.
    
    This function constructs a complex Q object for Django ORM queries, allowing 
    for the combination of multiple conditions with AND/OR logic.

    NOTE: Empty filters (e.g. None or []) will be ignored.

    Parameters
    ----------
    and_filter_fields: dict, default={}
        A dictionary of fields and their corresponding values to be included in 
        the query with an AND relationship. Each key is a field name combined with 
        a Django filter lookup (e.g., `field__lookup`), and the value is the filter 
        value(s).
        ```
        {
            "<field>__<desired_filter>": value/s,
            ...
        }
        ```

    list_filter_fields: dict, default={}
        A dictionary of fields that need OR/AND logic within themselves.
        Each key is a field name combined with a Django filter lookup, and the 
        value is a dictionary with a list of values and a boolean indicating OR/AND 
        relationship.
        ```
        {
            "<field>__<desired_filter>": { "values": [....], "OR": True/False },
            ...
        }
        ```

    Returns
    -------
    query: django.db.models.query_utils.Q
        A Q object representing the combined filter criteria, ready to be used in 
        a Django ORM query.
    """
    and_filter_fields = {
        k: v 
        # Ignore empty fields except booleans set to False (not None and not an empty list)
        for k, v in and_filter_fields.items() if not (v is None or (isinstance(v, list) and not v))
    }

    list_fields = [
        reduce(
            lambda x, y: (operator.or_ if data.get("OR", True) else operator.and_)(x, y), 
            [Q_DJ(**{field_filter:v}) for v in data.get("values", [])]
        )
        for field_filter, data in list_filter_fields.items()  
        if data.get("values")
    ]

    # Built information representation only for debug mode
    if logger.isEnabledFor(logging.DEBUG):  
        and_conditions = [f"{k}={v!r}" for k, v in and_filter_fields.items()]
        list_conditions = []
        
        for field_filter, data in list_filter_fields.items():
            if values := data.get("values"):
                operator_str = "OR" if data.get("OR", True) else "AND"
                list_conditions.append(f"{field_filter} ({operator_str}): {values!r}")

        logger.debug(
            "Created advanced filter: "
            f"AND conditions ({len(and_conditions)}): {and_conditions or None}. | "
            f"LIST conditions ({len(list_conditions)}): {list_conditions or None}."
        )

    return Q_DJ(*list_fields, **and_filter_fields)


def get_items_pks(
    ModelClass,
    and_filter_fields = {},
    list_filter_fields = {},
    exclude_filter_fields = {},
    apply_distinct = False
):
    """ Generic function to get the primary keys of items from a ModelClass.
    
    Parameters
    ----------
    ModelClass: django.db.models.Model
        ModelClass of the items to be retrieved.

    and_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of `create_advance_filter`.
        ```
        {
            "<field>__<desired_filter>": value/s,
            ...
        }
        ```

    list_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of `create_advance_filter`.
        ```
        {
            "<field>__<desired_filter>": { "values": [....], "OR": True/False },
            ...
        }
        ```

    exclude_filter_fields: dict, default={}
        Dict with the Django ORM filters to exclude items. 

    apply_distinct: boolean, default=False
        In case the filters inside `and_filter_fields` or `list_filter_fields` contain 
        related fields that may cause duplicates. This often happens with "related 
        fields" with a `Many` relationship. 

    Returns
    -------
    items_pks: list[uuid.UUID]
        List of primary keys of the items.

    total_items: int
        Total number of items.
    """
    items = ModelClass.objects.filter(
        create_advance_filter(
            and_filter_fields=and_filter_fields, 
            list_filter_fields=list_filter_fields
        )
    )

    if exclude_filter_fields:
        items = items.exclude(**exclude_filter_fields)

    # Ordering by an unique value allow the unevaluated Queryset to be sliced 
    # (without repeating items inconsistently in each slice).
    # "Slicing an unevaluated QuerySet usually returns another unevaluated QuerySet"
    items = items.order_by("pk")

    if apply_distinct:
        items = items.distinct()

    return items.values_list('pk', flat=True), items.count()


def generic_celery_task_orchestrator(
    model_class_name,
    model_class_app_label, 
    celery_task,
    celery_task_kwargs,
    and_filter_fields = {}, 
    list_filter_fields = {},
    exclude_filter_fields = {},
    apply_distinct = False,
    block_size = 500000,
    group_tasks = False,
    token = None
):
    """ Generic celery task orchestrator. 
    It allows to split the workload in N subtasks, each one with `block_size` items, 
    to split the computational cost and improve performance.

    Parameters
    ----------
    model_class_name: str
        Contains the class name of the Django Model with the items to be processed.

    model_class_app_name: str
        Contains the django app name which contains the model class.

    celery_task: celery.task
        This function contains the celery task to be executed. The celery_task must
        have the list of items pks, the `model_class_name` and the `model_class_app_label`
        as the first three positional arguments.
    
    celery_task_kwargs: dict
        Rest of the arguments will be passed to the `celery_task` function as kwargs.

    and_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of `create_advance_filter`.
        ```
        {
            "<field>__<desired_filter>": value/s,
            ...
        }
        ```
    
    list_filter_fields: dict, default={}
        Dict with the Django ORM filters to extract items. Check the documentation 
        of `create_advance_filter`.
        ```
        {
            "<field>__<desired_filter>": { "values": [....], "OR": True/False },
            ...
        }
        ```

    exclude_filter_fields: dict, default={}
        Dict with the Django ORM filters to exclude items. 

    apply_distinct: boolean, default=False
        In case the filters inside `and_filter_fields` or `list_filter_fields` contain 
        related fields that may cause duplicates. This often happens with "related 
        fields" with a `Many` relationship. 

    block_size: int, default=500000
        Block size of the items to be processed by each `celery_task`.

    group_tasks: boolean, default=False
        If True, the `celery_task` will be executed as a group of tasks. 

    token: string | uuid.UUID, default=None
        UUID.hex
    """
    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)

    items_pks, total_items = get_items_pks(
        ModelClass=ModelClass,
        and_filter_fields=and_filter_fields,
        list_filter_fields=list_filter_fields,
        exclude_filter_fields=exclude_filter_fields,
        apply_distinct=apply_distinct
    )

    blocks_of_tasks = []
    logger.debug(f"Total items: {total_items}")
    # Split the work load in tasks with an specific block_size (to avoid OOM errors 
    # on large amounts of data and and control the limitations of the embeddings service) 
    start_time = time.time()
    for i in range(0, total_items, block_size):
        # Each task will process all the items inside the block 
        task = celery_task.si(
                list(items_pks[i:i+block_size]), 
                model_class_name,
                model_class_app_label,
                **celery_task_kwargs
            )
        
        # launch the task one by one if not in a group
        if not group_tasks:
            task.apply_async()
        blocks_of_tasks.append(task)

        end_time = time.time()
        logger.debug(f"items[{i}:{i+block_size}]. Time: {end_time - start_time}secs")

        # Reset start time
        start_time = time.time()

    if blocks_of_tasks:
        logger.info(f"'{celery_task.__name__}' has been split into {len(blocks_of_tasks)} subtasks.")
        if  group_tasks:
            group(blocks_of_tasks).apply_async()

    # Finally, bind the Query to all the items involved
    if token:
        bulk_add_query_relationships_with_pks(items_pks, ContentType.objects.get_for_model(ModelClass), token)    


# ===============================================================
# ========    Functions to save (create/update) items    ========
# ===============================================================


def save_item_json(item_json, token, item_class, excluding_keys=[]):
    """Save/update an item from its JSON and add Query relationships.

    This function is for items with the hash_value field (creating or filtering
    its value).


    Parameters
    ----------
    item_json: dict
        Dict with the item info to create/update into the database

    token: string
        Query token to add the relation with the item

    item_class: class (django.db.models.Model)
        Item model class of the item

    excluding_keys: list
        List of keys from the previous dictionary that should not participate 
        in the hash value calculation.

    Returns
    ----------
    item: django.db.models.Model
        The created/updated item
    """

    item_json["hash_value"] = hash_json(
        item_json, excluding_keys=excluding_keys
    )

    existing_item = item_class.objects.filter(
        hash_value=item_json["hash_value"]
    ).first()

    if existing_item:
        update_item(existing_item, token)
        return existing_item
    else:
        return create_item(item_json, token, item_class)


def create_item(item_json, token, item_class):
    """Create item and add query relationship.

    This function is for items without a hash_value field
    """
    new_item = item_class.objects.create(**item_json)
    QueryItem.objects.create(
        content_object=new_item,
        query_id=token,
    )
    return new_item


def update_item(item_to_update, token):
    """Update queries relationship of item.

    This function is for items without a hash_value field
    """
    return QueryItem.objects.create(
        content_object=item_to_update,
        query_id=token,
    )


# ===============================================================
# ========    Functions to add many to many relations    ========
# ===============================================================
def get_through_list(
    ThroughModel, list_pk_class, target_class, list_pk_by_target_pk
):
    """Return a through list items to be bulked

    We will use Through Model to create the ManyToMany relations
    https://stackoverflow.com/questions/6996176/how-to-create-an-
    object-for-a-django-model-with-a-many-to-many-field/
    10116452#10116452

    Parameters
    ----------
    ThroughModel: through models
        The through model used to define the custom many-to-many relationship.
        It is a model class that represents the intermediate table storing the
        relationship between the current model and the target model.
        F.e: ModelClassName.relation_field.through

    list_pk_class: class (django.db.models.Model)
        Model class of the previous pk list.

    target_class: class (django.db.models.Model)
        Class of the target object which relation want to be created.

    list_pk_by_target_pk: list[dict]
        Dictionary with all the relationships is going to be created for the same
        ManytoMany field. The dict contains: 
        {
            # pks from target_class : [List of primary keys from list_pk_class]
            "UUID string pk": []
            ...
        }

    Returns
    -------
    throughs_to_create: List
        List of ThroughModel items
    """
    throughs_to_create = [
        ThroughModel(
            **{
                f"{target_class._meta.model_name}_id": target_pk,
                f"{list_pk_class._meta.model_name}_id": pk,
            }
        )
        for target_pk, list_pk in list_pk_by_target_pk.items() 
        for pk in list_pk
    ]
    return throughs_to_create


def bulk_add_generic_relationships(
    list_pk_class,
    relation_field,
    target_class,
    list_pk_by_target_pk
):
    """Add to an item many-to-many field a list of relations.

    Example (1) Add to a list of items a new element relation:
    - We have an item with a many-to-many field of elements
    - We want to add a new element to that relationship
    - So with the pk of the element (target_pk) and the pk of all
        items (list_pk) we create the relation
    - In this case, the relation_class is the same as that of the list of
        items (relation_class == list_pk_class)

    Example (2) Add to an items a list of new elements relation:
    - We have an item with a many-to-many field of elements
    - We want to add a list of new element to that relationship
    - So with the pk of all the element (list_pk) and the item (target_pk)
        we create the relation
    - In this case, the relation_class is the same as the item
        (relation_class == target_class)

    Parameters
    ----------
    list_pk_class: class (django.db.models.Model)
        Model class of the previous pk list.

    relation_field: string
        Field name of the many_to_many relationship from the list_pk_class perspective.

    target_class: class (django.db.models.Model)
        Class of the target object which relation want to be created.

    list_pk_by_target_pk: list[dict]
        Dictionary with all the relationships is going to be created for the same
        ManytoMany field. The dict contains: 
        {
            # pks from target_class : [List of primary keys from list_pk_class]
            "UUID string pk": []
            ...
        }
    """
    # Recover Through model to create the relationships
    ThroughModel = getattr(list_pk_class, relation_field).through

    throughs_to_create = get_through_list(
        ThroughModel, list_pk_class, target_class, list_pk_by_target_pk, 
    )
    ThroughModel.objects.bulk_create(throughs_to_create)


def bulk_add_query_relationships(
    items, 
    query_pk,
    block_size = 500000,
    batch_size = 100000
):
    """Add Query relationships to items by using instances.
    Parameters
    ----------
    items: Iterable[django.db.models.Model] 
        Iterable of model instances.

    query_pk: uuid.UUID
        PK of the QueryItem to which the items will be related.
    """

    if isinstance(items, QuerySet):
        total_items = items.count()
    else:
        total_items = len(items)

    start_time = time.time()
    for i in range(0, total_items, block_size):
        # For each item, the QueryItem is created and linked to it
        query_items = [
            QueryItem(**{"content_object": item, "query_id": query_pk}) 
            for item in items[i:i+block_size]
        ]
        QueryItem.objects.bulk_create(query_items, batch_size=batch_size)

    logger.info(f"Created {total_items} QueryItem items (bulk_create: {(time.time() - start_time):.3f} secs).")


def bulk_add_query_relationships_with_pks(
    items_pks, 
    item_content_type, 
    query_pk,
    block_size = 500000,
    batch_size = 100000
):
    """Add Query relationships to items by using their primary keys.
    
    Parameters
    ----------
    items_pks: Iterable[uuid.UUID] 
        List of primary keys.

    item_content_type: django.contrib.contenttypes.models.ContentType
        ContentType of the Django model to which the primary keys belong.

    query_pk: uuid.UUID
        PK of the QueryItem to which the items will be related.

    block_size: int, default=500000
        Number of QueryItems will be created at once to bulk.

    batch_size: int, default=100000
        Batch size of the Model.objects.bulk_create() method.
    """

    start_time = time.time()
    total_pks = len(items_pks)
    for i in range(0, total_pks, block_size):
        # For each item, the QueryItem is created and linked to it
        query_items = [
            QueryItem(
                **{
                    "object_id": pk, 
                    "content_type": item_content_type,
                    "query_id": query_pk
                }
            ) 
            for pk in items_pks[i:i+block_size]
        ]
        QueryItem.objects.bulk_create(query_items, batch_size=batch_size)

    logger.info(f"Created {total_pks} QueryItem items (bulk_create: {(time.time() - start_time):.3f} secs).")