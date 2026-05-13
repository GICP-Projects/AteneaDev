import time
import asyncio
import logging
from celery.app import shared_task
from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import F
from app_base.utils import call_service
from app_base.tagger import extract_tags
from app_base.api import generic_celery_task_orchestrator, bulk_add_query_relationships_with_pks


# Get an instance of a logger
logger = logging.getLogger(__name__)


def clean_text_to_sentiment(text, clean = True):
    """ Clean the text to improve sentiment analysis capabilities.
    - Removes any email
    - Removes any URL 
    - Removes any mention
    """
    if clean:
        text, _ = extract_tags(
            text,
            hashtag=False,
            date=False,
            time=False,
            number=False,
            emoji=False
        )
    return text


async def call_sentiment_service(
    data, 
    max_by_request = None,
    max_parallel_requests = None,
    batch_size = 100 # Max number of requests by session
):
    """ Calls the sentiment service endpoint with the provided data, handling batching.

    Parameters
    ----------
    data: List[dict]
        List of dicts with texts. 
        ```
        [
            {
                "text_to_sentiment": ...
            },
            ...
        ]
        ```

    max_by_request: int, default=None
        Maximum number of items to be included in a single request. It is recommended 
        to use the value of the settings file. If not provided, `settings.SENTIMENT_SERVICE`
        is used.

    max_parallel_requests: int, default=None
        Max number of parallel requests at the same time. It is recommended to
        use the value of the settings file. If not provided, `settings.SENTIMENT_SERVICE`
        is used.
            
    batch_size: int, default=100
        Maximum number of requests per session to manage resource usage and 
        reduce timeouts. Helps prevent exceeding memory and connection limits, 
        ensuring smoother execution.

    Returns
    ----------
    model_name: str
        Name of the model used to classify the sentiment.

    version: str
        Version of the model used to classify the sentiment.

    sentiments: List[int]
        List of dicts with the sentiment of each text. 
        ```
        [
            0,
            1,
            2,
            ...
        ]
        ```
    """
    service_config = settings.SENTIMENT_SERVICE
    max_by_request = max_by_request or service_config.max_items_by_request
    max_parallel_requests = max_parallel_requests or service_config.max_parallel_requests

    # Run sentiment service
    responses = await call_service(
        data=data,
        service_config=service_config,
        endpoint_name="sentiment",
        payload_builder_func=(lambda chunk, **_: {"data": [{"text": item["text_to_sentiment"]} for item in chunk]}),
        payload_builder_kwargs={},
        max_by_request=max_by_request,
        max_parallel_requests=max_parallel_requests,
        batch_size=batch_size
    )

    model_name = ""
    version = ""
    total_sentiments = [] 
    for result in responses:
        # Avoid errors in case some response are empty (due to any type of error)
        if not model_name:
            model_name = result.get("model", None) 
            version = result.get("version", -1)

        # `sentiments` should never be empty, because in case of error `call_service` 
        # fills the list with None to maintain consistency between data and responses.
        total_sentiments.extend(result["sentiments"])

    # Flatten all grouped responses [....] (flatten)
    return model_name, version, total_sentiments


# ======================================================
# =====           SENTIMENT CELERY TASKS           =====
# ======================================================
# =====  Celery tasks that have specific functio-  =====
# =====  -nality to extract and store sentiments.  =====
# ======================================================


# ======================================================
# =====   CELERY TASKS: SENTIMENT CLASSIFICATION   =====     
# ======================================================

