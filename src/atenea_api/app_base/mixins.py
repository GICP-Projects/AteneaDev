"""Reusable collection actions for DRF generic views."""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from app_base.serializers import CountResponseSerializer, GenericResponseSerializer
from app_base.views import create_message


class CountMixin:
    """Expose the total of the same filtered queryset used by a list."""

    @extend_schema(
        responses={
            status.HTTP_200_OK: CountResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
        },
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def count(self, request, *args, **kwargs):
        try:
            total = self.filter_queryset(self.get_queryset()).count()
        except ValidationError as error:
            return create_message(
                getattr(error, "token", None) or getattr(self, "query_token", None),
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=error.detail,
            )
        token = getattr(self, "query_token", None)
        return Response({"count": total, "token": str(token) if token is not None else None})
