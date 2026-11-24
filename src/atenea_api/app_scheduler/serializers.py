import json
from zoneinfo import available_timezones

from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
)
from django.db.models import Sum
from rest_framework import serializers

from app_base.models import GeneralEncoder
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.contracts import processing_summary_payload
from app_scheduler.enums import JobMode, JobStatus
from pydantic import ValidationError as PydanticValidationError
from app_scheduler.registry import (
    UnknownScheduledPipeline,
    get_scheduled_pipeline,
    get_scheduled_pipeline_for_task,
    normalize_pipeline_parameters,
    present_pipeline_parameters,
)
from app_scheduler.schedule_policy import (
    DEFAULT_LIFECYCLE_OPTIONS,
    LIFECYCLE_FIELDS,
    SCHEDULER_CONTEXT_KWARG,
    build_scheduler_context,
    extract_scheduler_context,
    normalize_lifecycle_options,
    public_lifecycle_options,
)


class ProcessingBatchSerializer(serializers.ModelSerializer):
    def validate_result_summary(self, value):
        try:
            return processing_summary_payload(value)
        except PydanticValidationError as exc:
            raise serializers.ValidationError(exc.errors()) from exc

    class Meta:
        model = ProcessingBatch
        fields = [
            "id", "ordinal", "status", "item_count", "item_content_type", "resource_key",
            "processed_count", "failed_count", "deferred_count", "skipped_count",
            "current_stage", "completed_stages", "stage_attempts", "processor_version",
            "processor_metadata", "result_summary", "source_handoff", "recover_on_lease_expiry",
            "queued_at", "lease_until", "run_after", "last_error",
            "created_at", "completed_at", "updated_at",
        ]


class ProcessingJobSerializer(serializers.ModelSerializer):
    batches_url = serializers.SerializerMethodField()
    child_job_ids = serializers.SerializerMethodField()
    pending_handoff_count = serializers.SerializerMethodField()
    pending_handoff_items = serializers.SerializerMethodField()

    class Meta:
        model = ProcessingJob
        fields = [
            "id", "processor", "mode", "workflow", "status", "parent_job", "child_job_ids",
            "filters", "source_snapshot_at", "source_cursor", "source_exhausted", "input_closed_at",
            "input_outcome", "failures_abandoned_at", "lifecycle_policy", "retry_deadline_at",
            "source_finished_at", "fingerprint",
            "batch_size", "max_inflight_batches",
            "estimated_batches_total",
            "total_items", "processed_items", "failed_items", "deferred_items", "skipped_items",
            "descendant_processed_items", "descendant_failed_items", "descendant_deferred_items",
            "descendant_skipped_items", "total_batches", "completed_batches", "failed_batches",
            "pending_handoff_count", "pending_handoff_items",
            "request_token", "last_error",
            "created_at", "started_at", "completed_at", "updated_at", "batches_url",
        ]
        read_only_fields = fields

    def get_batches_url(self, obj):
        request = self.context.get("request")
        path = f"/api/v1/scheduler/jobs/{obj.pk}/batches"
        return request.build_absolute_uri(path) if request else path

    def get_child_job_ids(self, obj):
        return [str(job_id) for job_id in obj.child_jobs.values_list("id", flat=True)]

    def get_pending_handoff_count(self, obj):
        return obj.incoming_handoffs.filter(status=ProcessingHandoff.PENDING).count()

    def get_pending_handoff_items(self, obj):
        return (
            obj.incoming_handoffs.filter(status=ProcessingHandoff.PENDING)
            .aggregate(total=Sum("item_count"))["total"]
            or 0
        )


class ProcessingJobListSerializer(serializers.Serializer):
    processor = serializers.CharField(required=False)
    mode = serializers.ChoiceField(choices=JobMode.choices, required=False)
    status = serializers.ChoiceField(choices=JobStatus.choices, required=False)


class IntervalScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntervalSchedule
        fields = "__all__"


class ClockedScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClockedSchedule
        fields = "__all__"


class CrontabScheduleSerializer(serializers.ModelSerializer):
    # Map timezone fix to allow serialization (`timezone_field.TimeZoneField` is not JSON serializable)
    timezone = serializers.ChoiceField(choices=available_timezones())

    class Meta:
        model = CrontabSchedule
        fields = "__all__"


