from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True)
class ServiceAuthConfig:
    api_key: Optional[str]
    auth_header: str = "ApiKey"


@dataclass(frozen=True)
class ServiceEndpointConfig:
    path: str
    response_label: str


@dataclass(frozen=True)
class BaseServiceConfig:
    host: str
    auth: ServiceAuthConfig
    endpoints: Mapping[str, ServiceEndpointConfig]
    max_items_by_request: int
    max_parallel_requests: int
    default_return_format: type


@dataclass(frozen=True)
class NERServiceConfig(BaseServiceConfig):
    languages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OpenAIEmbeddingsConfig:
    base_url: str
    model: str
    api_key: Optional[str] = None
    dimensions: Optional[int] = None
    max_items_by_request: int = 100
    max_parallel_requests: int = 1
    timeout: int = 60


@dataclass(frozen=True)
class QdrantConfig:
    url: str
    api_key: Optional[str] = None
    timeout: int = 60
    search_hnsw_ef: Optional[int] = None
    search_exact: Optional[bool] = None
    hnsw_m: Optional[int] = None
    hnsw_ef_construct: Optional[int] = None
    hnsw_full_scan_threshold: Optional[int] = None
    upsert_batch_size: int = 1000
    upsert_max_retries: int = 2
    upsert_retry_delay_seconds: int = 2
    collections: Mapping[str, str] = field(default_factory=dict)