@shared_task(track_started=True)
def run_sentiment(
    token, 
    model_class_name, 
    model_class_app_label, 
    model_class_text_field_name,
    model_class_sentiment_field_name,
    model_class_sentiment_model_field_name = None,
    and_filter_fields = {}, 
    list_filter_fields = {},
    apply_distinct = False,
    block_size = 500000,
    chunk_size = None,
    thread_count = None,
):
    """ Sentiment Classification Orchestrator.
    Run the sentiment classification in celery and split the task in N subtasks to
    split the computational cost and improve performance.

    NOTE: All tasks are executed one by one, not in a group (the number of parallel
    tasks will dependen on the sentiment queue size).

    Parameters
    ----------
    token: string
        Query token to add the relation with the item

    model_class_name: str
        Contains the class name of the Django Model with the text field that is 
        going to be used to classify its sentiment.

    model_class_app_name: str
        Contains the django app name which contains the model class. 

    model_class_text_field_name: str
        Contains the field name of the text field in the Django Model which is going to
        be used to classify the sentiment.

    model_class_sentiment_field_name: str
        Contains the field name where the sentiment will be stored.

    model_class_sentiment_model_field_name: str, default=None
        Contains the field name where the sentiment model name will be stored.
        In format "{model_class_name}_{version}". 

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
        
    instruct: str, default=""
        The instruction added to each text for the embeddings calculation.

    clean_text: bool, default=False
        Clean text by removing emails, urls and mentions.

    block_size: int, default=500000
        Block size of the items to be processed by each `classify_sentiment` task.
    
    chunk_size: int, default=None
        Maximum number of items to be included in a single request to the SENTIMENT service. 
        Ensures that each request stays within acceptable size limits to avoid 
        overloading the service. If not provided, the value from `settings.SENTIMENT_SERVICE`
        is used.

    thread_count: int, default=None
        Max number of parallel requests at the same time. If not provided, the value
        from `settings.SENTIMENT_SERVICE` is used.
    """
    service_config = settings.SENTIMENT_SERVICE
    chunk_size = chunk_size or service_config.max_items_by_request
    thread_count = thread_count or service_config.max_parallel_requests

    generic_celery_task_orchestrator(
        model_class_name=model_class_name,
        model_class_app_label=model_class_app_label,
        celery_task=classify_sentiment,
        celery_task_kwargs={
            "model_class_text_field_name": model_class_text_field_name,
            "model_class_sentiment_field_name": model_class_sentiment_field_name,
            "model_class_sentiment_model_field_name": model_class_sentiment_model_field_name,
            "clean_text":True,
            "chunk_size": chunk_size,
            "thread_count": thread_count,
        },
        and_filter_fields=and_filter_fields,
        list_filter_fields=list_filter_fields,
        exclude_filter_fields={f"{model_class_text_field_name}__exact":""}, # exclude empty messages
        apply_distinct=apply_distinct,
        block_size=block_size,
        token=token
    )


# ======================================================
# =====    CELERY TASKS: EMBEDDINGS PR0CESSORS     =====     
# ======================================================

@shared_task(track_started=True)
def classify_sentiment(
    items_pks, 
    model_class_name,
    model_class_app_label,
    model_class_text_field_name,
    model_class_sentiment_field_name,
    model_class_sentiment_model_field_name = None, 
    clean_text=False,
    chunk_size = None,
    thread_count = None,
):
    """ Extracts all the texts, retrieve their sentiment and update the items with 
    this new information. 
    """
    service_config = settings.SENTIMENT_SERVICE
    chunk_size = chunk_size or service_config.max_items_by_request
    thread_count = thread_count or service_config.max_parallel_requests

    # Extract model class from app_label + model_class name
    ModelClass = apps.get_model(model_class_app_label, model_class_name)

    # Start a transaction to avoid race conditions:
    # f.e: run this task more than once at the same with same items (wasting time 
    # and RAM usage).
    with transaction.atomic():

        # Extract texts (list.. to force the queryset evaluation)
        data = list(
            ModelClass.objects.filter(pk__in=items_pks)
            .select_for_update(skip_locked=True)
            .only(
                "pk", 
                model_class_text_field_name, 
            ).values(
                "pk", 
                text_to_sentiment = F(model_class_text_field_name), 
            )
        )

        # Clean each text and filter items
        if clean_text:
            data = [
                {
                    **item, 
                    "text_to_sentiment": extract_tags(
                        item["text_to_sentiment"],
                        hashtag=False,
                        date=False,
                        time=False,
                        number=False,
                        emoji=False
                    )[0]
                } 
                for item in data
            ]

        # Classify the sentiment of the texts
        model_name, version, sentiments = asyncio.run(
            call_sentiment_service(data, chunk_size, thread_count)
        )

        # Related sentiment data to the items (in order to update them)
        items_to_update = []
        sentiment_model = f"{model_name}_{version}"
        for item, sentiment in zip(data, sentiments):
            # If sentiment is None an error ocurred in the request (then, 
            # this item will be ignored)
            if sentiment is not None:
                items_to_update.append(
                    ModelClass(
                        **{
                            "pk": item["pk"],
                            model_class_sentiment_field_name: sentiment,
                            model_class_sentiment_model_field_name: sentiment_model
                        }
                    )
                )

        # Update the items by adding the relationships
        ini_time = time.time()
        num_updates = ModelClass.objects.bulk_update(
            items_to_update, 
            [model_class_sentiment_field_name] + (
                [model_class_sentiment_model_field_name] 
                if model_class_sentiment_model_field_name 
                else []
            ),
            batch_size=settings.BULK_BATCH_SIZE
        )
        updt_time = time.time() - ini_time

        logger.info(
            f"{num_updates} {ModelClass.__name__} items sentiment has been classified "
            f"(bulk_update: {updt_time:.3f} secs)."
        )
