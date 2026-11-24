from datetime import date, datetime, time, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.utils import timezone

from app_dashboard.services.statistics import build_statistics_series


def test_all_history_uses_elasticsearch_minimum_without_postgresql_fallback():
    first = datetime(2023, 5, 10, 12, tzinfo=dt_timezone.utc)
    parameters = {
        "createdat_min": timezone.make_aware(datetime.combine(settings.MIN_DATE, time.min)),
        "createdat_max": timezone.make_aware(datetime.combine(date(2024, 1, 2), time.max)),
        "group_by": "day",
        "room": ["group"],
        "tags": ["research"],
        "tag_match": "all",
        "is_reply": False,
        "z_score": True,
    }

    with timezone.override("UTC"), \
            patch("app_dashboard.services.statistics.get_message_created_at_minimum", return_value=first) as minimum, \
            patch("app_dashboard.services.statistics.get_message_stats", return_value=[]) as stats:
        result = build_statistics_series(parameters, all_history=True, auto_interval=True)

    minimum.assert_called_once_with(
        createdat_min=timezone.make_aware(datetime.combine(settings.MIN_DATE, time.min)),
        createdat_max=timezone.make_aware(datetime.combine(date(2024, 1, 2), time.max)),
        room=["group"],
        tags=["research"],
        tag_match="all",
        is_reply=False,
    )
    stats_arguments = stats.call_args.kwargs
    assert stats_arguments["createdat_min"] == datetime(2023, 5, 10, tzinfo=dt_timezone.utc)
    assert stats_arguments["group_by"] == "week"
    assert result["start"] == "2023-05-10"
    assert result["end"] == "2024-01-02"
    assert result["summary"]["total_messages"] == 0
    assert all(point["count"] == 0 and point["z_score"] is None for point in result["series"])
