from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import NotFound
from rest_framework.decorators import action
from django_elasticsearch_dsl_drf.viewsets import DocumentViewSet
from django_elasticsearch_dsl_drf.pagination import PageNumberPagination
from django_elasticsearch_dsl_drf.filter_backends.search.base import BaseSearchFilterBackend
from django_elasticsearch_dsl_drf.filter_backends.search.query_backends import MatchPhraseQueryBackend
from django_elasticsearch_dsl_drf.filter_backends import (
    MultiMatchSearchFilterBackend, 
    FilteringFilterBackend, 
    SuggesterFilterBackend,
    DefaultOrderingFilterBackend,
    OrderingFilterBackend
)
from django_elasticsearch_dsl_drf.constants import (
    SUGGESTER_COMPLETION,
    LOOKUP_FILTER_TERMS,
    LOOKUP_FILTER_RANGE,
    LOOKUP_FILTER_WILDCARD,
    LOOKUP_FILTER_REGEXP,
    LOOKUP_QUERY_GT,
    LOOKUP_QUERY_GTE,
    LOOKUP_QUERY_LT,
    LOOKUP_QUERY_LTE,
    LOOKUP_QUERY_EXCLUDE,
)
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
    OpenApiParameter,
)
from app_base.serializers import GenericResponseSerializer
from app_base.views import parse_request_data, create_paginate_response, create_message, LoggedValidationError
from app_telegram.documents import RoomDocument, MessageDocument
from app_telegram.serializers import MessageAISearchSerializer, RoomDocumentSerializer, MessageDocumentSerializer
from app_metadata.serializers import EmbeddingDocumentSerializer
from app_telegram.services import api
import logging


# Get an instance of a logger
logger = logging.getLogger(__name__)


class CustomMultiMatchSearchFilter(MultiMatchSearchFilterBackend):
    """ To modify the default param 'search_multi_match', which is ugly and offers
    an over-explanation to the clients, to 'q' 
    """
    search_param = 'q'


class CustomMatchPhraseSearchFilter(BaseSearchFilterBackend):
    """Match phrase search filter backend. To search for exact phrases (group of
     words in specific order)

    https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-match-query-phrase.html

    `django_elasticsearch_dsl_drf` library includes the `query_backends` but not 
    the `filter_backends`.
    """

    search_param = 'qem' # 'Query exact match'

    query_backends = [
        MatchPhraseQueryBackend,
    ]


class ESPaginator(PageNumberPagination):
    """ Custom paginator to limit the number of accesible pages.
    """
    def paginate_queryset(self, queryset, request, view=None):
        
        # Check page parameter
        param_value = request.query_params.get(self.page_query_param, "1")
        if param_value.isdigit():
            page_number = int(param_value)
        else:
            msg = self.invalid_page_message.format(
                message="Error in parameter 'page', contains wrong values."
            )
            raise NotFound(msg)
        
        # Get max pages
        max_pages = (
            settings.REST_FRAMEWORK.get('MAX_PAGES_AUTHENTICATED', 1000) 
            if request.user.is_authenticated 
            else settings.REST_FRAMEWORK.get('MAX_PAGES_ANON', 100) 
        )
        if page_number > max_pages:
            msg = self.invalid_page_message.format(
                page_number=page_number, message="Limit exceeded"
            )
            raise NotFound(msg)
        
        return super().paginate_queryset(queryset, request, view)


# ===============================================================
# ========     TELEGRAM SEARCH VIEWS FUNCTIONALITY       ========
# ===============================================================

