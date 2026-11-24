from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from app_dashboard.forms import DashboardStatisticsForm
from app_dashboard.services.statistics import automatic_grouping, calendar_periods, preset_date_ranges


@pytest.fixture(autouse=True)
def analytics_responses():
    """Exercise dashboard rendering with explicit ES service results, without HTTP."""
    with patch("app_dashboard.services.statistics.get_message_stats", return_value=[]) as stats, patch(
        "app_dashboard.services.statistics.get_message_created_at_minimum", return_value=None,
    ) as minimum:
        yield stats, minimum


@pytest.fixture
def dashboard_client(staff_user):
    client = Client()
    client.force_login(staff_user)
    return client


def test_calendar_intervals_align_with_stats_truncation():
    assert calendar_periods(date(2024, 2, 29), date(2024, 3, 6), "week") == [date(2024, 2, 26), date(2024, 3, 4)]
    assert calendar_periods(date(2024, 2, 29), date(2024, 3, 1), "month") == [date(2024, 2, 1), date(2024, 3, 1)]
    assert calendar_periods(date(2023, 12, 31), date(2024, 1, 1), "year") == [date(2023, 1, 1), date(2024, 1, 1)]
    assert automatic_grouping(date(2020, 1, 1), date(2024, 1, 1)) == "month"


def test_presets_use_calendar_months_and_years_at_leap_boundaries():
    ranges = preset_date_ranges(date(2024, 2, 29))
    assert ranges["1d"]["start"] == date(2024, 2, 29)
    assert ranges["1w"]["start"] == date(2024, 2, 23)
    assert ranges["1m"]["start"] == date(2024, 1, 30)
    assert ranges["3m"]["start"] == date(2023, 11, 30)
    assert ranges["1y"]["start"] == date(2023, 3, 1)
    assert ranges["5y"]["start"] == date(2019, 3, 1)


def test_chart_form_limits_periods_and_reuses_stats_validation():
    values = {"createdat_min": "2020-01-01", "createdat_max": "2024-01-01", "group_by": "day"}
    form = DashboardStatisticsForm(data=values)
    assert not form.is_valid()
    assert "too many points" in str(form.errors["group_by"])
    auto = DashboardStatisticsForm(data={**values, "group_by": "auto"})
    assert auto.is_valid(), auto.errors
    assert auto.statistics_parameters["group_by"] == "month"
    backwards = DashboardStatisticsForm(data={**values, "createdat_max": "2019-01-01"})
    assert not backwards.is_valid()
    assert "createdat_max" in backwards.errors


def test_chart_form_rejects_invalid_grouping_and_oversized_lists():
    values = {"createdat_min": "2024-01-01", "createdat_max": "2024-01-02", "group_by": "hour"}
    assert not DashboardStatisticsForm(data=values).is_valid()
    form = DashboardStatisticsForm(data={**values, "group_by": "day", "room": ",".join(str(i) for i in range(21))})
    assert not form.is_valid()
    assert "room" in form.errors
    long_tag = DashboardStatisticsForm(data={**values, "group_by": "day", "tag": "x" * 65})
    assert not long_tag.is_valid()
    assert "tag" in long_tag.errors
    too_early = DashboardStatisticsForm(data={**values, "group_by": "day", "createdat_min": "1999-12-31"})
    assert not too_early.is_valid()
    assert "createdat_min" in too_early.errors


@pytest.mark.django_db
def test_overview_is_a_staff_page_and_does_not_wait_for_statistics(dashboard_client):
    with patch("app_dashboard.services.statistics.get_message_stats") as stats:
        response = dashboard_client.get(reverse("app_dashboard:home"))
    assert response.status_code == 200
    stats.assert_not_called()
    body = response.content.decode()
    assert "Activity overview" in body
    assert "data-analytics-form" in body
    assert body.count('data-chart="activity"') == 1
    assert "data-zscore-toggle" in body
    assert 'class="analytics-control-panel"' in body
    for code in ("1d", "1w", "1m", "3m", "1y", "5y", "all"):
        assert f'data-range="{code}"' in body
    assert 'aria-current="page">Overview' in body
    assert body.count("app_dashboard/analytics.js") == 1


@pytest.mark.django_db
def test_statistics_requires_a_staff_session():
    from app_users.models import User
    client = Client()
    for name in ("home", "statistics"):
        response = client.get(reverse("app_dashboard:" + name))
        assert response.status_code == 302
        assert "/dashboard/login/" in response["Location"]
    user = User.objects.create_user(email="reader-stats@example.org", username="stats-reader", password="test-password", is_staff=False)
    client.force_login(user)
    assert client.get(reverse("app_dashboard:statistics")).status_code == 403


