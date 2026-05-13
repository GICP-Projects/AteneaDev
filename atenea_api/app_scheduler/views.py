from rest_framework import viewsets, status
from rest_framework.permissions import IsAdminUser
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
from app_scheduler.serializers import (
    PeriodicTaskSerializer,
    FilterPeriodicTaskSerializer, 
)
from app_base.serializers import GenericResponseSerializer
from app_base.views import (
    manage_crud_request,
    create_message, 
    parse_request_data,
    create_paginate_response,
    LoggedValidationError
)
from app_scheduler.services import api
import logging


# Get an instance of a logger
logger = logging.getLogger(__name__)

# ===============================================================
# ========       TELEGRAM APP VIEWS FUNCTIONALITY        ========
# ===============================================================

class SchedulerStaffView(viewsets.ViewSet):
    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests     
    permission_classes = [IsAdminUser]

    @extend_schema(
        request=PeriodicTaskSerializer(),
        tags=['Platform configuration'],
        summary="Create a scheduled task.",
        description=(
            "Create a PeriodicTask item from any available task. Add to this "
            "scheduled task a single scheduled type ('IntervalSchedule', 'ClockedSchedule', "
            "'CrontabSchedule', 'PeriodicTask')."
        ),
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=PeriodicTaskSerializer(),
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
                ModelSerializerClass=PeriodicTaskSerializer,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR) 
        
    @extend_schema(
        parameters=[FilterPeriodicTaskSerializer],
        tags=['Platform configuration'],
        summary="Search scheduled tasks.",
        description=(
            "This endpoint provides a list of scheduled tasks, optionally "
            "filtered by task, name, description os schudule type."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=PeriodicTaskSerializer(many=True),
                description="A list of scheduled tasks matching the filters.",
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
                FilterPeriodicTaskSerializer,
            )
            queryset = api.search(**params)
            return create_paginate_response(
                token=token,
                queryset=queryset,
                request=request,
                serializer_class=PeriodicTaskSerializer,
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
        request=PeriodicTaskSerializer(partial=True),
        tags=['Platform configuration'],
        summary="Update a scheduled task.",
        description=(
            "Update an existing PeriodicTask item."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=PeriodicTaskSerializer(),
                description="The updated scheduled task.",
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
                ModelSerializerClass=PeriodicTaskSerializer,
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
        summary="Delete a scheduled task.",
        description=(
            "Delete an existing PeriodicTask item."
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
                ModelSerializerClass=PeriodicTaskSerializer,
                item_pk=pk,
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)  