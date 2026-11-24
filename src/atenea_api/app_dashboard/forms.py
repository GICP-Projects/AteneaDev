"""Django forms that translate the existing DRF contracts into operator UI."""

from __future__ import annotations

import json
from zoneinfo import available_timezones

from django import forms
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_celery_beat.models import IntervalSchedule
from rest_framework import serializers

from app_scheduler.registry import (
    get_scheduled_pipeline,
    get_scheduled_pipeline_for_task,
    get_scheduled_pipelines,
    normalize_pipeline_parameters,
    present_pipeline_parameters,
)
from app_scheduler.schedule_policy import (
    LIFECYCLE_FIELDS,
    extract_scheduler_context,
    max_retry_grace_seconds_for_interval,
    public_lifecycle_options,
)
from app_dashboard.constants import (
    ADVANCED_PARAMETER_FIELDS,
    HIDDEN_PARAMETER_FIELDS,
    PIPELINE_FIELD_HELP_TEXTS,
    PIPELINE_FIELD_LABELS,
    pipeline_label,
)
from app_dashboard.services.statistics import automatic_grouping, calendar_periods
from app_stats.serializers import StatsMsgSerializer


PARAMETER_GROUPS = (
    (_("Selection"), ("room", "tags", "tag_match", "by_resource", "by_title")),
    (_("Message filters"), ("type", "types", "lang", "langs", "is_reply", "is_channel")),
    (_("Limits"), ("max_msgs",)),
    (_("Room information"), ("check_room",)),
)

# These fields are free-form lists in the processing contracts, but operators
# should not have to remember room names, tags or ISO language codes.  The
# dashboard renders them as tokenised comboboxes backed by staff-only lookup
# views instead of copying a potentially large catalogue into every form.
AUTOCOMPLETE_FIELD_KINDS = {
    "room": "rooms",
    "tags": "tags",
    "lang": "languages",
    "langs": "languages",
}


def _choice_pairs(choices):
    if hasattr(choices, "items"):
        return list(choices.items())
    return [(choice, choice) for choice in choices]


def _initial_value(field):
    if field.default is not serializers.empty:
        return field.default
    return None


def _label(name):
    return PIPELINE_FIELD_LABELS.get(
        name,
        name.replace("_", " ").capitalize(),
    )


def _coerce_nullable_bool(value):
    if value in ("", None):
        return None
    return value == "true"


class SplitListField(forms.CharField):
    """Accept comma/newline separated values while returning a list."""

    def prepare_value(self, value):
        """Keep the browser value compatible with the list editor widget."""

        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return super().prepare_value(value)

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        values = []
        for item in value.replace(",", "\n").splitlines():
            item = item.strip()
            if item and item not in values:
                values.append(item)
        return values