class StrictInputSerializerMixin:
    """Reject misspelled/unsupported keys instead of silently ignoring them."""

    allowed_extra_input_fields = frozenset()

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            normalized = {}
            errors = {}
            for key in data.keys():
                field_name = key.replace("-", "_")
                if field_name in self.fields:
                    if field_name in normalized:
                        errors[key] = ["This field was provided more than once."]
                    else:
                        normalized[field_name] = data.get(key)
                elif key in self.allowed_extra_input_fields:
                    normalized[key] = data.get(key)
                else:
                    errors[key] = ["Unknown field."]
            if errors:
                raise serializers.ValidationError(
                    {key: errors[key] for key in sorted(errors)}
                )
            data = normalized
        return super().to_internal_value(data)


class ProcessingTriggerSerializer(StrictInputSerializerMixin, serializers.Serializer):
    """Public trigger contract; task paths and Celery kwargs stay private."""

    class Meta:
        model = PeriodicTask

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=200)
    pipeline = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    parameters = serializers.JSONField(required=False, default=dict)
    interval = IntervalScheduleSerializer(required=False)
    crontab = CrontabScheduleSerializer(required=False)
    clocked = ClockedScheduleSerializer(required=False)
    enabled = serializers.BooleanField(required=False, default=False)
    one_off = serializers.BooleanField(required=False, default=False)
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    expires = serializers.DateTimeField(required=False, allow_null=True)
    expire_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    auto_finalize_failed = serializers.BooleanField(
        required=False,
        default=DEFAULT_LIFECYCLE_OPTIONS["auto_finalize_failed"],
        help_text=(
            "Automatically discard this scheduled Job's own retry-exhausted batches as PARTIAL "
            "before the next execution. Child processing remains retryable."
        ),
    )
    replace_running_job = serializers.BooleanField(
        required=False,
        default=DEFAULT_LIFECYCLE_OPTIONS["replace_running_job"],
        help_text=(
            "Dangerous: cancel an equivalent RUNNING Job when the schedule "
            "fires instead of reusing it."
        ),
    )
    retry_grace_seconds = serializers.IntegerField(
        required=False,
        default=DEFAULT_LIFECYCLE_OPTIONS["retry_grace_seconds"],
        min_value=1,
        help_text="Safety margin before the next scheduled execution.",
    )
    last_run_at = serializers.DateTimeField(read_only=True, allow_null=True)
    total_run_count = serializers.IntegerField(read_only=True)

    schedule_fields = ("interval", "crontab", "clocked")

    def validate_name(self, value):
        matches = PeriodicTask.objects.filter(name=value)
        if self.instance:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise serializers.ValidationError("A trigger with this name already exists.")
        return value

    def validate_pipeline(self, value):
        try:
            get_scheduled_pipeline(value)
        except UnknownScheduledPipeline as exc:
            raise serializers.ValidationError("Unknown or non-schedulable pipeline.") from exc
        return value

    def validate(self, attrs):
        schedule_count = sum(field in attrs for field in self.schedule_fields)
        if self.instance is None and schedule_count != 1:
            raise serializers.ValidationError(
                "Provide exactly one of interval, crontab, or clocked."
            )
        if self.instance is not None and schedule_count > 1:
            raise serializers.ValidationError(
                "Provide at most one of interval, crontab, or clocked."
            )

        if self.instance is None:
            pipeline_key = attrs.get("pipeline")
        else:
            try:
                current_pipeline = get_scheduled_pipeline_for_task(self.instance.task)
            except UnknownScheduledPipeline as exc:
                raise serializers.ValidationError(
                    "This Beat entry is not a pipeline trigger."
                ) from exc
            pipeline_key = attrs.get("pipeline", current_pipeline.key)
            if pipeline_key != current_pipeline.key:
                raise serializers.ValidationError(
                    {"pipeline": "A trigger's pipeline cannot be changed."}
                )

        if "parameters" in attrs:
            try:
                attrs["parameters"] = normalize_pipeline_parameters(
                    get_scheduled_pipeline(pipeline_key), attrs["parameters"]
                )
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({"parameters": exc.detail}) from exc

        if self.instance is None:
            is_clocked = attrs.get("clocked") is not None
            one_off = attrs.get("one_off", False)
        elif any(field in attrs for field in self.schedule_fields):
            is_clocked = "clocked" in attrs and attrs["clocked"] is not None
            one_off = attrs.get("one_off", self.instance.one_off)
        else:
            is_clocked = self.instance.clocked_id is not None
            one_off = attrs.get("one_off", self.instance.one_off)
        if is_clocked and not one_off:
            raise serializers.ValidationError(
                {"clocked": "A clocked trigger must set one_off=true."}
            )

        return attrs

    @staticmethod
    def _save_schedule(schedule_type, schedule_data):
        schedule_model = {
            "interval": IntervalSchedule,
            "crontab": CrontabSchedule,
            "clocked": ClockedSchedule,
        }[schedule_type]
        schedule, _ = schedule_model.objects.get_or_create(**schedule_data)
        return schedule

    @staticmethod
    def _stored_kwargs(instance):
        try:
            raw = json.loads(instance.kwargs or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        parameters, context = extract_scheduler_context(raw)
        return parameters, context

    @staticmethod
    def _lifecycle_from_data(data):
        return normalize_lifecycle_options(
            {name: data[name] for name in LIFECYCLE_FIELDS if name in data}
        )

    @staticmethod
    def _write_kwargs(parameters, context):
        stored = dict(parameters)
        stored[SCHEDULER_CONTEXT_KWARG] = context
        return json.dumps(stored, cls=GeneralEncoder)

    def create(self, validated_data):
        pipeline = get_scheduled_pipeline(validated_data.pop("pipeline"))
        parameters = validated_data.pop("parameters", {})
        lifecycle = self._lifecycle_from_data(validated_data)
        for name in LIFECYCLE_FIELDS:
            validated_data.pop(name, None)
        for schedule_type in self.schedule_fields:
            if schedule_type in validated_data:
                validated_data[schedule_type] = self._save_schedule(
                    schedule_type, validated_data[schedule_type]
                )
        trigger = PeriodicTask.objects.create(
            task=pipeline.task,
            args="[]",
            kwargs="{}",
            **validated_data,
        )
        trigger.kwargs = self._write_kwargs(
            parameters,
            build_scheduler_context(trigger, lifecycle),
        )
        trigger.save(update_fields=["kwargs"])
        return trigger

    def update(self, instance, validated_data):
        validated_data.pop("pipeline", None)
        parameters, stored_context = self._stored_kwargs(instance)
        lifecycle = public_lifecycle_options(stored_context)
        for name in LIFECYCLE_FIELDS:
            if name in validated_data:
                lifecycle[name] = validated_data.pop(name)
        lifecycle = normalize_lifecycle_options(lifecycle)
        if "parameters" in validated_data:
            parameters = validated_data.pop("parameters")

        for schedule_type in self.schedule_fields:
            if schedule_type in validated_data:
                new_schedule = self._save_schedule(
                    schedule_type, validated_data.pop(schedule_type)
                )
                setattr(instance, schedule_type, new_schedule)
                for other_type in set(self.schedule_fields) - {schedule_type}:
                    setattr(instance, other_type, None)
                break

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        instance.kwargs = self._write_kwargs(
            parameters,
            build_scheduler_context(instance, lifecycle),
        )
        instance.save(update_fields=["kwargs"])
        return instance

    def to_representation(self, instance):
        try:
            pipeline = get_scheduled_pipeline_for_task(instance.task)
        except UnknownScheduledPipeline as exc:
            raise serializers.ValidationError(
                "This Beat entry is not a pipeline trigger."
            ) from exc

        stored_parameters, stored_context = self._stored_kwargs(instance)

        return {
            "id": instance.pk,
            "name": instance.name,
            "pipeline": pipeline.key,
            "description": instance.description,
            "parameters": present_pipeline_parameters(pipeline, stored_parameters),
            "interval": IntervalScheduleSerializer(instance.interval).data if instance.interval_id else None,
            "crontab": CrontabScheduleSerializer(instance.crontab).data if instance.crontab_id else None,
            "clocked": ClockedScheduleSerializer(instance.clocked).data if instance.clocked_id else None,
            "enabled": instance.enabled,
            "one_off": instance.one_off,
            "start_time": instance.start_time,
            "expires": instance.expires,
            "expire_seconds": instance.expire_seconds,
            **public_lifecycle_options(stored_context),
            "last_run_at": instance.last_run_at,
            "total_run_count": instance.total_run_count,
        }


class ProcessingTriggerFilterSerializer(StrictInputSerializerMixin, serializers.Serializer):
    allowed_extra_input_fields = frozenset({"page", "page-size"})

    pipeline = serializers.CharField(max_length=100, required=False)
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=200, required=False)
    schedule_type = serializers.ChoiceField(
        choices=("interval", "crontab", "clocked"), required=False
    )
    enabled = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate_pipeline(self, value):
        try:
            get_scheduled_pipeline(value)
        except UnknownScheduledPipeline as exc:
            raise serializers.ValidationError("Unknown or non-schedulable pipeline.") from exc
        return value
