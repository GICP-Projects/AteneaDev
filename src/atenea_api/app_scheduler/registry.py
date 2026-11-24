"""Central catalog of ProcessingJob pipelines that can be scheduled.

This is the public scheduling allowlist: each entry maps a stable pipeline key
to its app-owned Celery launcher and the serializer for that launcher's
parameters. The pipeline implementations and Job creation remain in their
owning apps; this module only declares the scheduling contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from rest_framework import serializers

from app_entity.job_keys import NER_PROCESSOR
from app_metadata.job_keys import EMBEDDINGS_PROCESSOR, SENTIMENT_PROCESSOR
from app_telegram.job_keys import (
    TELEGRAM_ACCESS_PROCESSOR,
    TELEGRAM_INDEX_MESSAGES_PROCESSOR,
    TELEGRAM_POPULATE_PROCESSOR,
    TELEGRAM_SCAN_COMMENTS_PROCESSOR,
    TELEGRAM_SCAN_PROCESSOR,
)
from app_telegram.serializers import (
    EmbedFilterMsgSerializer,
    FilterMsgSerializer,
    FilterRoomSerializer,
    NERFilterMsgSerializer,
    ScanRepliesMsgSerializer,
    ScanRoomSerializer,
    SimpleFilterSeedSerializer,
)


@dataclass(frozen=True)
class ScheduledPipeline:
    key: str
    task: str
    parameter_serializer: type[serializers.Serializer]
    # Public API/launcher parameter name -> filter-serializer field name.
    parameter_aliases: Mapping[str, str] = field(default_factory=dict)
    description: str = ""


class UnknownScheduledPipeline(LookupError):
    """Raised when a key or task is not an explicitly registered launcher."""


class ScheduledScanCommentsParametersSerializer(ScanRepliesMsgSerializer):
    """Only the fields accepted by the scan-comments launcher."""


# Keep this catalog in one place. Adding a scheduled pipeline requires an
# explicit public key, app-owned launcher, and parameter contract here.
SCHEDULED_PIPELINES = (
    ScheduledPipeline(
        key=f"{NER_PROCESSOR}.backfill",
        task="app_entity.services.pipelines.ner_extraction_msgs_pipeline",
        parameter_serializer=NERFilterMsgSerializer,
        parameter_aliases={"tags": "tag"},
        description="Create a durable manual NER backfill Job.",
    ),
    ScheduledPipeline(
        key=EMBEDDINGS_PROCESSOR,
        task="app_metadata.services.pipelines.embeddings_msgs_pipeline",
        parameter_serializer=EmbedFilterMsgSerializer,
        parameter_aliases={"tags": "tag"},
        description="Create a durable message-embeddings Job.",
    ),
    ScheduledPipeline(
        key=SENTIMENT_PROCESSOR,
        task="app_metadata.services.pipelines.sentiment_classification_msg_pipeline",
        parameter_serializer=FilterMsgSerializer,
        parameter_aliases={"tags": "tag"},
        description="Create a durable message-sentiment Job.",
    ),
    ScheduledPipeline(
        key=TELEGRAM_SCAN_PROCESSOR,
        task="app_telegram.services.pipelines.scan_pipeline",
        parameter_serializer=ScanRoomSerializer,
        parameter_aliases={"tags": "tag"},
        description="Scan eligible Telegram rooms and start their durable message workflow.",
    ),
    ScheduledPipeline(
        key=TELEGRAM_SCAN_COMMENTS_PROCESSOR,
        task="app_telegram.services.pipelines.scan_comments_pipeline",
        parameter_serializer=ScheduledScanCommentsParametersSerializer,
        parameter_aliases={"tags": "tag", "langs": "lang", "types": "type"},
        description="Scan message comments and start their durable processing workflow.",
    ),
    ScheduledPipeline(
        key=TELEGRAM_ACCESS_PROCESSOR,
        task="app_telegram.services.pipelines.access_room_pipeline",
        parameter_serializer=FilterRoomSerializer,
        parameter_aliases={"tags": "tag"},
        description="Recover access hashes for Telegram rooms using ProcessingJob batches.",
    ),
    ScheduledPipeline(
        key=TELEGRAM_POPULATE_PROCESSOR,
        task="app_telegram.services.pipelines.populate_pipeline",
        parameter_serializer=SimpleFilterSeedSerializer,
        parameter_aliases={"tags": "tag"},
        description="Populate eligible seeds and durably index resolved rooms.",
    ),
    ScheduledPipeline(
        key=TELEGRAM_INDEX_MESSAGES_PROCESSOR,
        task="app_telegram.services.pipelines.index_msgs_pipeline",
        parameter_serializer=FilterMsgSerializer,
        parameter_aliases={"tags": "tag"},
        description="Create a durable manual message-indexing Job.",
    ),
)


def _build_scheduled_pipeline_registry() -> dict[str, ScheduledPipeline]:
    registry: dict[str, ScheduledPipeline] = {}
    task_keys: dict[str, str] = {}

    for pipeline in SCHEDULED_PIPELINES:
        if pipeline.key in registry:
            raise ValueError(f"Duplicate scheduled pipeline key: {pipeline.key!r}")
        if pipeline.task in task_keys:
            raise ValueError(
                f"Celery launcher {pipeline.task!r} is registered by both "
                f"{task_keys[pipeline.task]!r} and {pipeline.key!r}"
            )
        registry[pipeline.key] = pipeline
        task_keys[pipeline.task] = pipeline.key

    return registry


_SCHEDULED_PIPELINES_BY_KEY = _build_scheduled_pipeline_registry()


def get_scheduled_pipelines() -> dict[str, ScheduledPipeline]:
    return dict(_SCHEDULED_PIPELINES_BY_KEY)


def get_scheduled_pipeline_task_names() -> list[str]:
    return [pipeline.task for pipeline in _SCHEDULED_PIPELINES_BY_KEY.values()]


def get_scheduled_pipeline(key: str) -> ScheduledPipeline:
    try:
        return _SCHEDULED_PIPELINES_BY_KEY[key]
    except KeyError as exc:
        raise UnknownScheduledPipeline(key) from exc


def get_scheduled_pipeline_for_task(task: str) -> ScheduledPipeline:
    for pipeline in _SCHEDULED_PIPELINES_BY_KEY.values():
        if pipeline.task == task:
            return pipeline
    raise UnknownScheduledPipeline(task)


def normalize_pipeline_parameters(pipeline: ScheduledPipeline, parameters) -> dict:
    """Validate parameters and normalize aliases to the launcher's signature."""

    if not isinstance(parameters, dict):
        raise serializers.ValidationError("parameters must be a JSON object")

    serializer = pipeline.parameter_serializer()
    raw = dict(parameters)
    aliases = dict(pipeline.parameter_aliases)
    allowed = (set(serializer.fields) - set(aliases.values())) | set(aliases)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise serializers.ValidationError({key: ["Unknown parameter."] for key in unknown})

    for public_name, serializer_name in aliases.items():
        if public_name in raw:
            if serializer_name in raw:
                raise serializers.ValidationError(
                    {public_name: [f"Provide only {public_name!r}, not both aliases."]}
                )
            raw[serializer_name] = raw.pop(public_name)

    serializer = pipeline.parameter_serializer(data=raw)
    serializer.is_valid(raise_exception=True)
    normalized = dict(serializer.validated_data)

    # The API and stored Beat kwargs use the actual Celery task signature.
    for public_name, serializer_name in aliases.items():
        if serializer_name in normalized:
            normalized[public_name] = normalized.pop(serializer_name)

    return normalized


def present_pipeline_parameters(pipeline: ScheduledPipeline, parameters) -> dict:
    """Normalize parameters from an existing Beat row for API presentation."""

    if not isinstance(parameters, dict):
        return {}
    normalized = dict(parameters)
    for public_name, serializer_name in pipeline.parameter_aliases.items():
        if serializer_name in normalized and public_name not in normalized:
            normalized[public_name] = normalized.pop(serializer_name)
    return normalized