class DashboardStatisticsForm(forms.Form):
    """Use the stats endpoint's validation with HTML dates and an auto interval."""

    range = forms.ChoiceField(
        required=False,
        choices=(("1d", _("Last day")), ("1w", _("Last week")),
                 ("1m", _("Last month")), ("3m", _("Last 3 months")),
                 ("1y", _("Last year")), ("5y", _("Last 5 years")), ("all", _("All history")),
                 ("custom", _("Custom range"))),
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = {
            "room": _("Rooms"), "tag": _("Tags"), "tag_match": _("Tag matching"),
            "is_reply": _("Replies"), "stored_since": _("Stored since"),
            "createdat_min": _("From"), "createdat_max": _("To"),
            "group_by": _("Interval"),
        }
        for name, serializer_field in StatsMsgSerializer().fields.items():
            if name == "z_score":
                continue
            field = _django_field(name, serializer_field)
            field.label = labels[name]
            field.help_text = ""
            if name in {"createdat_min", "createdat_max"}:
                field.required = True
                field.widget.attrs["min"] = settings.MIN_DATE.isoformat()
            if name == "group_by":
                field.choices = (("auto", _("Auto")), ("day", _("Daily")),
                                 ("week", _("Weekly")), ("month", _("Monthly")),
                                 ("year", _("Yearly")))
            if name == "is_reply":
                field = forms.ChoiceField(label=labels[name], required=False, choices=(
                    ("", _("All messages")), ("true", _("Replies only")),
                    ("false", _("Exclude replies")),
                ))
            if name == "tag_match":
                field.choices = (("any", _("Any tag")), ("all", _("All tags")))
            if name == "tag":
                field.widget = forms.HiddenInput(attrs={"data-list-value": "true"})
                field.dashboard_autocomplete = True
                field.dashboard_autocomplete_kind = "tags"
                field.dashboard_autocomplete_url = reverse("app_dashboard:lookup", kwargs={"kind": "tags"})
            self.fields[name] = field

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        start, end = cleaned["createdat_min"], cleaned["createdat_max"]
        if end < start:
            self.add_error("createdat_max", _("The end date must be on or after the start date."))
            return cleaned
        for name in ("createdat_min", "createdat_max"):
            if cleaned[name] < settings.MIN_DATE:
                self.add_error(name, _("Choose a date on or after %(date)s.") % {"date": settings.MIN_DATE.isoformat()})
        if self.errors:
            return cleaned
        grouping = cleaned.get("group_by") or "auto"
        self.auto_interval = grouping == "auto"
        if grouping == "auto":
            grouping = automatic_grouping(start, end)
        try:
            if cleaned.get("range") != "all":
                calendar_periods(start, end, grouping)
        except ValueError:
            self.add_error("group_by", _("This range has too many points. Choose Auto or a wider interval."))
            return cleaned
        values = {name: value for name, value in cleaned.items()
                  if name != "range" and value not in (None, "", [])}
        values.update(group_by=grouping, z_score=True)
        for name in ("createdat_min", "createdat_max", "stored_since"):
            if name in values:
                values[name] = values[name].strftime(settings.FORMAT_DATE)
        serializer = StatsMsgSerializer(data=values)
        if not serializer.is_valid():
            def error_messages(value):
                if isinstance(value, dict):
                    return [message for errors in value.values() for message in error_messages(errors)]
                if isinstance(value, (list, tuple)):
                    return [message for error in value for message in error_messages(error)]
                return [str(value)]

            for name, errors in serializer.errors.items():
                self.add_error(name if name in self.fields else None, error_messages(errors))
            return cleaned
        self.statistics_parameters = dict(serializer.validated_data)
        if "tag" in self.statistics_parameters:
            self.statistics_parameters["tags"] = self.statistics_parameters.pop("tag")
        return cleaned


def _django_field(name, serializer_field):
    """Build the closest useful HTML field for a DRF serializer field."""

    required = bool(serializer_field.required)
    initial = _initial_value(serializer_field)

    if isinstance(serializer_field, serializers.ListField):
        child = serializer_field.child
        autocomplete_kind = AUTOCOMPLETE_FIELD_KINDS.get(name)
        if autocomplete_kind:
            field = SplitListField(
                required=required,
                initial=initial,
                widget=forms.HiddenInput(attrs={"data-list-value": "true"}),
            )
            field.dashboard_autocomplete = True
            field.dashboard_autocomplete_kind = autocomplete_kind
            field.dashboard_autocomplete_url = reverse(
                "app_dashboard:lookup",
                kwargs={"kind": autocomplete_kind},
            )
        elif hasattr(child, "choices"):
            field = forms.MultipleChoiceField(
                choices=_choice_pairs(child.choices),
                required=required,
                initial=initial if initial is not None else [],
            )
        else:
            field = SplitListField(
                required=required,
                initial=initial,
                widget=forms.HiddenInput(attrs={"data-list-value": "true"}),
            )
    elif isinstance(serializer_field, serializers.BooleanField):
        if getattr(serializer_field, "allow_null", False):
            field = forms.TypedChoiceField(
                choices=(
                    ("", _("Any")),
                    ("true", _("Yes")),
                    ("false", _("No")),
                ),
                required=False,
                coerce=_coerce_nullable_bool,
                empty_value=None,
                initial="" if initial is None else ("true" if initial else "false"),
            )
        else:
            field = forms.BooleanField(
                required=False,
                initial=False if initial is None else initial,
                widget=forms.CheckboxInput(attrs={"class": "toggle-input"}),
            )
    elif isinstance(serializer_field, serializers.IntegerField):
        field = forms.IntegerField(
            required=required,
            min_value=serializer_field.min_value,
            max_value=serializer_field.max_value,
            initial=initial,
        )
    elif isinstance(serializer_field, serializers.DateField):
        field = forms.DateField(
            required=required,
            input_formats=(settings.FORMAT_DATE, "%Y-%m-%d"),
            initial=initial,
            widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        )
    elif isinstance(serializer_field, serializers.DateTimeField):
        field = forms.DateTimeField(
            required=required,
            input_formats=("%Y-%m-%dT%H:%M",),
            initial=initial,
            widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        )
    elif isinstance(serializer_field, serializers.ChoiceField):
        field = forms.ChoiceField(
            choices=_choice_pairs(serializer_field.choices),
            required=required,
            initial=initial,
        )
    else:
        widget = forms.Textarea(attrs={"rows": 3}) if name == "instruct" else forms.TextInput()
        field = forms.CharField(
            required=required,
            max_length=getattr(serializer_field, "max_length", None),
            initial=initial,
            widget=widget,
        )

    field.label = _label(name)
    # Serializer help text is written for API clients and often contains
    # internal query parameter names. Keep those names out of the operator UI.
    field.help_text = _("Separate values with commas or line breaks.") if isinstance(serializer_field, serializers.ListField) and not hasattr(serializer_field.child, "choices") else ""
    field.help_text = PIPELINE_FIELD_HELP_TEXTS.get(name, field.help_text)
    field.dashboard_list = isinstance(serializer_field, serializers.ListField) and not hasattr(serializer_field.child, "choices")
    field.dashboard_advanced = name in ADVANCED_PARAMETER_FIELDS
    if isinstance(serializer_field, serializers.ListField) and name in AUTOCOMPLETE_FIELD_KINDS:
        field.dashboard_list = True
    return field


def _public_field_name(pipeline, serializer_name):
    for public_name, internal_name in pipeline.parameter_aliases.items():
        if serializer_name == internal_name:
            return public_name
    return serializer_name


def _parameter_initials(pipeline, initial):
    if not initial:
        return {}
    return present_pipeline_parameters(pipeline, initial)


def _add_parameter_fields(form, pipeline_key, initial=None):
    pipeline = get_scheduled_pipeline(pipeline_key)
    serializer = pipeline.parameter_serializer()
    initial = _parameter_initials(pipeline, initial or {})
    form.dashboard_pipeline = pipeline
    form.dashboard_parameter_fields = []
    form.dashboard_parameter_bound_fields = []
    form.dashboard_basic_parameter_bound_fields = []
    form.dashboard_advanced_parameter_bound_fields = []

    for serializer_name, serializer_field in serializer.fields.items():
        public_name = _public_field_name(pipeline, serializer_name)
        if public_name in HIDDEN_PARAMETER_FIELDS or serializer_name in HIDDEN_PARAMETER_FIELDS:
            continue
        field = _django_field(public_name, serializer_field)
        if public_name in initial:
            field.initial = initial[public_name]
        form.fields[public_name] = field
        form.dashboard_parameter_fields.append(public_name)
        bound_field = form[public_name]
        if field.help_text and isinstance(field.widget, forms.CheckboxInput):
            field.widget.attrs["aria-describedby"] = f"{bound_field.id_for_label}-help"
        form.dashboard_parameter_bound_fields.append(bound_field)
        if field.dashboard_advanced:
            form.dashboard_advanced_parameter_bound_fields.append(bound_field)
        else:
            form.dashboard_basic_parameter_bound_fields.append(bound_field)

    bound_by_name = {
        bound_field.name: bound_field
        for bound_field in form.dashboard_basic_parameter_bound_fields
    }
    grouped_names = set()
    form.dashboard_parameter_groups = []
    for group_label, names in PARAMETER_GROUPS:
        fields = [bound_by_name[name] for name in names if name in bound_by_name]
        if fields:
            grouped_names.update(field.name for field in fields)
            form.dashboard_parameter_groups.append({"label": group_label, "fields": fields})

    remaining_fields = [
        field
        for field in form.dashboard_basic_parameter_bound_fields
        if field.name not in grouped_names
    ]
    if remaining_fields:
        form.dashboard_parameter_groups.append(
            {"label": _("Other filters"), "fields": remaining_fields}
        )


class PipelineParameterForm(forms.Form):
    """Dynamic parameter form backed by a registered pipeline serializer."""

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        parameters = {
            name: value
            for name, value in cleaned.items()
            if name in getattr(self, "dashboard_parameter_fields", [])
            and value not in (None, "", [])
        }
        try:
            self.cleaned_parameters = normalize_pipeline_parameters(
                self.dashboard_pipeline,
                parameters,
            )
        except serializers.ValidationError as exc:
            raise forms.ValidationError(str(exc.detail)) from exc
        return cleaned


def build_pipeline_parameter_form(pipeline_key, data=None, initial=None):
    form = PipelineParameterForm(data=data, initial=initial)
    _add_parameter_fields(form, pipeline_key, initial=initial)
    return form


class JobFilterForm(forms.Form):
    """Validate the operator-facing filters for the Job list."""

    status = forms.ChoiceField(label=_("Status"), required=False)
    mode = forms.ChoiceField(label=_("Execution type"), required=False)
    started_from = forms.DateField(
        label=_("Started from"),
        required=False,
        input_formats=("%Y-%m-%d",),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    started_until = forms.DateField(
        label=_("Started until"),
        required=False,
        input_formats=("%Y-%m-%d",),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    finished_from = forms.DateField(
        label=_("Finished from"),
        required=False,
        input_formats=("%Y-%m-%d",),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    finished_until = forms.DateField(
        label=_("Finished until"),
        required=False,
        input_formats=("%Y-%m-%d",),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def __init__(self, status_choices=(), mode_choices=(), *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [("", _("All")), *status_choices]
        self.fields["mode"].choices = [("", _("All")), *mode_choices]

    def clean(self):
        cleaned = super().clean()
        for start_name, end_name in (
            ("started_from", "started_until"),
            ("finished_from", "finished_until"),
        ):
            start = cleaned.get(start_name)
            end = cleaned.get(end_name)
            if start and end and start > end:
                self.add_error(end_name, _("The end date must be on or after the start date."))
        return cleaned


class JobLaunchForm(PipelineParameterForm):
    pipeline = forms.ChoiceField(label=_("Operation"))

    def __init__(self, pipeline_key=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(key, pipeline_label(key)) for key in get_scheduled_pipelines()]
        self.fields["pipeline"].choices = choices
        pipeline_key = pipeline_key or (choices[0][0] if choices else None)
        self.pipeline_key = pipeline_key
        if pipeline_key:
            _add_parameter_fields(self, pipeline_key)
            self.fields["pipeline"].initial = pipeline_key

    def clean_pipeline(self):
        value = self.cleaned_data["pipeline"]
        if value not in get_scheduled_pipelines():
            raise forms.ValidationError(_("Choose an available operation."))
        self.pipeline_key = value
        return value

    def clean(self):
        cleaned = super(PipelineParameterForm, self).clean()
        if self.errors:
            return cleaned
        pipeline_key = cleaned.get("pipeline") or self.pipeline_key
        if not pipeline_key:
            raise forms.ValidationError(_("Choose an operation."))
        self.pipeline_key = pipeline_key
        parameters = {
            name: value
            for name, value in cleaned.items()
            if name in getattr(self, "dashboard_parameter_fields", [])
            and value not in (None, "", [])
        }
        try:
            self.cleaned_parameters = normalize_pipeline_parameters(
                get_scheduled_pipeline(pipeline_key),
                parameters,
            )
        except serializers.ValidationError as exc:
            raise forms.ValidationError(str(exc.detail)) from exc
        return cleaned


def _schedule_kind(instance):
    if instance is None:
        return "repeat"
    if instance.clocked_id:
        return "once"
    if instance.crontab_id:
        return "calendar"
    return "repeat"


def _stored_parameters(instance, pipeline):
    if instance is None:
        return {}
    try:
        raw = json.loads(instance.kwargs or "{}")
    except (TypeError, ValueError):
        raw = {}
    parameters, _ = extract_scheduler_context(raw if isinstance(raw, dict) else {})
    return present_pipeline_parameters(pipeline, parameters)


def _stored_lifecycle(instance):
    if instance is None:
        return public_lifecycle_options()
    try:
        raw = json.loads(instance.kwargs or "{}")
    except (TypeError, ValueError):
        raw = {}
    _, context = extract_scheduler_context(raw if isinstance(raw, dict) else {})
    return public_lifecycle_options(context)


class ScheduleForm(PipelineParameterForm):
    name = forms.CharField(label=_("Name"), max_length=200)
    description = forms.CharField(label=_("Description"), required=False, widget=forms.Textarea(attrs={"rows": 2}))
    pipeline = forms.ChoiceField(label=_("Operation"))
    schedule_kind = forms.ChoiceField(
        label=_("Schedule type"),
        choices=(
            ("repeat", _("Repeat every...")),
            ("calendar", _("Schedule by calendar")),
            ("once", _("Run once")),
        ),
    )
    repeat_every = forms.IntegerField(label=_("Every"), min_value=1, required=False)
    repeat_period = forms.ChoiceField(label=_("Unit"), required=False, choices=IntervalSchedule.PERIOD_CHOICES)
    calendar_minute = forms.CharField(label=_("Minute"), required=False, initial="*")
    calendar_hour = forms.CharField(label=_("Hour"), required=False, initial="*")
    calendar_day_of_week = forms.CharField(label=_("Day of the week"), required=False, initial="*")
    calendar_day_of_month = forms.CharField(label=_("Day of the month"), required=False, initial="*")
    calendar_month_of_year = forms.CharField(label=_("Month"), required=False, initial="*")
    calendar_timezone = forms.ChoiceField(
        label=_("Time zone"),
        required=False,
        choices=tuple((value, value) for value in sorted(available_timezones())),
    )
    once_time = forms.DateTimeField(
        label=_("Date and time"),
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
    )
    enabled = forms.BooleanField(
        label=_("Enabled"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "toggle-input"}),
    )
    start_time = forms.DateTimeField(
        label=_("Start from"),
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
    )
    expires = forms.DateTimeField(
        label=_("Expire at"),
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
    )
    expire_seconds = forms.IntegerField(label=_("Expire after (seconds)"), min_value=0, required=False)
    auto_finalize_failed = forms.BooleanField(
        label=_("Finish failed scheduled Jobs as partial"),
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle-input"}),
        help_text=_(
            "After automatic retries are exhausted, discard this scheduled Job's own failed batches "
            "shortly before the next run. Child processing remains retryable."
        ),
    )
    replace_running_job = forms.BooleanField(
        label=_("Enable replacement"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "toggle-input"}),
    )
    retry_grace_seconds = forms.IntegerField(
        label=_("Retry safety margin (seconds)"),
        min_value=1,
        required=False,
        initial=300,
        help_text=_(
            "Defaults to 300 seconds. For short intervals, the effective margin "
            "cannot exceed one quarter of the interval."
        ),
    )

    def __init__(self, pipeline_key=None, instance=None, *args, **kwargs):
        self.instance = instance
        if instance is not None:
            pipeline = get_scheduled_pipeline_for_task(instance.task)
            pipeline_key = pipeline.key
            initial = _stored_parameters(instance, pipeline)
        else:
            pipeline = get_scheduled_pipeline(pipeline_key)
            initial = kwargs.pop("parameter_initial", {})

        super().__init__(*args, **kwargs)
        choices = [(key, pipeline_label(key)) for key in get_scheduled_pipelines()]
        self.fields["pipeline"].choices = choices
        self.pipeline_key = pipeline_key
        self.fields["pipeline"].initial = pipeline_key
        self.fields["pipeline"].disabled = instance is not None
        self.fields["schedule_kind"].initial = _schedule_kind(instance)
        self._set_schedule_initial(instance)
        lifecycle = _stored_lifecycle(instance)
        for name in LIFECYCLE_FIELDS:
            self.fields[name].initial = lifecycle[name]
        _add_parameter_fields(self, pipeline_key, initial=initial)

    def _set_schedule_initial(self, instance):
        if instance is None:
            self.fields["repeat_every"].initial = 1
            self.fields["repeat_period"].initial = IntervalSchedule.MINUTES
            self.fields["calendar_timezone"].initial = settings.TIME_ZONE
            return
        self.fields["name"].initial = instance.name
        self.fields["description"].initial = instance.description
        self.fields["enabled"].initial = instance.enabled
        self.fields["start_time"].initial = instance.start_time
        self.fields["expires"].initial = instance.expires
        self.fields["expire_seconds"].initial = instance.expire_seconds
        if instance.interval_id:
            self.fields["repeat_every"].initial = instance.interval.every
            self.fields["repeat_period"].initial = instance.interval.period
        if instance.crontab_id:
            for name in ("minute", "hour", "day_of_week", "day_of_month", "month_of_year"):
                self.fields[f"calendar_{name}"].initial = getattr(instance.crontab, name)
            self.fields["calendar_timezone"].initial = str(instance.crontab.timezone)
        if instance.clocked_id:
            self.fields["once_time"].initial = instance.clocked.clocked_time

    def clean_pipeline(self):
        value = self.cleaned_data["pipeline"] if not self.fields["pipeline"].disabled else self.pipeline_key
        if value not in get_scheduled_pipelines():
            raise forms.ValidationError(_("Choose an available operation."))
        self.pipeline_key = value
        return value

    def clean(self):
        cleaned = super(PipelineParameterForm, self).clean()
        if self.errors:
            return cleaned

        pipeline_key = self.pipeline_key
        parameters = {
            name: value
            for name, value in cleaned.items()
            if name in getattr(self, "dashboard_parameter_fields", [])
            and value not in (None, "", [])
        }
        try:
            self.cleaned_parameters = normalize_pipeline_parameters(
                get_scheduled_pipeline(pipeline_key),
                parameters,
            )
        except serializers.ValidationError as exc:
            raise forms.ValidationError(str(exc.detail)) from exc

        kind = cleaned.get("schedule_kind")
        if kind == "repeat" and not cleaned.get("repeat_every"):
            self.add_error("repeat_every", _("Specify how often it repeats."))
        if (
            kind == "repeat"
            and cleaned.get("repeat_every")
            and cleaned.get("repeat_period")
            and self.data.get("advanced_options_touched") == "1"
        ):
            margin = cleaned.get("retry_grace_seconds")
            if margin is not None:
                maximum = max_retry_grace_seconds_for_interval(
                    cleaned["repeat_every"], cleaned["repeat_period"]
                )
                if margin > maximum:
                    self.add_error(
                        "retry_grace_seconds",
                        _(
                            "For this interval, the safety margin cannot exceed "
                            "%(maximum)s seconds."
                        )
                        % {"maximum": maximum},
                    )
        if kind == "calendar":
            for name in (
                "calendar_minute",
                "calendar_hour",
                "calendar_day_of_week",
                "calendar_day_of_month",
                "calendar_month_of_year",
                "calendar_timezone",
            ):
                if not cleaned.get(name):
                    self.add_error(name, _("This field is required for the calendar."))
        if kind == "once" and not cleaned.get("once_time"):
            self.add_error("once_time", _("Specify when it should run."))
        return cleaned

    @staticmethod
    def _aware(value):
        if value is None or timezone.is_aware(value):
            return value
        return timezone.make_aware(value, timezone.get_current_timezone())

    def trigger_payload(self):
        cleaned = self.cleaned_data
        stored_lifecycle = _stored_lifecycle(self.instance)
        advanced_options_touched = self.data.get("advanced_options_touched") == "1"
        auto_finalize_failed = (
            cleaned.get("auto_finalize_failed")
            if advanced_options_touched
            else stored_lifecycle["auto_finalize_failed"]
        )
        replace_running_job = (
            cleaned.get("replace_running_job")
            if advanced_options_touched
            else stored_lifecycle["replace_running_job"]
        )
        retry_grace_seconds = (
            cleaned.get("retry_grace_seconds") or stored_lifecycle["retry_grace_seconds"]
            if advanced_options_touched
            else stored_lifecycle["retry_grace_seconds"]
        )
        payload = {
            "name": cleaned["name"],
            "description": cleaned.get("description", ""),
            "pipeline": self.pipeline_key,
            "parameters": getattr(self, "cleaned_parameters", {}),
            "enabled": cleaned.get("enabled", False),
            "one_off": cleaned.get("schedule_kind") == "once",
            "start_time": self._aware(cleaned.get("start_time")),
            "expires": self._aware(cleaned.get("expires")),
            "expire_seconds": cleaned.get("expire_seconds"),
            "auto_finalize_failed": auto_finalize_failed,
            "replace_running_job": replace_running_job,
            "retry_grace_seconds": retry_grace_seconds,
        }
        kind = cleaned["schedule_kind"]
        if kind == "repeat":
            payload["interval"] = {
                "every": cleaned["repeat_every"],
                "period": cleaned["repeat_period"],
            }
        elif kind == "calendar":
            payload["crontab"] = {
                "minute": cleaned["calendar_minute"],
                "hour": cleaned["calendar_hour"],
                "day_of_week": cleaned["calendar_day_of_week"],
                "day_of_month": cleaned["calendar_day_of_month"],
                "month_of_year": cleaned["calendar_month_of_year"],
                "timezone": cleaned["calendar_timezone"],
            }
        else:
            payload["clocked"] = {"clocked_time": self._aware(cleaned["once_time"])}
        return payload
