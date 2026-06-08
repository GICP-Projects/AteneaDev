from rest_framework import viewsets, status
from rest_framework.permissions import IsAdminUser
from drf_spectacular.utils import extend_schema, OpenApiResponse
from app_base.views import parse_request_data, create_message, LoggedValidationError, create_paginate_response
from app_base.serializers import GenericResponseSerializer
from .serializers import StatsMsgSerializer, StatsResultSerializer
from .services.api import get_message_stats
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)

def _normalize_tag_params(params):
    # Rename public query param tag = [..] to internal service arg tags = [...]
    if "tag" in params:
        params["tags"] = params.pop("tag")
    return params

class MessageStatsViewSet(viewsets.ViewSet):
    """
    API ViewSet for message statistics.
    """
    throttle_scope = "staff_api"
    permission_classes = [IsAdminUser]

    @extend_schema(
        parameters=[StatsMsgSerializer],
        tags=['Statistics endpoints'],
        summary="Retrieve message statistics.",
        description=(
            "This endpoint provides message count statistics grouped by a "
            "specific time period and filtered by a date range."
        ),
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=StatsResultSerializer(many=True),
                description="A paginated list of statistics.",
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
                StatsMsgSerializer,
            )
            params = _normalize_tag_params(params)
            stats_qs = get_message_stats(**params)
            
            return create_paginate_response(
                token=token,
                queryset=stats_qs,
                request=request,
                serializer_class=StatsResultSerializer,
                max_items_page=1000
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

