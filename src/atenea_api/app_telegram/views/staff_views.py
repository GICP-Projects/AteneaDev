from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
)
from app_telegram.services import api
from app_telegram.serializers import (
    SeedItemSerializer,
    SimpleFilterSeedSerializer,
    FullFilterSeedSerializer,
    FilterRoomSerializer, 
    ScanRoomSerializer,
    FilterMsgSerializer,
    NERFilterMsgSerializer,
    ScanRepliesMsgSerializer,
    EmbedFilterMsgSerializer,
    DownloadableCatalogSerializer,
    DownloadableDeleteSerializer,
    ExternalUrlCollectSerializer,
    MediaDownloadSerializer,
    MediaDownloadStatusSerializer,
    TelegramAuthSerializer,
    FilterTelegramAuthSerializer,
    SimpleMessageFilterSerializer,
    MessageRetrievalFilterSerializer,
    MessageVectorSerializer,
    MessageDetailSerializer,
    EntityRankingFilterSerializer,
    EntityRankingResponseSerializer,
)
from app_base.mixins import CountMixin
from app_telegram.pagination import EntityRankingPaginator
from app_telegram.services.analytics import (
    entity_ranking_ordering, entity_ranking_results,
)
from app_base.serializers import (
    CountResponseSerializer,
    GenericResponseSerializer,
    GenericResultResponseSerializer,
    PKsSerializer,
)
from app_base.views import (
    manage_crud_request,
    manage_bulk_destroy_request,
    parse_request_data,
    create_message, 
    create_paginate_response,
    LoggedValidationError,
    BasePaginator,
    CursorPaginator,
)
import logging


# Get an instance of a logger
logger = logging.getLogger(__name__)


def _normalize_tag_params(params):
    # Rename public query param tag = [..] to internal service arg tags = [...]
    if "tag" in params:
        params["tags"] = params.pop("tag")
    return params


# ===============================================================
# ========       TELEGRAM APP VIEWS FUNCTIONALITY        ========
# ===============================================================

