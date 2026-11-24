"""HTML views for the operator dashboard."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.contrib.auth.views import redirect_to_login
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError as DRFValidationError
from rest_framework.request import Request as DRFRequest

from app_base.views import CursorPaginator
from app_entity.models import EntityItem
from app_telegram.serializers import EntityRankingFilterSerializer
from app_telegram.services.analytics import (
    entity_ranking, entity_ranking_ordering, entity_ranking_results,
)
from app_dashboard.constants import JOB_ACTION_LABELS, job_status_meta, pipeline_label
from app_dashboard.forms import DashboardStatisticsForm, JobFilterForm, JobLaunchForm, ScheduleForm
from app_dashboard.services.statistics import (
    ChartPeriodLimit, build_statistics_series, normalize_preset, preset_date_ranges,
)
from app_dashboard.services.audit import record_query
from app_dashboard.services.graph import (
    build_batch_cards,
    build_batch_detail,
    build_job_graph,
    build_job_live_summary,
    build_job_pipeline,
    build_job_pipeline_updates,
)
from app_dashboard.services.launchers import launch_pipeline
from app_dashboard.services.presentation import present_job
from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.enums import JobMode, JobStatus
from app_scheduler.serializers import ProcessingTriggerSerializer
from app_scheduler.services import api as scheduler_api
from app_scheduler.services.jobs import (
    cancel_job,
    discard_failed_job,
    InvalidJobAction,
    pause_job,
    processing_capacity,
    resume_job,
    retry_failed_job,
    retry_failed_job_tree,
    retry_failed_jobs,
    retryable_jobs,
)
from app_scheduler.registry import get_scheduled_pipelines
from app_base.languages import LANGS_ISO_639_1
from app_telegram.models import MessageItem, RoomItem
from app_telegram.serializers import MessageDocumentSerializer, MessageRetrievalFilterSerializer
from app_telegram.services import api as telegram_api
from app_telegram.views.search_views import build_message_search_queryset
from app_frontend.views import list_room_tags


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    login_url = reverse_lazy("app_dashboard:login")
    raise_exception = True

    def test_func(self):
        return bool(self.request.user.is_staff)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                REDIRECT_FIELD_NAME,
            )
        raise PermissionDenied


def _statistics_form_values(request):
    today = timezone.localdate()
    selected_range = normalize_preset(request.GET.get("range", "custom" if "createdat_min" in request.GET or "createdat_max" in request.GET else "1m"))
    bounds = preset_date_ranges(today).get(selected_range, preset_date_ranges(today)["1m"])
    values = {
        "range": selected_range, "group_by": "auto", "tag_match": "any",
        "createdat_min": bounds["start"].isoformat(),
        "createdat_max": bounds["end"].isoformat(),
    }
    for name in DashboardStatisticsForm().fields:
        if name not in request.GET:
            continue
        if name in {"room", "tag"}:
            values[name] = ",".join(request.GET.getlist(name))
        elif name in {"createdat_min", "createdat_max", "stored_since"}:
            values[name] = _dashboard_search_date_value(request.GET[name])
        else:
            values[name] = request.GET[name]
    values["range"] = selected_range
    return values


class DashboardOverviewView(StaffRequiredMixin, TemplateView):
    """Render controls immediately; statistics are fetched independently."""

    template_name = "app_dashboard/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "form": DashboardStatisticsForm(data=_statistics_form_values(self.request)),
            "today": timezone.localdate().isoformat(),
            "statistics_timezone": timezone.get_current_timezone_name(),
            "analytics_presets": preset_date_ranges(timezone.localdate()),
            "entity_types": EntityItem.SPACY_TYPES + EntityItem.CUSTOM_TYPES,
            "entity_cloud_labels": {
                "loading": _("Loading entities…"), "updating": _("Updating entities…"),
                "waiting": _("Waiting for the selected range…"),
                "error": _("Entities could not be loaded. Try again."),
                "session": _("Your session has expired. Sign in to continue."),
                "empty": _("No entities in this selection."),
                "emptyHint": _("Try a wider range or different entity types."),
                "messages": _("Messages"), "appearances": _("Appearances"),
                "allTypes": _("All types"), "types": _("types"),
                "showing": _("Showing"), "of": _("of"),
                "more": _("More entities are available through the API."),
                "stale": _("Showing the previous selection while new data loads."),
                "failedStale": _("Showing the last successful selection."),
            },
            "preset_buttons": (("1d", "1D", _("Last day")), ("1w", "1W", _("Last week")),
                               ("1m", "1M", _("Last month")), ("3m", "3M", _("Last 3 months")),
                               ("1y", "1Y", _("Last year")), ("5y", "5Y", _("Last 5 years")),
                               ("all", _("All"), _("All history"))),
            "analytics_labels": {
                "loading": _("Loading statistics…"), "updating": _("Updating charts…"),
                "error": _("Statistics could not be loaded. Try again."),
                "session": _("Your session has expired. Sign in to continue."),
                "invalid": _("Check the highlighted filters."), "empty": _("No messages in this range"),
                "emptyHint": _("Try a wider date range or adjust your filters."),
                "frequency": _("Message frequency"), "zscore": _("Z-score"),
                "combined": _("Frequency and z-score"),
                "messages": _("Messages"), "noScore": _("No score"),
                "day": _("Daily"), "week": _("Weekly"), "month": _("Monthly"), "year": _("Yearly"),
                "updated": _("Updated"), "unusual": _("Unusual activity"),
                "zoom": _("Drag to select a range. Use arrow keys to inspect periods."),
                "tableEmpty": _("No data to display."),
                "showing": _("Showing"), "of": _("of"),
                "stale": _("Showing the previous selection while new data loads."),
                "failedStale": _("Showing the last successful selection. Current filters were not applied."),
                "csvPeriod": _("Period start"), "csvEnd": _("Period end"),
            },
        })
        return context


class DashboardStatisticsDataView(StaffRequiredMixin, View):
    """Staff session access to the same filters and service as /api/v1/stats/msg."""

    def get(self, request):
        form = DashboardStatisticsForm(data=_statistics_form_values(request))
        if not form.is_valid():
            response = JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        else:
            try:
                response = JsonResponse(build_statistics_series(
                    form.statistics_parameters,
                    all_history=form.cleaned_data.get("range") == "all",
                    auto_interval=form.auto_interval,
                ))
            except ChartPeriodLimit:
                response = JsonResponse({"errors": {"group_by": [{"message": _("This range has too many points. Choose Auto or a wider interval.")}]}}, status=400)
            except Exception:
                logging.getLogger(__name__).exception("Dashboard statistics query failed")
                response = JsonResponse({"message": _("Statistics could not be loaded. Try again.")}, status=503)
        response["Cache-Control"] = "no-store"
        return response


class DashboardEntityRankingDataView(StaffRequiredMixin, View):
    """Bounded cloud data using the same ranking service as the staff API."""

    def get(self, request):
        form = DashboardStatisticsForm(data=_statistics_form_values(request))
        entity_filters = EntityRankingFilterSerializer(data={
            "entity_type": request.GET.getlist("entity_type"),
            "order": request.GET.get("order", "messages"),
        })
        if not form.is_valid():
            response = JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        elif not entity_filters.is_valid():
            response = JsonResponse({"errors": entity_filters.errors}, status=400)
        else:
            try:
                filters = {key: value for key, value in form.statistics_parameters.items()
                           if key not in {"group_by", "z_score"}}
                order = entity_filters.validated_data["order"]
                rows = entity_ranking(
                    filters, entity_type=entity_filters.validated_data.get("entity_type"),
                    ordering=entity_ranking_ordering(order), limit=61,
                )
                response = JsonResponse({
                    "results": entity_ranking_results(
                        rows[:60], filters, messages_url=reverse("app_dashboard:search"),
                        extra_params={"mode": "retrieval", "search": "1"},
                    ),
                    "limit": 60, "has_more": len(rows) > 60, "order": order,
                    "generated_at": timezone.now().isoformat(),
                })
            except Exception:
                logging.getLogger(__name__).exception("Dashboard entity ranking query failed")
                response = JsonResponse({"message": _("Entities could not be loaded. Try again.")}, status=503)
        response["Cache-Control"] = "no-store"
        return response


class DashboardLookupView(StaffRequiredMixin, View):
    """Return small, searchable catalogues for dashboard comboboxes.

    The public frontend endpoints intentionally require an API key.  A staff
    dashboard must not receive that key in browser JavaScript, so these views
    provide the same read-only catalogues through the dashboard session.
    """

    MAX_RESULTS = 50
    KINDS = {"rooms", "tags", "languages"}

    def get(self, request, kind):
        if kind not in self.KINDS:
            raise Http404
        query = (request.GET.get("q") or "").strip()
        if kind == "rooms":
            results = self._rooms(query)
        elif kind == "tags":
            results = self._tags(query)
        else:
            results = self._languages(query)
        return JsonResponse({"results": results})

    def _rooms(self, query):
        rooms = RoomItem.objects.filter(is_deleted=False).exclude(
            unique_name__isnull=True,
        ).exclude(unique_name="")
        if query:
            rooms = rooms.filter(Q(unique_name__icontains=query) | Q(title__icontains=query))
        rooms = rooms.order_by("unique_name").values("unique_name", "title")[: self.MAX_RESULTS]
        return [
            {
                "value": room["unique_name"],
                "label": room["title"] or room["unique_name"],
                "description": "@%s" % room["unique_name"],
            }
            for room in rooms
        ]

    def _tags(self, query):
        tags = list_room_tags(
            query=query,
            limit=self.MAX_RESULTS,
            exclude_deleted=True,
        )
        return [{"value": tag, "label": tag} for tag in tags]

    def _languages(self, query):
        query = query.lower()
        return [
            {
                "value": code,
                "label": "%s — %s" % (code, name),
                "description": name,
            }
            for code, name in LANGS_ISO_639_1
            if not query or query in code.lower() or query in str(name).lower()
        ][: self.MAX_RESULTS]


def _dashboard_message_search_values(request):
    """Read retrieval filters from GET, accepting comma-separated or repeated values."""
    data = {}
    filter_names = {
        "room", "tag", "tag_match", "is_reply", "stored_since", "createdat_min",
        "createdat_max", "entity",
    }
    date_names = {"stored_since", "createdat_min", "createdat_max"}
    for name, values in request.GET.lists():
        if name not in filter_names:
            continue
        if name in {"room", "tag", "entity"}:
            items = [
                item.strip()
                for value in values
                for item in value.split(",")
                if item.strip()
            ]
            if items:
                data[name] = items
        elif values:
            value = values[-1]
            if value != "":
                if name in date_names:
                    try:
                        value = date.fromisoformat(value).strftime(settings.FORMAT_DATE)
                    except ValueError:
                        pass
                data[name] = value
    return data


def _dashboard_search_date_value(value):
    """Return a date input value from either HTML or serializer date format."""
    if not value:
        return ""
    for date_format in ("%Y-%m-%d", settings.FORMAT_DATE):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return value


def _dashboard_analytics_url(request, mode):
    """Carry the loaded search's compatible scope filters into analytics."""
    if mode == "retrieval":
        values = _dashboard_message_search_values(request)
        for name in ("createdat_min", "createdat_max", "stored_since"):
            if name in values:
                values[name] = _dashboard_search_date_value(values[name])
    else:
        values = {}
        for source, target in (("room_name__terms", "room"), ("tag__terms", "tag")):
            items = [item for value in request.GET.getlist(source) for item in value.split("__") if item]
            if items:
                values[target] = items
        for source, target in (("created_at__gte", "createdat_min"), ("created_at__lte", "createdat_max"), ("is_reply", "is_reply")):
            if request.GET.get(source):
                values[target] = request.GET[source]
    if values or request.GET.get("search"):
        values["range"] = "custom" if "createdat_min" in values or "createdat_max" in values else "all"
    return reverse("app_dashboard:home") + ("?" + urlencode(values, doseq=True) if values else "")