# Since drf-spectacular is unable to infer the filter parameters from `django-elasticsearch-dsl-drf`,
# we manually specify them here for proper Swagger documentation.
@extend_schema(
    tags=['Search endpoints'],
    parameters=[
        OpenApiParameter(name='q', description='Free text search in `title` (boosted) and `about` fields.', type=str),
        
        # Ordering
        OpenApiParameter(name='ordering', description='Fields to sort by (e.g., `created_at`, `name`). Default is `-created_at`.', type=str),

        # Name Filters
        OpenApiParameter(name='name', description='Filter by a specific unique name.', type=str),
        OpenApiParameter(name='name__wildcard', description='Filter by unique names using wildcards.', type=str),
        OpenApiParameter(name='name__regexp', description='Filter by unique names using regex.', type=str),
        OpenApiParameter(name='name__terms', description='Filter by a list of unique names (separated by `__`).', type=str),
        OpenApiParameter(name='name__exclude', description='Exclude unique names.', type=str),
        
        # Title Exact Filters
        OpenApiParameter(name='title', description='Filter by exact title.', type=str),
        OpenApiParameter(name='title__wildcard', description='Filter by title using wildcards.', type=str),
        
        # Standard Filters
        OpenApiParameter(name='lang', description='Filter by room language.', type=str),
        OpenApiParameter(name='tag', description='Filter by a specific tag.', type=str),
        OpenApiParameter(name='tag__wildcard', description='Filter by tags using wildcards.', type=str),
        OpenApiParameter(name='tag__regexp', description='Filter by tags using regex.', type=str),
        OpenApiParameter(name='tag__terms', description='Filter by a list of tags (separated by `__`).', type=str),
        OpenApiParameter(name='tag__exclude', description='Exclude tags.', type=str),
        
        OpenApiParameter(name='is_channel', description='Filter if it is a channel (`true` or `false`).', type=bool),
        
        # Date Filters
        OpenApiParameter(name='created_at__range', description='Filter by creation date range (`YYYY-MM-DD__YYYY-MM-DD`).', type=str),
        OpenApiParameter(name='created_at__gt', description='Filter by creation date after X.', type=str),
        OpenApiParameter(name='created_at__gte', description='Filter by creation date after or equal X.', type=str),
        OpenApiParameter(name='created_at__lt', description='Filter by creation date before X.', type=str),
        OpenApiParameter(name='created_at__lte', description='Filter by creation date before or equal X.', type=str),
        
        # Member Filters
        OpenApiParameter(name='members', description='Filter by members\' unique name.', type=str),
        OpenApiParameter(name='members__terms', description='Filter by a list of members (separated by `__`).', type=str),
        
        # Suggesters
        OpenApiParameter(name='title__completion', description='Suggests titles based on the input text.', type=str),
    ]
) 
class RoomSearchView(DocumentViewSet):
    document = RoomDocument
    serializer_class = RoomDocumentSerializer
    pagination_class = ESPaginator
    permission_classes = [AllowAny]

    filter_backends = [
        FilteringFilterBackend,
        CustomMultiMatchSearchFilter,
        SuggesterFilterBackend,
        OrderingFilterBackend,
        DefaultOrderingFilterBackend,
    ]
    
    multi_match_search_fields = {
        'title': {
            'fuzziness': 'AUTO',
            'boost': 3, 
        },
        'about': {
            'fuzziness': 'AUTO' 
        }
    }
    
    multi_match_options = {
        'type': 'best_fields'
    }

    ordering_fields = {
        'created_at': 'created_at',
        'name': 'unique_name', 
    }
    
    ordering = ('-created_at', 'unique_name')

    filter_fields = {
        'name': {
            'field': 'unique_name',
            'lookups': [
                LOOKUP_FILTER_WILDCARD,
                LOOKUP_FILTER_REGEXP,
                LOOKUP_FILTER_TERMS,
                LOOKUP_QUERY_EXCLUDE
            ]
        },
        'title': {
            'field': 'title.keyword',
            'lookups': [
                LOOKUP_FILTER_TERMS,
                LOOKUP_FILTER_WILDCARD,
            ]
        },
        'lang': 'lang',
        'tag': {
            'field': 'tags',
            'lookups': [
                LOOKUP_FILTER_WILDCARD,
                LOOKUP_FILTER_REGEXP,
                LOOKUP_FILTER_TERMS,
                LOOKUP_QUERY_EXCLUDE
            ]
        },
        'is_channel': 'is_channel',
        'created_at':{ 
            'field': 'created_at',
            'lookups': [
                LOOKUP_FILTER_RANGE,
                LOOKUP_QUERY_GT,
                LOOKUP_QUERY_GTE,
                LOOKUP_QUERY_LT,
                LOOKUP_QUERY_LTE,
            ]
        },
        'members': {
            'field': 'members.unique_name',
            'lookups': [
                LOOKUP_FILTER_TERMS, 
                LOOKUP_QUERY_EXCLUDE,
            ]
        }
    }

    suggester_fields = {
        'title': {
            'field': 'title.suggest',
            'suggesters': [
                SUGGESTER_COMPLETION,
            ],
        },  
    }


