"""Calendar series for the dashboard, using the existing message statistics service."""

from datetime import datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone

from app_stats.services.api import (
    get_message_created_at_minimum,
    get_message_stats,
)


MAX_CHART_PERIODS = 1000


class ChartPeriodLimit(ValueError):
    pass


def normalize_preset(value):
    return {"7d": "1w", "30d": "1m", "90d": "3m", "365d": "1y"}.get(value, value)


def preset_date_ranges(today):
    """Calendar months/years, with both displayed boundary dates included."""
    offsets = {
        "1d": timedelta(days=1), "1w": timedelta(days=7),
        "1m": relativedelta(months=1), "3m": relativedelta(months=3),
        "1y": relativedelta(years=1), "5y": relativedelta(years=5),
    }
    ranges = {key: {"start": today - offset + timedelta(days=1), "end": today}
              for key, offset in offsets.items()}
    ranges["all"] = {"start": settings.MIN_DATE, "end": today}
    return ranges


def automatic_grouping(start, end):
    days = (end - start).days + 1
    if days <= 180:
        return "day"
    if days <= 730:
        return "week"
    if days <= 7300:
        return "month"
    return "year"


def _floor_period(value, grouping):
    if grouping == "week":
        return value - timedelta(days=value.weekday())
    if grouping == "month":
        return value.replace(day=1)
    if grouping == "year":
        return value.replace(month=1, day=1)
    return value


def _next_period(value, grouping):
    if grouping == "year":
        return value.replace(year=value.year + 1)
    if grouping == "month":
        return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)
    return value + timedelta(days=7 if grouping == "week" else 1)


def calendar_periods(start, end, grouping):
    """Mirror Django Trunc boundaries, including Monday-based weeks."""
    current = _floor_period(start, grouping)
    last = _floor_period(end, grouping)
    periods = []
    while current <= last:
        periods.append(current)
        if len(periods) > MAX_CHART_PERIODS:
            raise ChartPeriodLimit("Too many chart periods")
        if current == last:
            break
        current = _next_period(current, grouping)
    return periods


def build_statistics_series(parameters, *, all_history=False, auto_interval=False):
    """Keep /stats z-scores intact; missing calendar buckets have no z-score."""
    parameters = dict(parameters)
    if all_history:
        filters = {key: value for key, value in parameters.items()
                   if key in {"createdat_max", "stored_since", "room", "is_reply", "tags", "tag_match"}}
        minimum = timezone.make_aware(datetime.combine(settings.MIN_DATE, time.min))
        first = get_message_created_at_minimum(
            createdat_min=minimum,
            **filters,
        )
        start = timezone.localdate(first) if first else timezone.localdate(parameters["createdat_max"])
        parameters["createdat_min"] = timezone.make_aware(datetime.combine(start, time.min))
        if auto_interval:
            parameters["group_by"] = automatic_grouping(start, timezone.localdate(parameters["createdat_max"]))
    start = timezone.localdate(parameters["createdat_min"])
    end = timezone.localdate(parameters["createdat_max"])
    grouping = parameters["group_by"]
    periods = calendar_periods(start, end, grouping)
    rows = get_message_stats(**parameters)
    observed = {
        timezone.localdate(row["period_start"]): row
        for row in rows
    }
    series = []
    for index, period in enumerate(periods):
        row = observed.get(period)
        period_end = periods[index + 1] - timedelta(days=1) if index + 1 < len(periods) else end
        series.append({
            "period_start": period.isoformat(),
            "range_start": max(period, start).isoformat(),
            "range_end": min(period_end, end).isoformat(),
            "count": int(row["count"]) if row else 0,
            "z_score": float(row["z_score"]) if row else None,
        })
    active = [point for point in series if point["count"]]
    total = sum(point["count"] for point in active)
    peak = max(active, key=lambda point: point["count"], default=None)
    return {
        "series": series,
        "group_by": grouping,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "today": timezone.localdate().isoformat(),
        "timezone": timezone.get_current_timezone_name(),
        "generated_at": timezone.now().isoformat(),
        "presets": preset_date_ranges(timezone.localdate()),
        "summary": {
            "total_messages": total,
            "active_periods": len(active),
            "periods": len(series),
            "mean_count": total / len(active) if active else 0,
            "peak_count": peak["count"] if peak else 0,
            "peak_date": peak["range_start"] if peak else None,
            "unusual_periods": sum(abs(point["z_score"]) >= 2 for point in active),
        },
    }