class TelegramAuthView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]
    pagination_class = BasePaginator

    @extend_schema(
        request=TelegramAuthSerializer(),
        tags=['Platform configuration'],
        summary="Create a Telegram API credential.",
        description=(
            "Create a Telegram API credential."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=GenericResultResponseSerializer,
                description="The created Telegram credential."
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
    def create(self, request):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="create", 
                ModelSerializerClass=TelegramAuthSerializer,
                hide_data_in_query=True
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)    

    @extend_schema(
        parameters=[FilterTelegramAuthSerializer],
        tags=['Platform configuration'],
        summary="Search Telegram API credentials.",
        description=(
            "This endpoint provides a list of Telegram API credentials, optionally "
            "filtered by name."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=TelegramAuthSerializer(many=True),
                description="A list of Telegram API credentials matching the filters.",
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
    def list(self, request):
        token = None
        try:
            data, token = parse_request_data(
                request,
                FilterTelegramAuthSerializer,
            )
            queryset = api.tgauth_search(**data)
            return create_paginate_response(
                token=token,
                queryset=queryset,
                request=request,
                serializer_class=TelegramAuthSerializer,
                #max_items_page = len(queryset)
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)   

    @extend_schema(
        request=TelegramAuthSerializer(partial=True),
        tags=['Platform configuration'],
        summary="Update a Telegram API credential.",
        description=(
            "Update an existing TelegramAuth item."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=GenericResultResponseSerializer,
                description="The updated Telegram API credential.",
            ), 
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Validation errors."
            ),       
            status.HTTP_404_NOT_FOUND: OpenApiResponse(
                response=GenericResponseSerializer,
                description="There is no element with that id."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    def partial_update(self, request, pk=None):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="update", 
                ModelSerializerClass=TelegramAuthSerializer,
                item_pk=pk,
                extra_args={"partial": True},
                hide_data_in_query=True
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  

    @extend_schema(
        tags=['Platform configuration'],
        summary="Delete a Telegram API credential.",
        description=(
            "Delete an existing TelegramAuth item."
        ),
        responses={
            status.HTTP_204_NO_CONTENT: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Succesfully deleted."
            ),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(
                response=GenericResponseSerializer,
                description="There is no element with that id."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    def destroy(self, request, pk=None):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="destroy",
                ModelSerializerClass=TelegramAuthSerializer,
                item_pk=pk,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  


class SeedView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]
    pagination_class = BasePaginator

    @extend_schema(
        request=SeedItemSerializer(),
        tags=['Ingest endpoints'],
        summary="Create a seed item.",
        description=(
            "Create a seed item with a Telegram resource (Group/Channel/User/Bot)."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=GenericResultResponseSerializer,
                description="The created SeedItem."
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
    def create(self, request):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="create", 
                ModelSerializerClass=SeedItemSerializer,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR) 
        
    @extend_schema(
        request=SeedItemSerializer(many=True, max_length=1000),
        tags=['Ingest endpoints'],
        summary="Insert Telegram basic info (Group/Channel/User/Bot) from a JSON list.",
        description=(
            "This bulk create endpoint will try to create as many items as possible "
            "('Best Effort philosophy') and will return the created items and the invalid ones."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=GenericResponseSerializer,
                description=(
                    "Created items will be listed in `results` and and invalid items "
                    "will be listed in `errors`. The errors will be a dictionary with "
                    "the invalid link as key and the errors as value."
                ),
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description=(
                    "Same as 200 but with the `created_items` key set to an empty list."
                ),
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    @action(methods=['post'], detail=False, url_path='bulk', url_name='{basename}-bulk')
    def bulk(self, request):
        token = None
        try:
            data, token = parse_request_data(
                request,
                SeedItemSerializer,
                # To Allow a list of items and apply a limit
                extra_args={"many": True, "min_length": 1, "max_length": 1000}, 
            )

            seeds_to_create = data.get("items", [])
            if seeds_to_create:
                code = status.HTTP_200_OK
                created_items = api.seed_bulk_create(token, seeds_to_create)
                message = (
                    f"{len(created_items)} items have been created." +  
                    ("" if not len(data.get('invalid_items', {})) else 
                    f" It has been impossible to process {len(data.get('invalid_items', {}))} items.")
                )
            else: 
                code = status.HTTP_400_BAD_REQUEST
                created_items = []
                message = "No items have been created."
            return create_message(
                token=token, 
                status_code=code, 
                custom_message=message,
                # Model items are not JSON serializable, so we have to send them serialized
                add_results=SeedItemSerializer(created_items, many=True).data,
                add_errors=data.get("invalid_items", {})
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        parameters=[FullFilterSeedSerializer],
        tags=['Data Retrieval endpoints'],
        summary="List or search seeds.",
        description=(
            "This endpoint provides a list of seeds, optionally you can use parameters "
            "to filter the data."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=SeedItemSerializer(many=True),
                description="A list of seeds matching the filters.",
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
    def list(self, request):
        token = None
        try:
            seed_filters, token = parse_request_data(
                request,
                FullFilterSeedSerializer,
            )
            # Rename param tag = [..] to tags = [...]
            if "tag" in seed_filters:
                seed_filters["tags"] = seed_filters.pop("tag")

            seeds_qs = api.seed_list(token, **seed_filters)
            return create_paginate_response(
                token=token,
                queryset=seeds_qs,
                request=request,
                serializer_class=SeedItemSerializer,
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=SeedItemSerializer(partial=True),
        tags=['Platform configuration'],
        summary="Update a seed.",
        description=(
            "Update an existing seed."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=GenericResultResponseSerializer,
                description="The updated seed.",
            ), 
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Validation errors."
            ),       
            status.HTTP_404_NOT_FOUND: OpenApiResponse(
                response=GenericResponseSerializer,
                description="There is no element with that id."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    def partial_update(self, request, pk=None):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="update", 
                ModelSerializerClass=SeedItemSerializer,
                item_pk=pk,
                extra_args={"partial": True},
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  

    @extend_schema(
        tags=['Platform configuration'],
        summary="Delete a seed.",
        description=(
            "Delete an existing seed. Related rooms, users and collected messages "
            "are preserved; their seed references are cleared."
        ),
        responses={
            status.HTTP_204_NO_CONTENT: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Succesfully deleted."
            ),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(
                response=GenericResponseSerializer,
                description="There is no element with that id."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    def destroy(self, request, pk=None):
        token = None
        try:
            return manage_crud_request(
                request, 
                crud_action="destroy",
                ModelSerializerClass=SeedItemSerializer,
                item_pk=pk,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  
        
    @extend_schema(
        request=PKsSerializer(),
        tags=['Platform configuration'],
        summary="Delete a list of seeds.",
        description=(
            "Delete a list of seeds by their primary keys (max 1000 items). "
            "Related rooms, users and collected messages are preserved; "
            "their seed references are cleared."
        ),
        responses={
            status.HTTP_204_NO_CONTENT: None,
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
    @action(methods=['post'], detail=False, url_path='delete/bulk', url_name='{basename}-delete-bulk')
    def bulk_destroy(self, request):
        # post must be used due to the current specs of HTTP (https://stackoverflow.com/a/299696)
        token = None
        try:
            return manage_bulk_destroy_request(
                request=request,
                ModelSerializerClass=SeedItemSerializer,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  

    @extend_schema(
        request=SimpleFilterSeedSerializer(),
        tags=['Ingest endpoints'],
        summary="Starts populating.",
        description=("It takes all the filtered seeds that haven't yet been added "
                     "to the data pool so that they can be monitored."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued. <num> seeds will be populated."
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
    @action(methods=['post'], detail=False, url_path='populate', url_name='{basename}-populate')
    def populate(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                SimpleFilterSeedSerializer,
            )
            # Rename param tag = [..] to tags = [...]
            if "tag" in params:
                params["tags"] = params.pop("tag")

            populate_result = api.seed_populate(token, **params)
            num_populated = (
                populate_result["items_count"]
                if isinstance(populate_result, dict)
                else populate_result
            )
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_populated} seeds will be populated.",
                add_results=(
                    {"job_id": populate_result["job_id"]}
                    if isinstance(populate_result, dict)
                    else None
                ),
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)


class RoomView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]

    @extend_schema(
        request=ScanRoomSerializer(),
        tags=['Ingest endpoints'],
        summary="Starts scanning.",
        description=("Starts scanning tasks over the filtered rooms to extract "
                     "messages and users from them."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued. <num> channels/groups will be scanned."
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
    @action(methods=['post'], detail=False, url_path='scan', url_name='{basename}-scan')
    def scan(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                ScanRoomSerializer,
            )
            # Rename param tag = [..] to tags = [...]
            if "tag" in params:
                params["tags"] = params.pop("tag")

            scan_result = api.room_scanning(token, **params)
            # Keep the view compatible with integrations that still return the
            # historic integer while exposing a live Job from the new service.
            num_rooms = scan_result["items_count"] if isinstance(scan_result, dict) else scan_result
            job_result = {"job_id": scan_result["job_id"]} if isinstance(scan_result, dict) else None
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_rooms} channels/groups will be scanned.",
                add_results=job_result,
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=FilterRoomSerializer(),
        tags=['Ingest endpoints'],
        summary="Recalculate the access of any group/channel without valid access.",
        description=(
            "Starts recalculating the access of any group/channel without "
            "valid access. "
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="A durable access Job was queued for <num> channels/groups; the response includes its job_id."
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
    @action(methods=['post'], detail=False, url_path='access', url_name='{basename}-access')
    def access(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterRoomSerializer,
            )

            # Rename param tag = [..] to tags = [...]
            if "tag" in params:
                params["tags"] = params.pop("tag")

            access_result = api.room_access(token, **params)
            num_rooms = (
                access_result["items_count"]
                if isinstance(access_result, dict)
                else access_result
            )
            job_result = (
                {"job_id": access_result["job_id"]}
                if isinstance(access_result, dict)
                else None
            )
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The access ProcessingJob has been queued. {num_rooms} channels/groups will be processed.",
                add_results=job_result,
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema_view(
    count=extend_schema(
        parameters=[MessageRetrievalFilterSerializer],
        tags=["Data Retrieval endpoints"],
        summary="Count stored messages matching the filters.",
        description=(
            "Count the complete filtered selection without pagination. "
            "The total reflects the time of this request; data may change during cursor traversal."
        ),
    ),
)
class MessageView(CountMixin, viewsets.GenericViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api"
    permission_classes = [IsAdminUser]
    pagination_class = CursorPaginator
    serializer_class = MessageDetailSerializer

    # Let any authenticated account query message data while preserving the
    # admin-only policy for processing and other operational actions.
    authenticated_read_actions = {
        "list",
        "count",
        "vector",
        "vector_count",
        "entities",
    }

    def get_permissions(self):
        permission_classes = (
            [IsAuthenticated]
            if self.action in self.authenticated_read_actions
            else self.permission_classes
        )
        return [permission() for permission in permission_classes]

    def get_message_filters(self):
        """Validate and log once, even if the queryset is requested again."""
        if not hasattr(self, "_message_filters"):
            params, self.query_token = parse_request_data(
                self.request, MessageRetrievalFilterSerializer,
            )
            self._message_filters = _normalize_tag_params(params)
        return self._message_filters

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return MessageDetailSerializer.Meta.model.objects.none()
        params = self.get_message_filters()
        if self.action in {"vector", "vector_count"}:
            return api.get_messages_embeddings(**params)
        return api.get_messages(**params)

    def _message_list_response(self, request, serializer_class, page_size):
        try:
            queryset = self.filter_queryset(self.get_queryset())
            return create_paginate_response(
                token=self.query_token,
                queryset=queryset,
                request=request,
                serializer_class=serializer_class,
                max_items_page=page_size,
                pagination_class=self.pagination_class,
                pagination_kwargs={"ordering": ("created_at", "id")},
            )
        except ValidationError as error:
            return create_message(
                getattr(error, "token", None) or getattr(self, "query_token", None),
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=error.detail,
            )
        except Exception:
            logger.exception("Unable to retrieve messages.")
            return create_message(getattr(self, "query_token", None), status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        parameters=[EntityRankingFilterSerializer],
        tags=["Data Retrieval endpoints"],
        summary="Rank entities within filtered messages.",
        description=(
            "Ranks per-message entity facts and sums their occurrences. "
            "Uses /tg/msg room, tag, tag_match, date, stored_since and reply filters, "
            "plus entity_type and entity_name. "
            "order=messages (default) sorts by message_count, then appearance_count; "
            "order=appearances reverses those priorities. Entity UUID breaks ties. "
            "Follow signed next/previous cursors; page-size defaults to 60, maximum 100. "
            "No total ranking count is computed. Each result includes messages_url for "
            "paginated access to its message IDs and links with the same filters. "
            "Counts cover messages with current NER annotations. "
            "Pagination reads live data; changing counts can move entities between pages. "
            "Each page still aggregates the matching counts."
        ),
        responses={200: EntityRankingResponseSerializer, 400: GenericResponseSerializer},
    )
    @action(methods=["get"], detail=False, url_path="entities", pagination_class=None)
    def entities(self, request):
        token = None
        try:
            params, token = parse_request_data(request, EntityRankingFilterSerializer)
            params = _normalize_tag_params(params)
            entity_type = params.pop("entity_type", None)
            entity_name = params.pop("entity_name", None)
            order = params.pop("order")
            params.pop("cursor", None)
            page_size = params.pop("page_size", 60)
            paginator = EntityRankingPaginator(
                page_size=page_size, is_authenticated=True,
                ordering=entity_ranking_ordering(order),
            )
            page = paginator.paginate_ranking(
                params, request, entity_type=entity_type, entity_name=entity_name,
                ordering=entity_ranking_ordering(order),
            )
            results = entity_ranking_results(
                page, params, messages_url=request.build_absolute_uri(reverse("msg-staff-list")),
            )
            return paginator.get_paginated_response(results, token=token)
        except ValidationError as error:
            return create_message(
                getattr(error, "token", None) or token, status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.", add_errors=error.detail,
            )
        except Exception:
            logger.exception("Unable to rank message entities.")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=ScanRepliesMsgSerializer(),
        tags=['Ingest endpoints'],
        summary="Starts scanning replies from channels.",
        description=("Selects parent messages from eligible channels and scans their discussion "
                     "comments in credential-specific message batches."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued; results report selected parent messages and rooms."
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
    @action(methods=['post'], detail=False, url_path='scan', url_name='{basename}-scan')
    def scan(self, request):
        """Validate a comments launch and return selected parent/room counts.

        The request supplies message filters and per-parent limits. Persist a
        query and durable root/child Jobs through the API; return HTTP 202 with
        their IDs and counts, or a validation/server error response.
        """
        token = None
        try:
            params, token = parse_request_data(
                request,
                ScanRepliesMsgSerializer,
            )
            # Rename param tag = [..] to tags = [...]
            if "tag" in params:
                params["tags"] = params.pop("tag")

            scan_result = api.msg_scanning_comments(token, **params)
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {scan_result['items_count']} parent messages "
                f"in {scan_result['rooms_count']} channels will be scanned for comments.",
                add_results=scan_result,
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=MediaDownloadSerializer(),
        tags=['Data processing endpoints'],
        summary="Download Telegram media files from already scanned messages.",
        description=(
            "Queues media download tasks for messages already stored in Atenea. "
            "Only PHOTO, VIDEO, AUDIO, DOCUMENT and GIF messages are considered."
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The media download scheduler has been queued."
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(response=GenericResponseSerializer),
        },
    )
    @action(methods=['post'], detail=False, url_path='media/download', url_name='{basename}-media-download')
    def media_download(self, request):
        token = None
        try:
            params, token = parse_request_data(request, MediaDownloadSerializer)
            if "tag" in params:
                params["tags"] = params.pop("tag")
            api.msg_media_download(token, **params)
            return create_message(
                token,
                status.HTTP_202_ACCEPTED,
                "The media download scheduler has been queued. Use the returned token to inspect scheduling and worker progress."
            )
        except LoggedValidationError as e:
            logger.info(f"{ e.__class__.__name__ }: {e.detail}")
            return create_message(
                e.token,
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=e.detail
            )
        except Exception as e:
            logger.error(f"{ e.__class__.__name__ }: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        parameters=[MediaDownloadStatusSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve progress for a Telegram media download request.",
        description=(
            "Returns Redis-backed progress counters for a media download request "
            "using the token returned by /tg/msg/media/download."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(description="Media download progress."),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(response=GenericResponseSerializer),
        },
    )
    @action(methods=['get'], detail=False, url_path='media/download/status', url_name='{basename}-media-download-status')
    def media_download_status(self, request):
        token = None
        try:
            params, _ = parse_request_data(
                request,
                MediaDownloadStatusSerializer,
                to_log_query=False,
            )
            token = params["token"]
            progress = api.msg_media_download_progress(token)
            if not progress:
                return create_message(
                    token,
                    status.HTTP_404_NOT_FOUND,
                    "No media download progress was found for that token."
                )
            return Response(
                {
                    "token": str(token),
                    "results": progress,
                },
                status=status.HTTP_200_OK,
            )
        except LoggedValidationError as e:
            logger.info(f"{ e.__class__.__name__ }: {e.detail}")
            return create_message(
                e.token,
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=e.detail
            )
        except Exception as e:
            logger.error(f"{ e.__class__.__name__ }: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=ExternalUrlCollectSerializer(),
        tags=['Data processing endpoints'],
        summary="Collect external downloadable URLs from already scanned messages.",
        description=(
            "Queues tasks to persist whitelisted download/cloud URLs detected in "
            "message URL entities or, optionally, message text."
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(response=GenericResponseSerializer),
        },
    )
    @action(methods=['post'], detail=False, url_path='external-url/collect', url_name='{basename}-external-url-collect')
    def external_url_collect(self, request):
        token = None
        try:
            params, token = parse_request_data(request, ExternalUrlCollectSerializer)
            if "tag" in params:
                params["tags"] = params.pop("tag")
            num_messages = api.msg_external_url_collect(token, **params)
            return create_message(
                token,
                status.HTTP_202_ACCEPTED,
                f"The external URL collection tasks have been queued. {num_messages} messages matched the filters."
            )
        except LoggedValidationError as e:
            logger.info(f"{ e.__class__.__name__ }: {e.detail}")
            return create_message(
                e.token,
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=e.detail
            )
        except Exception as e:
            logger.error(f"{ e.__class__.__name__ }: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        methods=["GET"],
        parameters=[DownloadableCatalogSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve downloadable media and external URLs grouped by channel.",
        description=(
            "Returns bucket media download links and whitelisted external download "
            "URLs grouped by Telegram channel/group."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(description="Grouped downloadable catalog."),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(response=GenericResponseSerializer),
        },
    )
    @extend_schema(
        methods=["DELETE"],
        parameters=[DownloadableDeleteSerializer],
        tags=['Data processing endpoints'],
        summary="Delete downloaded bucket media.",
        description=(
            "Deletes matching S3/MinIO media objects and marks their metadata as "
            "deleted. This endpoint only targets bucket media currently marked as "
            "downloaded; external URLs are catalog-only references and are never "
            "deleted here. At least one room or tag filter is required to avoid "
            "accidental broad deletes. dry_run defaults to false, so use "
            "dry_run=true explicitly to preview the matched count before deleting. "
            "If the match count is "
            f"greater than {settings.MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD}, the request "
            "must include confirm=true. Example: "
            "DELETE /api/v1/tg/msg/downloadable?room=<room>&ext=mp4&dry_run=true"
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(description="Downloadable items deleted."),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(response=GenericResponseSerializer),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(response=GenericResponseSerializer),
        },
    )
    @action(methods=['get', 'delete'], detail=False, url_path='downloadable', url_name='{basename}-downloadable')
    def downloadable(self, request):
        token = None
        try:
            if request.method == "DELETE":
                params, token = parse_request_data(request, DownloadableDeleteSerializer)
                if "tag" in params:
                    params["tags"] = params.pop("tag")
                results = api.msg_downloadable_delete(**params)
                return Response(
                    {
                        "token": token,
                        "results": results,
                    },
                    status=status.HTTP_200_OK,
                )

            params, token = parse_request_data(request, DownloadableCatalogSerializer)
            if "tag" in params:
                params["tags"] = params.pop("tag")
            results = api.msg_downloadable_catalog(**params)
            return Response(
                {
                    "token": token,
                    "count": len(results),
                    "results": results,
                },
                status=status.HTTP_200_OK,
            )
        except LoggedValidationError as e:
            logger.info(f"{ e.__class__.__name__ }: {e.detail}")
            return create_message(
                e.token,
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=e.detail
            )
        except Exception as e:
            logger.error(f"{ e.__class__.__name__ }: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=NERFilterMsgSerializer(),
        tags=['Data processing endpoints'],
        summary="Process name entity recognition (NER) in a list of messages.",
        description=("Process name entity recognition (NER) in the filtered messages."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued."
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
    @action(methods=['post'], detail=False, url_path='ner', url_name='{basename}-ner')
    def ner(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                NERFilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            job, created = api.msg_ner(token, **params)
            return create_message(
                token,
                status.HTTP_202_ACCEPTED,
                add_results={"job_id": str(job.id), "created": created},
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=FilterMsgSerializer(),
        tags=['Data processing endpoints'],
        summary="Index a list of messages.",
        description=("Index a list of messages."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued."
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
    @action(methods=['post'], detail=False, url_path='index', url_name='{basename}-index')
    def index(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            job_id = api.msg_index(token, **params)
            return create_message(token, status.HTTP_202_ACCEPTED, add_results={"job_id": str(job_id)})
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=EmbedFilterMsgSerializer(),
        tags=['Data processing endpoints'],
        summary="Process embeddings calculation to a list of messages.",
        description=("Process embeddings calculation to a list of messages."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued."
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
    @action(methods=['post'], detail=False, url_path='embed', url_name='{basename}-embed')
    def embed(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                EmbedFilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            job_id = api.msg_embed(token, **params)
            return create_message(token, status.HTTP_202_ACCEPTED, add_results={"job_id": str(job_id)})
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=FilterMsgSerializer(),
        tags=['Data processing endpoints'],
        summary="Perform zero-shot classification to a list of messages.",
        description=(
            "Perform zero-shot classification to a list of messages. "
            "They need to have their 'category' embeddings slot calculated."
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued."
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
    @action(methods=['post'], detail=False, url_path='categorize', url_name='{basename}-categorize')
    def categorize(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            api.msg_categorize(token, **params)
            return create_message(token, status.HTTP_202_ACCEPTED)
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        request=FilterMsgSerializer(),
        tags=['Data processing endpoints'],
        summary="Perform zero-shot classification to a list of messages.",
        description=(
            "Perform sentiment analysis to a list of messages. "
            "They need to have their 'sentiment' embeddings slot calculated."
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued."
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
    @action(methods=['post'], detail=False, url_path='sentiment', url_name='{basename}-sentiment')
    def sentimentalize(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            job_id = api.msg_sentiment(token, **params)
            return create_message(token, status.HTTP_202_ACCEPTED, add_results={"job_id": str(job_id)})
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
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        parameters=[MessageRetrievalFilterSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve a paginated list of message embeddings.",
        description=(
            "Retrieve messages with embeddings in ascending created_at, id order, "
            "with null dates last. Start without cursor and follow next/previous links; "
            "the default page-size is 10, with a maximum of 100. The cursor is signed, "
            "not encrypted, and is bound to this endpoint and its filters. "
            "Sending page or an invalid cursor returns HTTP 400. Lists contain no count; "
            "request /api/v1/tg/msg/vector/count separately when needed. "
            "Traversal reads live data rather than a fixed snapshot."
        ),
        responses={
            status.HTTP_200_OK: MessageVectorSerializer(many=True),
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
    @action(methods=['get'], detail=False, url_path='vector', url_name='{basename}-vector')
    def vector(self, request):
        return self._message_list_response(request, MessageVectorSerializer, 10)

    @extend_schema(
        parameters=[MessageRetrievalFilterSerializer],
        tags=["Data Retrieval endpoints"],
        summary="Count messages with embeddings matching the filters.",
        description=(
            "Count the complete filtered selection of messages with embeddings, without pagination. "
            "The total reflects the time of this request; data may change during cursor traversal."
        ),
        responses={
            status.HTTP_200_OK: CountResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
        },
    )
    @action(methods=["get"], detail=False, url_path="vector/count", pagination_class=None)
    def vector_count(self, request):
        return self.count(request)

    @extend_schema(
        parameters=[MessageRetrievalFilterSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve a list of (filtered) messages.",
        description=(
            "Retrieve stored messages, optionally filtered by date range, room and tags, "
            "in ascending created_at, id order with null dates last. Start without cursor "
            "and follow next/previous links; page-size defaults to 100 and is capped at 100. "
            "The cursor is signed, not encrypted, and is bound to this endpoint and its filters. "
            "Sending page or an invalid cursor returns HTTP 400. Lists contain no count; "
            "request /api/v1/tg/msg/count separately when needed. "
            "Traversal reads live data rather than a fixed snapshot."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=MessageDetailSerializer(many=True),
                description="A paginated list of messages matching the filters.",
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
    def list(self, request):
        return self._message_list_response(request, MessageDetailSerializer, 100)
