from rest_framework import viewsets, status
from rest_framework.decorators import action
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
from rest_framework_api_key.permissions import HasAPIKey
from django.db.models import Func, F
from django.db.models.functions import Lower
from app_telegram.models import RoomItem
from app_frontend.serializers import StrListSerializer
from app_base.languages import LANGS_ISO_639_1
from app_base.views import (
    log_query, 
    create_message, 
    create_paginate_response
)
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)


# ======================================================
# =====          VIEWS RELATED TO FRONT            =====
# ======================================================
class FrontFormView(viewsets.ViewSet):
    """ Front views to fill web forms.
    """
    permission_classes = [HasAPIKey]

    @extend_schema(
        summary="Retrieve a list of tags used in the groups/channels.",
        description="This endpoint provides a list of tags, optionally ",
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=StrListSerializer(),
                description="A list of tags.",
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(description="Unexpected error occurred.")
        },
    )
    @action(methods=['get'], detail=False, url_path='tags', url_name='{basename}-tag-list')
    def list_tags(self, request):
        token = None
        try:
            token = log_query(request) # Create Query 
            room_tags = (
                RoomItem.objects.annotate(tag=Func(F('tags'), function='unnest'))
                .annotate(tag=Lower("tag")) # Lowercase to normalize
                .order_by('tag')
                .distinct('tag')
                .values_list("tag", flat=True)
            )
            return create_paginate_response(
                token=token,
                queryset=room_tags,
                request=request,
                serializer_class=StrListSerializer,
                many=False,
                max_items_page = room_tags.count()
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Retrieve a list with all the allowed ISO-639-1 language codes.",
        description="Retrieve a list with all the allowed ISO-639-1 language codes.",
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=StrListSerializer(),
                description="A list of ISO-639-1 language codes.",
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(description="Unexpected error occurred.")
        },
    )
    @action(methods=['get'], detail=False, url_path='languages', url_name='{basename}-lang-list')
    def list_languages(self, request):
        token = None
        try:
            token = log_query(request) # Create Query 
            lang_names = [code for code, name in LANGS_ISO_639_1]
            return create_paginate_response(
                token=token,
                queryset=lang_names,
                request=request,
                serializer_class=StrListSerializer,
                many=False,
                max_items_page = len(lang_names)
            )
        except Exception as e:
            logger.error(
                f"{ e.__class__.__name__ }: {e}",
            )
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)