# Since drf-spectacular is unable to infer the filter parameters from `django-elasticsearch-dsl-drf`,
# we manually specify them here for proper Swagger documentation.
@extend_schema(
    tags=['Search endpoints'],
    parameters=[
        OpenApiParameter(name='q', description='Free text search in `text` and `room_about` fields.', type=str),
        OpenApiParameter(name='qem', description='Exact phrase search in the `text` field.', type=str),
        
        # Room Name Filters (Optimized replacement for room link)
        OpenApiParameter(name='room_name', description='Filter by exact room unique name (e.g., `name`).', type=str),
        OpenApiParameter(name='room_name__wildcard', description='Filter by room name using wildcards (e.g., `name*`).', type=str),
        OpenApiParameter(name='room_name__regexp', description='Filter by room name using regular expressions.', type=str),
        OpenApiParameter(name='room_name__terms', description='Filter by a list of room names (separated by `__`).', type=str),
        OpenApiParameter(name='room_name__exclude', description='Exclude specific rooms (separated by `__`).', type=str),
        
        # Message ID Filters (Optimized numeric search)
        OpenApiParameter(name='msg_id__gt', description='Filter by message ID greater than X.', type=int),
        OpenApiParameter(name='msg_id__lt', description='Filter by message ID less than X.', type=int),
        
        # Standard Filters
        OpenApiParameter(name='lang', description='Filter by message language (2-letter code, e.g., `es`, `en`).', type=str),
        OpenApiParameter(name='tag', description='Filter by a specific tag.', type=str),
        OpenApiParameter(name='tag__wildcard', description='Filter by tags using wildcards (e.g., `tag*`). Note: uses `*` and `?` as wildcards.', type=str),
        OpenApiParameter(name='tag__terms', description='Filter by a list of tags (separated by `__`).', type=str),
        OpenApiParameter(name='tag__exclude', description='Exclude tags from the search (separated by `__`).', type=str),
        OpenApiParameter(name='entity', description='Filter by a specific entity.', type=str),
        OpenApiParameter(name='entity__wildcard', description='Filter by entities using wildcards (e.g., `ent*`). Note: uses `*` and `?` as wildcards.', type=str),
        OpenApiParameter(name='entity__regexp', description='Filter by entities using regular expressions (e.g., `ent.*`). Note: uses standard regex syntax.', type=str),
        OpenApiParameter(name='entity__terms', description='Filter by a list of entities (separated by `__`).', type=str),
        OpenApiParameter(name='entity__exclude', description='Exclude entities from the search (separated by `__`).', type=str),
        
        # Date Filters
        OpenApiParameter(name='created_at__range', description='Filter by date range (format: `YYYY-MM-DD__YYYY-MM-DD`).', type=str),
        OpenApiParameter(name='created_at__gt', description='Filter by dates after the specified one (format: `YYYY-MM-DD`).', type=str),
        OpenApiParameter(name='created_at__gte', description='Filter by dates after or equal to the specified one (format: `YYYY-MM-DD`).', type=str),
        OpenApiParameter(name='created_at__lt', description='Filter by dates before the specified one (format: `YYYY-MM-DD`).', type=str),
        OpenApiParameter(name='created_at__lte', description='Filter by dates before or equal to the specified one (format: `YYYY-MM-DD`).', type=str),
        
        # Misc Filters
        OpenApiParameter(name='is_reply', description='Filter if the message is a reply (`true` or `false`).', type=bool),
        OpenApiParameter(name='media_type__terms', description='Filter by media type (separated by `__`, e.g., `photo__video`).', type=str),
        OpenApiParameter(name='ordering', description='Ordering fields (e.g., `created_at`, `-msg_id`).', type=str),
    ]
)
class MessageSearchView(DocumentViewSet):
    document = MessageDocument
    serializer_class = MessageDocumentSerializer
    pagination_class = ESPaginator
    permission_classes = [AllowAny]

    filter_backends = [
        FilteringFilterBackend,
        CustomMultiMatchSearchFilter,
        CustomMatchPhraseSearchFilter,
        OrderingFilterBackend, # Added to allow ordering by specific fields e.g: ?ordering=created_at or ?ordering=-msg_id
        DefaultOrderingFilterBackend, # Added for consistent pagination with 150M docs
    ]

    # Used in the custom Match phrase filter, to search for exact phrases
    search_fields = ['text']
    
    multi_match_search_fields = {
        'text': {
            'field': 'text',
            'fuzziness': 'AUTO'
        },
        'about': {
            'field': 'room_about',
            'fuzziness': 'AUTO' 
        }
    }
    multi_match_options = {
        'type': 'best_fields'
    }

    # Added to allow sorting by specific fields
    ordering_fields = {
        'created_at': 'created_at',
        'msg_id': 'msg_id',
        'views': 'views',
    }
    ordering = ('-created_at', '-msg_id') # Default ordering

    filter_fields = {
        'lang': 'lang',
        'tag': {
            'field': 'tags',
            'lookups': [
                LOOKUP_FILTER_WILDCARD,
                LOOKUP_FILTER_TERMS,
                LOOKUP_QUERY_EXCLUDE
            ]
        },
        # Optimized Room Filter (Exact Match instead of Wildcard on URL)
        'room_name': {
            'field': 'room_name',
            'lookups': [
                LOOKUP_FILTER_REGEXP,
                LOOKUP_FILTER_TERMS,     # ?room_name__terms=name__others
                LOOKUP_FILTER_WILDCARD,  # ?room_name__wildcard=name* (Fast on keywords)
                LOOKUP_QUERY_EXCLUDE,    # ?room_name__exclude=spam_channel
            ]
        },
        # Optimized Message ID Filter
        'msg_id': {
            'field': 'msg_id',
            'lookups': [
                LOOKUP_QUERY_GT,
                LOOKUP_QUERY_GTE,
                LOOKUP_QUERY_LT,
                LOOKUP_QUERY_LTE,
                LOOKUP_FILTER_RANGE,
            ]
        },
        'entity': {
            'field': 'entities',
            'lookups': [
                LOOKUP_FILTER_WILDCARD,
                LOOKUP_FILTER_REGEXP,
                LOOKUP_FILTER_TERMS,
                LOOKUP_QUERY_EXCLUDE
            ]
        },
        'created_at':{ 
            'field': 'created_at', # Format: yyyy-MM-dd
            'lookups': [
                LOOKUP_FILTER_RANGE,
                LOOKUP_QUERY_GT,
                LOOKUP_QUERY_GTE,
                LOOKUP_QUERY_LT,
                LOOKUP_QUERY_LTE,
            ]
        },
        'is_reply': 'is_reply',
        'media_type': {
            'field': 'media_type',
            'lookups': [
                LOOKUP_FILTER_TERMS,
            ]
        }
    }

class MessagesEmbedSearchView(viewsets.ViewSet):
    @extend_schema(
        parameters=[MessageAISearchSerializer],
        tags=['Search endpoints'],
        summary="Message semantic search.",
        description=(
            "Message semantic search. It uses the embeddings calculation to retrieve "
            "the most similar messages."),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=EmbeddingDocumentSerializer(many=True),
                description="A list of items matching the query.",
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Validation errors."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    @action(methods=['get'], detail=False, url_path='ai', url_name='{basename}-aisearch')
    def ai_search(self, request):
        try:
            params, token = parse_request_data(
                request,
                MessageAISearchSerializer
            )
            queryset = api.msg_search_embeds(token, **params)
            return create_paginate_response(
                token=token,
                queryset=queryset,
                request=request,
                serializer_class=EmbeddingDocumentSerializer,
                #max_items_page = len(...)
            )
        except LoggedValidationError as e:
            logger.info(
                f"{ e.__class__.__name__ }: {e.detail}",
            )
            return create_message(
                e.token, 
                status.HTTP_400_BAD_REQUEST, 
                "There were some issues with the input data.",
                add_errors=e.detail
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, 500)