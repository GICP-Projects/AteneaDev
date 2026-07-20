from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from app_telegram.models import MessageItem
from app_metadata.models import CategoryItem
from app_metadata.services.embeddings import (
    clean_text_to_embed, 
    call_embed_service,
    run_embeddings, 
)
from app_metadata.vector_store import get_collection_name, search_points
from app_base.api import bulk_add_query_relationships, bulk_add_query_relationships_with_pks
import asyncio


# ==================================================================
#               Ingest & retrieve default metadata endpoints
# ==================================================================

def category_bulk_create(token, categories):
    """Bulk create categories by a list of dictionary as source into the DB

    This function serves the endpoint /metadata/categories
    
    NOTE: This items have been already validated and cleaned by its Serializer 
    so it's not necessary to re-check them. 

    Parameters
    ----------
    token: str
        UUID.hex 

    categories: List[dict]
        List of CategoryItem dictionaries, the DataIngestSerializer has already 
        cleaned up each element an checked duplicates.

    Returns
    -------
    affected_items: List[CategoryItem]
        List of created/updated CategoryItem items.
    """

    # Bulk create
    affected_items = CategoryItem.objects.bulk_create(
        [CategoryItem(**seed) for seed in categories],
        batch_size=settings.BULK_BATCH_SIZE,
        ignore_conflicts=True
    )

    # Calculate (or re-calculate) embeddings for all the created/updated items
    run_embeddings.delay(
        token, 
        CategoryItem.__name__,
        CategoryItem._meta.app_label,
        "description",
        "embeddings",
        and_filter_fields = {"pk__in": [item.pk for item in affected_items]},
        minimum_text_length=1
    )

    return affected_items


def dataitem_search(token, dataitem_model_class, name=None, description=None):
    """ Search for dataitem items (CategoryItem).

    Parameters
    ----------
    token: str
        UUID.hex 

    dataitem_model_class: class (django.db.models.Model)
        CategoryItem model class.

    name: str, default=None
        Filter dataitem items by the name.

    description: str, default=None
        Filter dataitem items by the description.

    Returns
    -------
    queryset: QuerySet[CategoryItem]
        A queryset of CategoryItem items.
    """
    and_fields = {
        # Last update range filter
        "name__icontains": name,
        "description__icontains": description,
    }

    # None can't be passed to filter
    and_fields = {k: v for k, v in and_fields.items() if v}
    items = dataitem_model_class.objects.filter(**and_fields).order_by('name')

    if token:
        bulk_add_query_relationships(
            items=items,
            query_pk=token
        )

    return items


def dataitem_recalculate(token, dataitem_model_class):
    """ Recalculate the embeddings for all the items of a dataitem_model_class.
    """
    # Calculate (or re-calculate) embeddings for all the created/updated items
    run_embeddings.delay(
        token, 
        dataitem_model_class.__name__,
        dataitem_model_class._meta.app_label,
        "description",
        "embeddings",
        #and_filter_fields = {"pk__in": [item.pk for item in affected_items]},
        minimum_text_length=1
    )



# ==================================================================
#                      General search embeddings  
# ==================================================================

def embed_search(token, q, source_model_class, instruct="", empty=False):
    if not instruct and not empty:
        instruct="Given a web search query, retrieve relevant passages that answer the query"
    
    _, _, embeddings = asyncio.run(
        call_embed_service(
            data=[{"text_to_embed": clean_text_to_embed(q)}], 
            instruct=instruct
        )
    )
    if not embeddings or not embeddings[0]:
        raise Exception("Error during embeddings extraction.")

    collection_name = get_collection_name(source_model_class, "embeddings")
    hits = search_points(
        collection_name=collection_name,
        query_vector=embeddings[0],
        limit=10,
        exact_filters={"source_model_class": source_model_class},
    )

    results = []
    matched_pks = []
    for hit in hits:
        payload = getattr(hit, "payload", {}) or {}
        source_pk = payload.get("source_pk")
        if source_pk:
            matched_pks.append(source_pk)
        results.append(
            {
                "source_pk": source_pk,
                "text": payload.get("text", ""),
                "lang": payload.get("lang"),
                "score": getattr(hit, "score", None),
            }
        )

    if token and matched_pks and source_model_class == MessageItem.__name__:
        bulk_add_query_relationships_with_pks(
            matched_pks,
            ContentType.objects.get_for_model(MessageItem),
            token,
        )

    return results
