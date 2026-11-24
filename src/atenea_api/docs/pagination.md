# Cursor pagination for model and computed fields

`app_base.views.CursorPaginator` reads a composite position from each result
and uses it to select the next or previous page. Each ordering field needs a
Django field descriptor to convert cursor strings back to their Python types.
The final ordering field must be unique in the result set and non-nullable.

The examples below belong inside a DRF view and use its `request`.
For the HTTP response and cursor contract, see [REST API usage](api_usage.md#pagination).

## Model fields

Use `CursorPaginator` directly to paginate model fields:

```python
from app_base.views import CursorPaginator
from app_telegram.models import MessageItem

queryset = MessageItem.objects.all()
paginator = CursorPaginator(
    page_size=100,
    is_authenticated=True,
    ordering=("created_at", "id"),
)
page = paginator.paginate_queryset(queryset, request)
```

The default `_get_ordering_fields(queryset)` obtains `created_at` and `id`
from `MessageItem._meta`. Their model definitions supply the types,
nullability and uniqueness used by the cursor. `id` breaks ties between
messages with the same date.

`paginate_queryset()` calls `_get_ordering_fields()` to resolve the ordering
fields. Subclasses can override this method to describe computed query fields.

## Computed fields

A query annotation such as `text_length` exists on the query results but has
no definition in `MessageItem._meta`. Declare its type in a subclass:

```python
from django.db import models
from django.db.models.functions import Length

from app_base.views import CursorPaginator
from app_telegram.models import MessageItem


class MessageLengthPaginator(CursorPaginator):
    ordering = ("text_length", "id")

    def _get_ordering_fields(self, queryset):
        length = models.IntegerField(null=True)
        length.set_attributes_from_name("text_length")
        return [length, queryset.model._meta.get_field("id")]


queryset = MessageItem.objects.annotate(text_length=Length("text"))
paginator = MessageLengthPaginator(page_size=60, is_authenticated=True)
page = paginator.paginate_queryset(queryset, request)
```

This example uses a fixed ordering. `text` can be null, so the descriptor for
its length also allows null. `set_attributes_from_name()` sets the name used
to read the value from a result and filter the queryset. The model's real `id`
is the unique tie breaker.

The descriptor is a Python object used to interpret cursor values; the
annotation computes the value. Neither adds a database column or requires a
migration. Computing and sorting an expression can still be expensive.

For subclasses that accept several orderings, return the descriptors in
`self.ordering` order, as `EntityRankingPaginator` does.

## ClickHouse entity ranking

The [ranking service](../app_telegram/services/analytics.py) runs native ClickHouse SQL
against signed ClickHouse `entity_facts` rows. It filters current message metadata,
then groups by `entity_id` and computes `message_count` and
`appearance_count`.

Use `EntityRankingPaginator` to preserve the signed cursor contract:

```python
from app_telegram.pagination import EntityRankingPaginator
from app_telegram.services.analytics import entity_ranking_ordering

ordering = entity_ranking_ordering("messages")
paginator = EntityRankingPaginator(
    page_size=60,
    is_authenticated=True,
    ordering=ordering,
)
page = paginator.paginate_ranking(
    {"room": ["Alviseperez"]},
    request,
    entity_type=["PER", "ORG"],
    ordering=ordering,
)
```

The paginator describes the two counts as `BigIntegerField` and the entity ID
as a unique `UUIDField` so it can validate cursor positions. `order=messages`
sorts by message count and then appearances; `order=appearances` swaps those
priorities. The entity UUID breaks ties.

Each page aggregates its matching ClickHouse fact scope. Counts can change
between requests and move entities between pages.
