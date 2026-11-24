from django.db import models
from rest_framework import pagination
from rest_framework.exceptions import ValidationError

from app_base.views import CursorPaginator


class EntityRankingPaginator(CursorPaginator):
    """Exact composite cursor over ClickHouse entity ranking rows."""

    ordering = ("-message_count", "-appearance_count", "entity_id")
    cursor_salt = "app_telegram.EntityRankingPaginator"

    def _get_ordering_fields(self, queryset):
        """Describe aggregate values so signed cursor positions decode correctly."""
        fields = {
            "message_count": models.BigIntegerField(name="message_count"),
            "appearance_count": models.BigIntegerField(name="appearance_count"),
            "entity_id": models.UUIDField(name="entity_id", unique=True),
        }
        for name, field in fields.items():
            field.set_attributes_from_name(name)
        return [fields[order.lstrip("-")] for order in self.ordering]

    def paginate_ranking(
        self, message_filters, request, *, entity_type=None, entity_name=None,
        ordering=None, client=None,
    ):
        """Fetch one ranking page while preserving signed filter/order context."""
        if "page" in request.query_params:
            raise ValidationError({
                "page": ["Use 'cursor' or follow the next/previous links instead of 'page'."],
            })
        from app_telegram.services.analytics import (
            entity_ranking,
            entity_ranking_ordering,
        )

        self.request = request
        self.base_url = request.build_absolute_uri()
        configured_ordering = ordering if ordering is not None else self.ordering
        self.ordering = tuple(configured_ordering or entity_ranking_ordering("messages"))
        self.ordering_fields = self._get_ordering_fields(None)
        if not self.ordering_fields[-1].unique or self.ordering_fields[-1].null:
            raise ValueError("Cursor ordering must end in a unique, non-nullable field")
        self.page_size = self.get_page_size(request)
        self.cursor = self.decode_cursor(request)
        position = self.cursor.position if self.cursor else None
        reverse = self.cursor.reverse if self.cursor else False

        query_kwargs = {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "ordering": self.ordering,
            "limit": self.page_size + 1,
            "client": client,
        }
        candidates = entity_ranking(
            message_filters,
            position=position,
            reverse=reverse,
            **query_kwargs,
        )
        has_more_in_direction = len(candidates) > self.page_size
        page = candidates[:self.page_size]
        if reverse:
            page.reverse()
        self.page = page

        if reverse:
            self.has_previous = has_more_in_direction
            self.previous_position = (
                self._get_position_from_instance(page[0], self.ordering)
                if self.has_previous and page else None
            )
            self.has_next = bool(page) and bool(entity_ranking(
                message_filters,
                position=self._get_position_from_instance(page[-1], self.ordering),
                reverse=False,
                limit=1,
                **{key: value for key, value in query_kwargs.items() if key != "limit"},
            ))
            self.next_position = (
                self._get_position_from_instance(page[-1], self.ordering)
                if self.has_next else None
            )
        else:
            self.has_next = has_more_in_direction
            self.next_position = (
                self._get_position_from_instance(page[-1], self.ordering)
                if self.has_next and page else None
            )
            # A first-page request has no previous cursor. Skip the extra
            # aggregate probe in that common case; later pages check one row.
            self.has_previous = bool(self.cursor) and bool(page) and bool(entity_ranking(
                message_filters,
                position=self._get_position_from_instance(page[0], self.ordering),
                reverse=True,
                limit=1,
                **{key: value for key, value in query_kwargs.items() if key != "limit"},
            ))
            self.previous_position = (
                self._get_position_from_instance(page[0], self.ordering)
                if self.has_previous else None
            )
        self.display_page_controls = self.has_next or self.has_previous
        return self.page

    def get_next_link(self):
        if not self.has_next or not self.next_position:
            return None
        return self.encode_cursor(pagination.Cursor(
            offset=0, reverse=False, position=self.next_position,
        ))

    def get_previous_link(self):
        if not self.has_previous or not self.previous_position:
            return None
        return self.encode_cursor(pagination.Cursor(
            offset=0, reverse=True, position=self.previous_position,
        ))