@pytest.mark.django_db
def test_calendar_zero_counts_preserve_endpoint_z_scores_and_inclusive_dates(dashboard_client, analytics_responses):
    stats, _ = analytics_responses
    stats.return_value = [
        {"period_start": datetime(2024, 1, 1, tzinfo=dt_timezone.utc), "count": 1, "z_score": -1.0},
        {"period_start": datetime(2024, 1, 3, tzinfo=dt_timezone.utc), "count": 3, "z_score": 1.0},
    ]
    response = dashboard_client.get(reverse("app_dashboard:statistics"), {"createdat_min": "2024-01-01", "createdat_max": "2024-01-03", "group_by": "day"})
    assert response.status_code == 200
    data = response.json()
    assert [row["count"] for row in data["series"]] == [1, 0, 3]
    assert [row["z_score"] for row in data["series"]] == [-1.0, None, 1.0]
    assert data["summary"]["total_messages"] == 4
    assert data["summary"]["mean_count"] == 2
    assert data["summary"]["active_periods"] == 2
    assert data["summary"]["peak_date"] == "2024-01-03"
    assert data["summary"]["unusual_periods"] == 0
    assert response["Cache-Control"] == "no-store"




@pytest.mark.django_db
def test_statistics_accepts_repeated_and_comma_separated_filters(dashboard_client):
    with patch("app_dashboard.services.statistics.get_message_stats", return_value=[]) as stats:
        response = dashboard_client.get(reverse("app_dashboard:statistics"), {
            "createdat_min": "2024-01-01", "createdat_max": "2024-01-02", "group_by": "day",
            "room": ["one,two", "three"], "tag": ["a", "b,c"], "tag_match": "all",
            "is_reply": "false", "stored_since": "2024-01-01", "z_score": "false",
        })
    assert response.status_code == 200
    arguments = stats.call_args.kwargs
    assert arguments["room"] == ["one", "two", "three"]
    assert arguments["tags"] == ["a", "b", "c"]
    assert arguments["tag_match"] == "all"
    assert arguments["is_reply"] is False
    assert arguments["z_score"] is True
    assert timezone.is_aware(arguments["stored_since"])


@pytest.mark.django_db
def test_statistics_forwards_room_tags_and_replies(dashboard_client, analytics_responses):
    stats, _ = analytics_responses
    stats.return_value = [{"period_start": datetime(2024, 1, 2, tzinfo=dt_timezone.utc), "count": 1, "z_score": 0.0}]
    response = dashboard_client.get(reverse("app_dashboard:statistics"), {"createdat_min": "2024-01-01", "createdat_max": "2024-01-03", "tag": "research,selected", "tag_match": "all", "is_reply": "false"})
    assert response.json()["summary"]["total_messages"] == 1
    assert stats.call_args.kwargs["tags"] == ["research", "selected"]
    assert stats.call_args.kwargs["tag_match"] == "all"
    assert stats.call_args.kwargs["is_reply"] is False




@pytest.mark.django_db
def test_statistics_uses_server_timezone_for_day_boundaries(dashboard_client, analytics_responses):
    stats, _ = analytics_responses
    stats.return_value = [{"period_start": datetime(2024, 1, 1, 23, tzinfo=dt_timezone.utc), "count": 1, "z_score": 0.0}]
    with timezone.override("Europe/Madrid"):
        response = dashboard_client.get(reverse("app_dashboard:statistics"), {"createdat_min": "2024-01-02", "createdat_max": "2024-01-02"})
    data = response.json()
    assert data["timezone"] == "Europe/Madrid"
    assert data["series"] == [{"period_start": "2024-01-02", "range_start": "2024-01-02", "range_end": "2024-01-02", "count": 1, "z_score": 0.0}]




@pytest.mark.django_db
def test_statistics_clamps_partial_week_labels_to_selection(dashboard_client, message_factory):
    message_factory(created_at=datetime(2024, 2, 29, 12, tzinfo=dt_timezone.utc))
    data = dashboard_client.get(reverse("app_dashboard:statistics"), {"createdat_min": "2024-02-29", "createdat_max": "2024-03-06", "group_by": "week"}).json()
    assert data["series"][0]["range_start"] == "2024-02-29"
    assert data["series"][0]["range_end"] == "2024-03-03"
    assert data["series"][1]["range_end"] == "2024-03-06"


