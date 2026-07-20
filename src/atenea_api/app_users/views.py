from rest_framework import viewsets, status
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from app_users.serializers import UserSerializer
from app_base.serializers import GenericResponseSerializer
from app_base.views import (
    manage_crud_request,
    create_message,
)
import logging


# Get an instance of a logger
logger = logging.getLogger(__name__)

# ===============================================================
# ========         USER APP VIEWS FUNCTIONALITY          ========
# ===============================================================

class UserAuthView(viewsets.ViewSet):
    """
    A viewset to create new endpoints related to User authentication (e.g: register).
    """
    permission_classes = [AllowAny]  # Allow anyone to access this endpoint
    
    @extend_schema(
        request=UserSerializer,
        responses={
            status.HTTP_201_CREATED: OpenApiResponse(
                response=UserSerializer(),
                description="The created user."
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(
                response=GenericResponseSerializer,
                description="Validation errors."
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(
                response=GenericResponseSerializer,
                description="An unexpected error occurred."
            )
        },
        tags=['oauth'],
        summary="Register a new user.",
        description="Create a new user account with email, username, and password."
    )
    @action(methods=['post'], detail=False, url_path='register', url_name='{basename}-register')
    def register(self, request):
        token = None
        try:
            return manage_crud_request(
                request,
                crud_action="create",
                ModelSerializerClass=UserSerializer,
                to_log_query=True,
                hide_data_in_query=True,  # Hide sensitive data like passwords
            )
        except Exception as e:
            logger.error(f"{e.__class__.__name__}: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)


class UserView(viewsets.ViewSet):
    pass