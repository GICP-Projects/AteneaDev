"""Pydantic contract for the persisted ``ProcessingBatch.result_summary`` format."""

from typing import Literal

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SummaryCount(BaseModel):
    """A count of one kind of data flowing through a Batch stage."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    count: int | None = Field(default=None, ge=0, strict=True)
    kind: str = Field(min_length=1)
    unit: str = Field(default="records", min_length=1)


class SummaryReason(BaseModel):
    """A counted reason why input was skipped or failed."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    count: int | None = Field(default=None, ge=0, strict=True)


class StageSummary(BaseModel):
    """Measured inputs and outcomes for one workflow stage."""

    model_config = ConfigDict(extra="forbid")

    inputs: list[SummaryCount] = Field(default_factory=list)
    processed: list[SummaryCount] = Field(default_factory=list)
    outputs: list[SummaryCount] = Field(default_factory=list)
    skipped: list[SummaryReason] = Field(default_factory=list)
    failed: list[SummaryReason] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ProcessingSummary(BaseModel):
    """Versioned persisted contract for ``ProcessingBatch.result_summary``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    stages: dict[str, StageSummary] = Field(default_factory=dict)


def empty_processing_summary() -> dict:
    """Return a fresh, valid summary payload for a newly created Batch."""

    return ProcessingSummary().model_dump(mode="json")


def processing_summary_payload(value) -> dict:
    """Validate a summary and return its JSON-serialisable representation."""

    return ProcessingSummary.model_validate(value).model_dump(mode="json")


def validate_processing_summary(value) -> None:
    """Django JSONField validator backed by the persisted Pydantic contract."""

    try:
        ProcessingSummary.model_validate(value)
    except ValidationError as exc:
        raise DjangoValidationError(f"Invalid ProcessingBatch result_summary: {exc}") from exc