@pytest.mark.django_db
def test_empty_statistics_and_relative_date_defaults(dashboard_client):
    data = dashboard_client.get(reverse("app_dashboard:statistics"), {"range": "7d"}).json()
    assert len(data["series"]) == 7
    assert data["summary"]["total_messages"] == 0
    assert data["summary"]["mean_count"] == 0
    assert all(point["z_score"] is None for point in data["series"])


@pytest.mark.django_db
def test_invalid_statistics_does_not_run_a_query(dashboard_client):
    with patch("app_dashboard.services.statistics.get_message_stats") as stats:
        response = dashboard_client.get(reverse("app_dashboard:statistics"), {"createdat_min": "invalid"})
    assert response.status_code == 400
    assert "createdat_min" in response.json()["errors"]
    stats.assert_not_called()


@pytest.mark.django_db
def test_statistics_failure_returns_a_retryable_error(dashboard_client):
    with patch("app_dashboard.services.statistics.get_message_stats", side_effect=RuntimeError("private database error")):
        response = dashboard_client.get(reverse("app_dashboard:statistics"))
    assert response.status_code == 503
    assert "private" not in response.content.decode()


@pytest.mark.django_db
def test_all_history_starts_at_the_first_matching_message(dashboard_client, analytics_responses):
    stats, minimum = analytics_responses
    minimum.return_value = datetime(2024, 5, 1, 12, tzinfo=dt_timezone.utc)
    stats.return_value = [
        {"period_start": datetime(2024, 5, 1, tzinfo=dt_timezone.utc), "count": 1, "z_score": 0.0},
        {"period_start": datetime(2024, 6, 1, tzinfo=dt_timezone.utc), "count": 1, "z_score": 0.0},
    ]
    response = dashboard_client.get(reverse("app_dashboard:statistics"), {"range": "all", "tag": "research", "createdat_min": "2024-06-01", "createdat_max": "2024-06-05"})
    data = response.json()
    assert response.status_code == 200
    assert data["start"] == "2024-05-01"
    assert data["group_by"] == "day"
    assert data["summary"]["total_messages"] == 2
    assert len(data["series"]) == 36
    assert minimum.call_args.kwargs["tags"] == ["research"]




@pytest.mark.django_db
def test_all_history_rejects_too_many_explicit_daily_periods(dashboard_client, analytics_responses):
    stats, minimum = analytics_responses
    minimum.return_value = datetime(2018, 1, 1, tzinfo=dt_timezone.utc)
    response = dashboard_client.get(reverse("app_dashboard:statistics"), {"range": "all", "group_by": "day", "createdat_max": "2024-01-01"})
    assert response.status_code == 400
    assert "group_by" in response.json()["errors"]
    stats.assert_not_called()




@pytest.mark.django_db
def test_empty_all_history_has_a_single_empty_period(dashboard_client):
    data = dashboard_client.get(reverse("app_dashboard:statistics"), {"range": "all"}).json()
    assert data["start"] == data["end"]
    assert data["summary"]["total_messages"] == 0
    assert len(data["series"]) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("mode,filters", [
    ("retrieval", {"room": ["one", "two"], "tag": "research", "createdat_min": "2024-01-01", "createdat_max": "2024-01-03"}),
    ("elasticsearch", {"room_name__terms": "one__two", "tag__terms": "research", "created_at__gte": "2024-01-01", "created_at__lte": "2024-01-03"}),
])
def test_search_links_to_analytics_with_compatible_filters(dashboard_client, mode, filters):
    from urllib.parse import parse_qs, urlsplit

    paginator = MagicMock()
    paginator.paginate_queryset.return_value = []
    paginator.page.paginator.count = 0
    paginator.get_next_link.return_value = None
    paginator.get_previous_link.return_value = None
    search_view = MagicMock()
    search_view.pagination_class.return_value = paginator
    with patch("app_dashboard.views.build_message_search_queryset", return_value=(search_view, [])):
        response = dashboard_client.get(reverse("app_dashboard:search"), {"mode": mode, **filters})
    assert response.status_code == 200
    assert "data-browse-analytics" in response.content.decode()
    parameters = parse_qs(urlsplit(response.context["analytics_url"]).query)
    assert parameters["room"] == ["one", "two"]
    assert parameters["tag"] == ["research"]
    assert parameters["createdat_min"] == ["2024-01-01"]
    assert parameters["createdat_max"] == ["2024-01-03"]
    assert parameters["range"] == ["custom"]
