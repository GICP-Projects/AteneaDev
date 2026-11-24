from rest_framework import viewsets, status
from rest_framework.decorators import action
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
from rest_framework_api_key.permissions import HasAPIKey
from django.db.models import F, Func
from django.db.models.functions import Lower
from app_telegram.models import RoomItem
from app_frontend.serializers import StrListSerializer
from app_base.languages import LANGS_ISO_639_1
from app_base.views import (
    log_query, 
    create_message, 
    create_paginate_response,
    BasePaginator,
)
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)


def list_room_tags(*, query="", limit=None, exclude_deleted=False):
    """Return distinct, lowercase room tags through the Django ORM."""

    rooms = RoomItem.objects.all()
    if exclude_deleted:
        rooms = rooms.filter(is_deleted=False)
    tags = (
        rooms.annotate(tag=Func(F("tags"), function="unnest"))
        .annotate(tag=Lower("tag"))
        .order_by("tag")
        .distinct("tag")
        .values_list("tag", flat=True)
    )
    needle = query.strip().lower()
    results = [tag for tag in tags if tag and (not needle or needle in tag)]
    return results[:limit] if limit is not None else results


# ======================================================
# =====          VIEWS RELATED TO FRONT            =====
# ======================================================
class FrontFormView(viewsets.ViewSet):
    """ Front views to fill web forms.
    """
    permission_classes = [HasAPIKey]
    pagination_class = BasePaginator

    @extend_schema(
        summary="Retrieve the public names of stored rooms.",
        description="This endpoint provides room names for form selectors.",
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=StrListSerializer(),
                description="A list of room names.",
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR: OpenApiResponse(description="Unexpected error occurred."),
        },
    )
    @action(methods=["get"], detail=False, url_path="rooms", url_name="{basename}-room-list")
    def list_rooms(self, request):
        token = None
        try:
            token = log_query(request)
            room_names = (
                RoomItem.objects.filter(is_deleted=False)
                .exclude(unique_name__isnull=True)
                .exclude(unique_name="")
                .order_by(Lower("unique_name"))
                .values_list("unique_name", flat=True)
            )
            return create_paginate_response(
                token=token,
                queryset=room_names,
                request=request,
                serializer_class=StrListSerializer,
                many=False,
                max_items_page=room_names.count(),
            )
        except Exception as e:
            logger.error(f"{e.__class__.__name__}: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            room_tags = list_room_tags()
            return create_paginate_response(
                token=token,
                queryset=room_tags,
                request=request,
                serializer_class=StrListSerializer,
                many=False,
                max_items_page = len(room_tags)
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
