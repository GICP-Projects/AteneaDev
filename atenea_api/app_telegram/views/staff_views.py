from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
from rest_framework.decorators import action
from django.conf import settings
from drf_spectacular.utils import (
    extend_schema,
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
    MessageVectorSerializer,
    MessageDetailSerializer
)
from app_base.serializers import GenericResponseSerializer, PKsSerializer
from app_base.views import (
    manage_crud_request,
    manage_bulk_destroy_request,
    parse_request_data,
    create_message, 
    create_paginate_response,
    LoggedValidationError
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

    @extend_schema(
        request=TelegramAuthSerializer(),
        tags=['Platform configuration'],
        summary="Create a Telegram API credential.",
        description=(
            "Create a Telegram API credential."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=TelegramAuthSerializer(),
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
                response=TelegramAuthSerializer(),
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

    @extend_schema(
        request=SeedItemSerializer(),
        tags=['Ingest endpoints'],
        summary="Create a seed item.",
        description=(
            "Create a seed item with a Telegram resource (Group/Channel/User/Bot)."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=SeedItemSerializer(),
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
                response=SeedItemSerializer(),
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
            "Delete an existing seed."
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
            "Delete a list of seeds by their primary keys (max 1000 items)."
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
        parameters=[SimpleFilterSeedSerializer],
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
    @action(methods=['get'], detail=False, url_path='populate', url_name='{basename}-populate')
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

            num_populated = api.seed_populate(token, **params)
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_populated} seeds will be populated."
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
        parameters=[ScanRoomSerializer],
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
    @action(methods=['get'], detail=False, url_path='scan', url_name='{basename}-scan')
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

            num_rooms = api.room_scanning(token, **params)
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_rooms} channels/groups will be scanned."
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
        parameters=[FilterRoomSerializer],
        tags=['Ingest endpoints'],
        summary="Recalculate the access of any group/channel without valid access.",
        description=(
            "Starts recalculating the access of any group/channel without "
            "valid access. "
        ),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued. <num> channels/groups will be processed."
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
    @action(methods=['get'], detail=False, url_path='access', url_name='{basename}-access')
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

            num_rooms = api.room_access(token, **params)
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_rooms} channels/groups will be processed."
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


class MessageView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]

    @extend_schema(
        parameters=[ScanRepliesMsgSerializer],
        tags=['Ingest endpoints'],
        summary="Starts scanning replies from channels.",
        description=("Starts scanning tasks over the filtered channels (not groups) to extract "
                     "from their messages any reply."),
        responses={
            status.HTTP_202_ACCEPTED: OpenApiResponse(
                response=GenericResponseSerializer,
                description="The tasks have been queued. The message of <num> channels will be scanned."
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
    @action(methods=['get'], detail=False, url_path='scan', url_name='{basename}-scan')
    def scan(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                ScanRepliesMsgSerializer,
            )
            # Rename param tag = [..] to tags = [...]
            if "tag" in params:
                params["tags"] = params.pop("tag")

            num_rooms = api.msg_scanning_comments(token, **params)
            return create_message(
                token, status.HTTP_202_ACCEPTED, 
                f"The tasks have been queued. {num_rooms} channels/groups will be scanned."
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
        parameters=[MediaDownloadSerializer],
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
    @action(methods=['get'], detail=False, url_path='media/download', url_name='{basename}-media-download')
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
        parameters=[ExternalUrlCollectSerializer],
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
    @action(methods=['get'], detail=False, url_path='external-url/collect', url_name='{basename}-external-url-collect')
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
        parameters=[FilterMsgSerializer],
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
    @action(methods=['get'], detail=False, url_path='ner', url_name='{basename}-ner')
    def ner(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            api.msg_ner(token, **params)
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
        parameters=[FilterMsgSerializer],
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
    @action(methods=['get'], detail=False, url_path='index', url_name='{basename}-index')
    def index(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            api.msg_index(token, **params)
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
        parameters=[EmbedFilterMsgSerializer],
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
    @action(methods=['get'], detail=False, url_path='embed', url_name='{basename}-embed')
    def embed(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                EmbedFilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            api.msg_embed(token, **params)
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
        parameters=[FilterMsgSerializer],
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
    @action(methods=['get'], detail=False, url_path='categorize', url_name='{basename}-categorize')
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
        parameters=[FilterMsgSerializer],
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
    @action(methods=['get'], detail=False, url_path='sentiment', url_name='{basename}-sentiment')
    def sentimentalize(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterMsgSerializer,
            )
            params = _normalize_tag_params(params)
            api.msg_sentiment(token, **params)
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
        parameters=[SimpleMessageFilterSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve a paginated list of message embeddings.",
        description=("Retrieve a paginated list of embeddings of the filtered messages."),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=MessageVectorSerializer(many=True),
                description="A paginated list of message embeddings."
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
    @action(methods=['get'], detail=False, url_path='vector', url_name='{basename}-vector')
    def vector(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                SimpleMessageFilterSerializer,
            )
            params = _normalize_tag_params(params)
            
            queryset = api.get_messages_embeddings(**params)
            
            return create_paginate_response(
                token=token,
                queryset=queryset,
                request=request,
                serializer_class=MessageVectorSerializer,
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
        parameters=[SimpleMessageFilterSerializer],
        tags=['Data Retrieval endpoints'],
        summary="Retrieve a list of (filtered) messages.",
        description=(
            "This endpoint provides a paginated list of messages, optionally "
            "filtered by date range and room."
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
        token = None
        try:
            params, token = parse_request_data(
                request,
                SimpleMessageFilterSerializer,
            )
            params = _normalize_tag_params(params)
            queryset = api.get_messages(**params)
            return create_paginate_response(
                token=token,
                queryset=queryset,
                request=request,
                max_items_page=100,
                serializer_class=MessageDetailSerializer,
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
