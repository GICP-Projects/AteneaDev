import logging
from rest_framework import viewsets, status
from rest_framework.permissions import IsAdminUser
from rest_framework.decorators import action
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
from app_metadata.services import api
from app_metadata.serializers import (
    CategoryItemSerializer, 
    FilterDataSerializer
)
from app_base.serializers import GenericResponseSerializer, PKsSerializer
from app_base.views import (
    get_and_log_request_data,
    parse_request_data,
    manage_crud_request,
    manage_bulk_destroy_request,
    create_message, 
    create_paginate_response,
    LoggedValidationError
)


# Get an instance of a logger
logger = logging.getLogger(__name__)


# ===============================================================
# ========       METADATA APP VIEWS FUNCTIONALITY        ========
# ===============================================================

class CategoryView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]

    @extend_schema(
        request=CategoryItemSerializer(),
        tags=['Platform configuration'],
        summary="Create a category.",
        description=(
            "Create a category with its name and description."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=CategoryItemSerializer(),
                description="The created category."
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
                ModelSerializerClass=CategoryItemSerializer,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR) 
        
    @extend_schema(
        request=CategoryItemSerializer(many=True, max_length=100),
        tags=['Platform configuration'],
        summary="Insert categories from a JSON list.",
        description=(
            "Not duplicates allowed, a validation error will be raised."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=GenericResponseSerializer,
                description=(
                    "Created items will be listed in `results` and and invalid items "
                    "will be listed in `errors`. The errors will be a dictionary with "
                    "the invalid category name as key and the errors as value."
                ),
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
    @action(methods=['post'], detail=False, url_path='bulk', url_name='{basename}-bulk')
    def bulk(self, request):
        token = None
        try:
            data, token = parse_request_data(
                request,
                CategoryItemSerializer,
                # To Allow a list of items and apply a limit
                extra_args={"many": True, "max_length": 100}, 
            )

            cats_to_create = data.get("items", [])
            if cats_to_create:
                code = status.HTTP_200_OK
                created_items = api.category_bulk_create(token, cats_to_create)
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
                add_results=CategoryItemSerializer(created_items, many=True).data,
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
        request=CategoryItemSerializer(partial=True),
        tags=['Platform configuration'],
        summary="Update a category.",
        description=(
            "Update an existing category."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=CategoryItemSerializer(),
                description="The updated category."
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
                ModelSerializerClass=CategoryItemSerializer,
                item_pk=pk,
                extra_args={"partial": True},
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  

    @extend_schema(
        parameters=[FilterDataSerializer],
        tags=['Platform configuration'],
        summary="Retrieve a list of categories.",
        description="This endpoint provides a list of categories, optionally filtered by name or description.",
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=CategoryItemSerializer(many=True),
                description="A list of categories matching the filters.",
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Validation errors."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error occurred."
            )
        },
    )
    def list(self, request):
        token = None
        try:
            params, token = parse_request_data(
                request,
                FilterDataSerializer,
            )
            cat_queryset = api.dataitem_search(
                token=token, 
                dataitem_model_class=CategoryItemSerializer.Meta.model,
                **params
            )
            return create_paginate_response(
                token=token,
                queryset=cat_queryset,
                request=request,
                serializer_class=CategoryItemSerializer,
                max_items_page = cat_queryset.count()
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
        tags=['Platform configuration'],
        summary="Recalculate the embeddings for all the categories.",
        description=(
            "Recalculate the embeddings for all the categories."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Your request has been sent."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Unexpected error ocurred."
            )
        },
    )
    @action(methods=['get'], detail=False, url_path='recalculate', url_name='{basename}-recalculate')
    def recalculate(self, request):
        token = None
        try:
            _, token = get_and_log_request_data(
                request=request,
            )
            api.dataitem_recalculate(token, CategoryItemSerializer.Meta.model)
            return create_message(
                token=token,
                status_code=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        tags=['Platform configuration'],
        summary="Delete a sentiment.",
        description=(
            "Delete an existing sentiment."
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
                ModelSerializerClass=CategoryItemSerializer,
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
        summary="Delete a list of categories.",
        description=(
            "Delete a list of categories by their primary keys (max 1000 items)."
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
                ModelSerializerClass=CategoryItemSerializer,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  