def _dashboard_search_datetime(value):
    """Return an Elasticsearch date as a Python date/datetime for the template."""
    if isinstance(value, (date, datetime)):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dashboard_search_query_display(request):
    """Rebuild repeated exact-phrase parameters as editable Google-style syntax."""
    phrases = [value.strip() for value in request.GET.getlist("qem") if value.strip()]
    if not phrases:
        return request.GET.get("q", "")

    operator = request.GET.get("qem_operator", "or").strip().lower()
    if operator not in {"and", "or"}:
        operator = "or"
    rendered_phrases = []
    for phrase in phrases:
        if len(phrase) >= 2 and phrase.startswith('"') and phrase.endswith('"'):
            phrase = phrase[1:-1]
        phrase = phrase.replace("\\", "\\\\").replace('"', '\\"')
        rendered_phrases.append('"%s"' % phrase)
    return (" %s " % operator.upper()).join(rendered_phrases)


def _flatten_search_errors(errors):
    if isinstance(errors, dict):
        return " ".join(
            "%s: %s" % (name, _flatten_search_errors(value))
            for name, value in errors.items()
        )
    if isinstance(errors, (list, tuple)):
        return " ".join(_flatten_search_errors(value) for value in errors)
    return str(errors)


def _message_snippet(text, query, limit=260):
    text = " ".join((text or "").split())
    if not text:
        return _("This message has no text content.")

    start = 0
    if query:
        match_at = text.casefold().find(query.casefold())
        if match_at >= 0:
            start = max(0, match_at - 80)
    end = min(len(text), start + limit)
    snippet = text[start:end]
    if start:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet


class DashboardMessageSearchView(StaffRequiredMixin, TemplateView):
    """Search stored messages through retrieval or the configured ES search view."""

    template_name = "app_dashboard/search.html"
    page_size = 20

    def get_template_names(self):
        if self.request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return ["app_dashboard/search/_content.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        # Keep the shared dashboard header's auth checks and active navigation
        # state explicit for this search view and its template fragments.
        context.update({"request": request, "user": request.user})
        context["search_entity_filters"] = request.GET.getlist("entity")
        entity_clear_params = request.GET.copy()
        entity_clear_params.pop("entity", None)
        entity_clear_params.pop("cursor", None)
        context["search_entity_clear_url"] = reverse("app_dashboard:search") + "?" + entity_clear_params.urlencode()
        mode = request.GET.get("mode", "elasticsearch")
        if mode not in {"retrieval", "elasticsearch"}:
            mode = "elasticsearch"
        context["analytics_url"] = _dashboard_analytics_url(request, mode)
        requested_ordering = request.GET.get("ordering", "")
        ordering_is_ascending = (
            requested_ordering.split(",", 1)[0].strip() == "created_at"
        )
        search_ordering = (
            "created_at,msg_id" if ordering_is_ascending
            else "-created_at,-msg_id"
        )
        media_type_values = []
        valid_media_types = {value for value, _label in MessageItem.TYPE_CHOICES}
        for raw_value in request.GET.getlist("media_type__terms"):
            for value in raw_value.split("__"):
                if value in valid_media_types and value not in media_type_values:
                    media_type_values.append(value)
        translated_media_labels = {
            "WEBPAGE": _("Web page"),
            "DOCUMENT": _("Document media"),
            "PHOTO": _("Photo media"),
            "VIDEO": _("Video media"),
            "AUDIO": _("Audio media"),
            "GIF": _("GIF media"),
            "CONTACT": _("Contact media"),
            "GEO": _("Geolocation"),
            "ONLY_TEXT": _("Only text"),
            "OTHER": _("Other"),
        }
        media_type_labels = {
            value: translated_media_labels.get(value, label)
            for value, label in MessageItem.TYPE_CHOICES
        }
        form_values = {
            "mode": mode,
            "ordering": search_ordering,
            "ordering_is_ascending": ordering_is_ascending,
            "q": (
                _dashboard_search_query_display(request)
                if mode == "elasticsearch"
                else ""
            ),
            "room": ", ".join(request.GET.getlist("room")),
            "tag": ", ".join(request.GET.getlist("tag")),
            "tag_match": request.GET.get("tag_match", "any"),
            "is_reply": request.GET.get("is_reply", "") if mode == "retrieval" else "",
            "stored_since": _dashboard_search_date_value(request.GET.get("stored_since", "")),
            "createdat_min": _dashboard_search_date_value(request.GET.get("createdat_min", "")),
            "createdat_max": _dashboard_search_date_value(request.GET.get("createdat_max", "")),
            "es_room_names": request.GET.get("room_name__terms", ""),
            "es_tags": request.GET.get("tag__terms", ""),
            "es_entities": request.GET.get("entity__terms", ""),
            "es_lang": request.GET.get("lang", ""),
            "es_media_types": "__".join(media_type_values),
            "es_media_types_selected": media_type_values,
            "es_media_types_label": ", ".join(
                media_type_labels[value] for value in media_type_values
            ) or _("All media types"),
            "es_created_from": request.GET.get("created_at__gte", ""),
            "es_created_until": request.GET.get("created_at__lte", ""),
            "es_is_reply": request.GET.get("is_reply", "") if mode == "elasticsearch" else "",
        }
        submitted = request.GET.get("search") == "1" or any(
            value
            for name, value in form_values.items()
            if name not in {"tag_match", "mode", "es_media_types_label"}
        )
        context.update(
            {
                "form_values": form_values,
                "media_type_choices": [
                    (value, media_type_labels[value])
                    for value, _label in MessageItem.TYPE_CHOICES
                ],
                "search_submitted": submitted,
                "search_results": [],
                "search_error": "",
                "search_loaded_count": 0,
                "search_total_count": None,
                "next_url": None,
                "previous_url": None,
                "show_results": False,
            }
        )
        if not submitted:
            return context

        if mode == "retrieval":
            filter_data = _dashboard_message_search_values(request)
            filter_serializer = MessageRetrievalFilterSerializer(data=filter_data)
            if not filter_serializer.is_valid():
                context["search_error"] = _flatten_search_errors(filter_serializer.errors)
                return context

            filters = dict(filter_serializer.validated_data)
            if "tag" in filters:
                filters["tags"] = filters.pop("tag")
            queryset = telegram_api.get_messages(**filters)
            cursor_ordering = (
                ("created_at", "id") if ordering_is_ascending
                else ("-created_at", "-id")
            )
            paginator = CursorPaginator(
                page_size=self.page_size,
                ordering=cursor_ordering,
            )
            try:
                page = paginator.paginate_queryset(queryset, DRFRequest(request))
            except DRFValidationError as error:
                context["search_error"] = _flatten_search_errors(error.detail)
                return context

            context["search_results"] = [
                {
                    "link": message.link,
                    "room_name": message.room.unique_name,
                    "msg_id": message.msg_id,
                    "snippet": _message_snippet(message.text, ""),
                    "created_at": message.created_at,
                    "media_type": message.media_type,
                    "lang": message.lang,
                    "views": message.views,
                    "tags": message.room.tags or [],
                    "entities": [],
                }
                for message in page
            ]
            context["search_loaded_count"] = len(context["search_results"])
            context["next_url"] = paginator.get_next_link()
            context["previous_url"] = paginator.get_previous_link()
        else:
            query = request.GET.get("q", "")
            if not query:
                qem_values = request.GET.getlist("qem")
                if qem_values:
                    query = qem_values[0].strip().strip('"')
            try:
                query = serializers.CharField(
                    required=False,
                    allow_blank=True,
                    max_length=512,
                    trim_whitespace=False,
                ).run_validation(query)
            except serializers.ValidationError as error:
                context["search_error"] = _flatten_search_errors(error.detail)
                return context

            try:
                search_request = DRFRequest(request)
                search_view, queryset = build_message_search_queryset(search_request)
                paginator = search_view.pagination_class()
                paginator.page_size = self.page_size
                page = paginator.paginate_queryset(queryset, search_request, search_view)
            except (DRFValidationError, NotFound) as error:
                detail = getattr(error, "detail", str(error))
                context["search_error"] = _flatten_search_errors(detail)
                return context

            serialized = MessageDocumentSerializer(
                page,
                many=True,
                context={"request": search_request},
            ).data
            context["search_results"] = [
                {
                    "link": data.get("link"),
                    "room_name": getattr(hit, "room_name", ""),
                    "msg_id": getattr(hit, "msg_id", ""),
                    "snippet": _message_snippet(data.get("text"), query),
                    "created_at": _dashboard_search_datetime(
                        data.get("created_at") or getattr(hit, "created_at", None)
                    ),
                    "media_type": getattr(hit, "media_type", ""),
                    "lang": getattr(hit, "lang", ""),
                    "views": getattr(hit, "views", None),
                    "tags": data.get("tags", []),
                    "entities": data.get("entities", []),
                }
                for hit, data in zip(page, serialized)
            ]
            context["search_loaded_count"] = len(context["search_results"])
            context["search_total_count"] = paginator.page.paginator.count
            context["next_url"] = paginator.get_next_link()
            context["previous_url"] = paginator.get_previous_link()

        context["show_results"] = True
        return context


class DashboardMessageSearchCountView(StaffRequiredMixin, View):
    """Count retrieval matches separately so the dashboard page stays responsive."""

    def get(self, request):
        filter_serializer = MessageRetrievalFilterSerializer(
            data=_dashboard_message_search_values(request)
        )
        if not filter_serializer.is_valid():
            return JsonResponse(
                {"errors": _flatten_search_errors(filter_serializer.errors)},
                status=400,
            )

        filters = dict(filter_serializer.validated_data)
        if "tag" in filters:
            filters["tags"] = filters.pop("tag")
        return JsonResponse({"count": telegram_api.get_messages(**filters).count()})


class DashboardLoginView(LoginView):
    template_name = "app_dashboard/login.html"
    redirect_authenticated_user = False

    def get_success_url(self):
        redirect_to = self.get_redirect_url()
        if redirect_to and url_has_allowed_host_and_scheme(
            redirect_to,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return redirect_to
        return reverse("app_dashboard:jobs")


class DashboardLogoutView(LogoutView):
    next_page = reverse_lazy("app_dashboard:login")


def _job_filter_choices():
    status_choices = [
        (value, job_status_meta(value)["label"])
        for value, choice_label in JobStatus.choices
    ]
    mode_choices = [
        (value, _("Live") if value == JobMode.LIVE else _("Historical"))
        for value, choice_label in JobMode.choices
    ]
    return status_choices, mode_choices


def _job_filters(request, filter_form):
    queryset = ProcessingJob.objects.filter(parent_job__isnull=True).order_by("-created_at")
    if not filter_form.is_valid():
        return queryset

    cleaned = filter_form.cleaned_data
    if cleaned.get("status"):
        queryset = queryset.filter(
            Q(status=cleaned["status"]) | Q(child_jobs__status=cleaned["status"])
        ).distinct()
    if cleaned.get("mode"):
        queryset = queryset.filter(mode=cleaned["mode"])
    if cleaned.get("started_from"):
        queryset = queryset.filter(started_at__date__gte=cleaned["started_from"])
    if cleaned.get("started_until"):
        queryset = queryset.filter(started_at__date__lte=cleaned["started_until"])
    if cleaned.get("finished_from"):
        queryset = queryset.filter(completed_at__date__gte=cleaned["finished_from"])
    if cleaned.get("finished_until"):
        queryset = queryset.filter(completed_at__date__lte=cleaned["finished_until"])
    return queryset


def _schedule_summary(trigger):
    data = ProcessingTriggerSerializer(instance=trigger).data
    if data.get("interval"):
        interval = data["interval"]
        period_labels = {
            "microseconds": _("microseconds"),
            "milliseconds": _("milliseconds"),
            "seconds": _("seconds"),
            "minutes": _("minutes"),
            "hours": _("hours"),
            "days": _("days"),
        }
        schedule_kind = _("Interval")
        schedule_detail = _("Every %(every)s %(period)s") % {
            "every": interval["every"],
            "period": period_labels.get(interval["period"], interval["period"]),
        }
    elif data.get("crontab"):
        schedule_kind = _("Calendar")
        schedule_detail = _("According to calendar")
    else:
        schedule_kind = _("Specific date and time")
        schedule_detail = data.get("clocked", {}).get("clocked_time", _("Once"))
    return {
        "object": trigger,
        "id": trigger.pk,
        "name": trigger.name,
        "description": trigger.description,
        "pipeline": pipeline_label(data["pipeline"]),
        "pipeline_key": data["pipeline"],
        "schedule_kind": schedule_kind,
        "schedule_detail": schedule_detail,
        "enabled": trigger.enabled,
        "last_run_at": trigger.last_run_at,
        "total_run_count": trigger.total_run_count,
        "parameters": data.get("parameters", {}),
    }


def _job_detail_context(job):
    graph = build_job_graph(job, include_job_details=False)
    return {
        "job": graph["root"],
        "graph": graph,
        "all_jobs": [present_job(item) for item in graph["jobs"]],
        "action_labels": JOB_ACTION_LABELS,
        "can_pause": any(item.status in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING} for item in graph["jobs"]),
        "can_resume": any(item.status == ProcessingJob.PAUSED for item in graph["jobs"]),
        "can_cancel": any(item.status in ProcessingJob.ACTIVE_STATUSES for item in graph["jobs"]),
        "can_retry": job.can_retry_failed,
        "can_discard": (
            job.failed_batches > 0
            and job.status not in {
                ProcessingJob.CANCELLED,
                ProcessingJob.CANCELLING,
                ProcessingJob.PARTIAL,
            }
            and job.failures_abandoned_at is None
        ),
    }


class JobListView(StaffRequiredMixin, TemplateView):
    template_name = "app_dashboard/jobs/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status_choices, mode_choices = _job_filter_choices()
        filter_form = JobFilterForm(
            data=self.request.GET or None,
            status_choices=status_choices,
            mode_choices=mode_choices,
        )
        jobs = list(_job_filters(self.request, filter_form).prefetch_related("child_jobs"))
        presented_jobs = []
        for job in jobs:
            presented = present_job(job)
            presented["children"] = [present_job(child) for child in job.child_jobs.all()]
            presented_jobs.append(presented)
        context.update(
            {
                "jobs": presented_jobs,
                "capacity": processing_capacity(),
                "retryable_message_jobs": _retryable_message_jobs().count(),
                "job_filter_form": filter_form,
                "filters_active": bool(self.request.GET),
                "date_filters_active": any(
                    self.request.GET.get(name)
                    for name in ("started_from", "started_until", "finished_from", "finished_until")
                ),
                "selected_status": self.request.GET.get("status", ""),
                "selected_mode": self.request.GET.get("mode", ""),
            }
        )
        return context


def _retryable_message_jobs():
    from app_telegram.job_keys import TELEGRAM_PROCESS_MESSAGES_PROCESSOR

    return retryable_jobs().filter(processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR).exclude(status=ProcessingJob.PAUSED)


class RetryMessageProcessingView(StaffRequiredMixin, View):
    """Recover all retryable message pipelines after a shared service outage."""

    def post(self, request):
        job_ids = list(_retryable_message_jobs().values_list("pk", flat=True))
        record_query(request, {"action": "retry-message-processing", "job_ids": [str(pk) for pk in job_ids]})
        result = retry_failed_jobs(job_ids, include_paused=False)
        messages.success(request, _("Queued retries for %(jobs)s Jobs and %(batches)s Batches.") % result)
        return redirect("app_dashboard:jobs")


class JobDetailView(StaffRequiredMixin, TemplateView):
    template_name = "app_dashboard/jobs/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = get_object_or_404(ProcessingJob, pk=kwargs["pk"])
        context.update(_job_detail_context(job))
        return context


class JobLiveFragmentView(StaffRequiredMixin, View):
    def get(self, request, pk):
        job = get_object_or_404(ProcessingJob, pk=pk)
        summary = build_job_live_summary(job)
        return render(request, "app_dashboard/jobs/_live.html", {
            "job": summary["job"],
            "graph": {
                "pending_child_jobs": summary["pending_child_jobs"],
                "retryable_child_jobs": summary["retryable_child_jobs"],
                "workflow_summary": summary["workflow_summary"],
                "active": summary["active"],
            },
        })


class JobGraphFragmentView(StaffRequiredMixin, View):
    def get(self, request, pk):
        job = get_object_or_404(ProcessingJob, pk=pk)
        if request.GET.get("format") == "json":
            try:
                since = parse_datetime(request.GET.get("since", ""))
            except ValueError:
                since = None
            if since and timezone.is_naive(since):
                since = timezone.make_aware(since)
            try:
                job_states = json.loads(request.GET.get("jobs", "null"))
            except ValueError:
                job_states = None
            if not isinstance(job_states, dict):
                job_states = None
            return JsonResponse(build_job_pipeline_updates(
                job, since=since, topology=request.GET.get("topology"),
                revision=request.GET.get("revision"), job_states=job_states,
            ))
        graph = build_job_pipeline(job)
        return render(request, "app_dashboard/jobs/_graph.html", {"graph": graph})


class JobBatchDetailView(StaffRequiredMixin, View):
    def get(self, request, pk):
        job = get_object_or_404(ProcessingJob, pk=pk)
        try:
            if "cards" in request.GET:
                values = request.GET["cards"].split(",")
                if not 1 <= len(values) <= 40:
                    raise ValueError
                batch_ids = list(dict.fromkeys(UUID(value) for value in values))
                return JsonResponse(build_batch_cards(job, batch_ids))
            batch_id = UUID(request.GET.get("batch", ""))
            detail = build_batch_detail(job, batch_id)
        except (ValueError, ProcessingBatch.DoesNotExist):
            raise Http404
        return JsonResponse(detail)


class JobCreateView(StaffRequiredMixin, TemplateView):
    template_name = "app_dashboard/jobs/new.html"

    def _pipeline_key(self):
        choices = list(get_scheduled_pipelines())
        selected = self.request.POST.get("pipeline") or self.request.GET.get("pipeline")
        return selected if selected in choices else (choices[0] if choices else None)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pipeline_key = self._pipeline_key()
        context["form"] = JobLaunchForm(pipeline_key=pipeline_key)
        return context

    def post(self, request, *args, **kwargs):
        pipeline_key = self._pipeline_key()
        form = JobLaunchForm(pipeline_key=pipeline_key, data=request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        parameters = getattr(form, "cleaned_parameters", {})
        token = record_query(
            request,
            {"pipeline": form.cleaned_data["pipeline"], "parameters": parameters},
        )
        try:
            job_id = launch_pipeline(
                form.cleaned_data["pipeline"],
                parameters,
                request_token=token,
            )
        except (ValueError, RuntimeError, serializers.ValidationError) as exc:
            form.add_error(None, str(getattr(exc, "detail", exc)))
            return render(request, self.template_name, {"form": form})

        messages.success(request, _("The Job was created and will start when capacity is available."))
        return redirect("app_dashboard:job-detail", pk=job_id)


class JobActionView(StaffRequiredMixin, View):
    operations = {
        "pause": pause_job,
        "resume": resume_job,
        "cancel": cancel_job,
        "retry-failed": retry_failed_job,
        "retry-pending": retry_failed_job_tree,
        "discard-failed": discard_failed_job,
    }

    def post(self, request, pk, action):
        operation = self.operations.get(action)
        if operation is None:
            raise Http404
        job = get_object_or_404(ProcessingJob, pk=pk)
        record_query(request, {"job_id": str(job.pk), "action": action})
        try:
            operation(job.pk)
        except InvalidJobAction as exc:
            messages.error(request, str(exc))
            return redirect("app_dashboard:job-detail", pk=job.pk)
        messages.success(request, _("Action applied: %(action)s.") % {"action": JOB_ACTION_LABELS[action]})
        return redirect("app_dashboard:job-detail", pk=job.pk)


class ScheduleListView(StaffRequiredMixin, TemplateView):
    template_name = "app_dashboard/schedules/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schedules = scheduler_api.public_trigger_queryset().order_by("name")
        context["schedules"] = [_schedule_summary(trigger) for trigger in schedules]
        return context


class ScheduleFormView(StaffRequiredMixin, View):
    template_name = "app_dashboard/schedules/form.html"
    instance = None

    def get_instance(self, pk=None):
        if pk is None:
            return None
        return get_object_or_404(scheduler_api.public_trigger_queryset(), pk=pk)

    def get_form(self, request, instance=None):
        if instance is not None:
            return ScheduleForm(instance=instance, data=request.POST or None)
        pipeline_choices = list(get_scheduled_pipelines())
        pipeline_key = request.POST.get("pipeline") or request.GET.get("pipeline")
        if pipeline_key not in pipeline_choices:
            pipeline_key = pipeline_choices[0] if pipeline_choices else None
        return ScheduleForm(pipeline_key=pipeline_key, data=request.POST or None)

    def _render(self, request, form, instance=None):
        return render(
            request,
            self.template_name,
            {"form": form, "instance": instance, "editing": instance is not None},
        )

    def get(self, request, pk=None):
        instance = self.get_instance(pk)
        return self._render(request, self.get_form(request, instance), instance)

    def post(self, request, pk=None):
        instance = self.get_instance(pk)
        form = self.get_form(request, instance)
        if not form.is_valid():
            return self._render(request, form, instance)

        payload = form.trigger_payload()
        token = record_query(request, {"schedule": payload, "editing": instance is not None})
        serializer = ProcessingTriggerSerializer(
            instance=instance,
            data=payload,
            partial=instance is not None,
        )
        if not serializer.is_valid():
            for field, errors in serializer.errors.items():
                form.add_error(None, f"{field}: {errors}")
            return self._render(request, form, instance)
        trigger = serializer.save()
        action = _("updated") if instance is not None else _("created")
        messages.success(request, _("The schedule was %(action)s.") % {"action": action})
        return redirect("app_dashboard:schedules")


class ScheduleToggleView(StaffRequiredMixin, View):
    def post(self, request, pk):
        trigger = get_object_or_404(scheduler_api.public_trigger_queryset(), pk=pk)
        record_query(request, {"schedule_id": trigger.pk, "action": "toggle"})
        serializer = ProcessingTriggerSerializer(
            instance=trigger,
            data={"enabled": not trigger.enabled},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        messages.success(request, _("The schedule was updated."))
        return redirect("app_dashboard:schedules")


class ScheduleDeleteView(StaffRequiredMixin, View):
    def post(self, request, pk):
        trigger = get_object_or_404(scheduler_api.public_trigger_queryset(), pk=pk)
        record_query(request, {"schedule_id": trigger.pk, "action": "delete"})
        trigger.delete()
        messages.success(request, _("The schedule was deleted."))
        return redirect("app_dashboard:schedules")
