"""Module contract for service scanners.

Every service module MUST implement ServiceModule. The dataclasses here
define the typed boundary between service scanners, the orchestrator,
and the reporter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

CONTRACT_SCHEMA_VERSION: int = 1


# -- Warning / permission records (schema matches current JSON shape) ----------


@dataclass(frozen=True)
class WarningRecord:
    message: str
    service: str
    timestamp: str


@dataclass(frozen=True)
class PermissionIssueRecord:
    message: str
    service: str
    action: str | None
    timestamp: str


# -- Findings -----------------------------------------------------------------


@dataclass(frozen=True)
class SourceBlock:
    count: int
    recommendations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class StatCardSpec:
    label: str
    source_path: str
    formatter: Literal["int", "currency", "percent", "size_gb"] = "int"


@dataclass(frozen=True)
class GroupingSpec:
    by: Literal["check_category", "resource_type", "source"]
    label_path: str | None = None


@dataclass(frozen=True)
class Group:
    label: str
    recommendations: tuple[dict[str, Any], ...]
    aggregated_savings: float = 0.0


@dataclass(frozen=True)
class ServiceFindings:
    service_name: str
    total_recommendations: int
    total_monthly_savings: float
    sources: dict[str, SourceBlock]
    optimization_descriptions: dict[str, dict[str, Any]] | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)
    total_count: int = 0
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def freeze(self) -> ServiceFindings:
        """Wrap mutable dict fields in MappingProxyType for read-only semantics."""
        return ServiceFindings(
            service_name=self.service_name,
            total_recommendations=self.total_recommendations,
            total_monthly_savings=self.total_monthly_savings,
            sources=dict(self.sources),
            optimization_descriptions=(
                dict(self.optimization_descriptions) if self.optimization_descriptions is not None else None
            ),
            extras=MappingProxyType(dict(self.extras)),
            total_count=self.total_count,
            schema_version=self.schema_version,
        )


# -- Module Protocol ----------------------------------------------------------


@runtime_checkable
class ServiceModule(Protocol):
    key: str
    cli_aliases: tuple[str, ...]
    display_name: str
    stat_cards: tuple[StatCardSpec, ...]
    grouping: GroupingSpec | None
    requires_cloudwatch: bool
    reads_fast_mode: bool

    def required_clients(self) -> tuple[str, ...]: ...
    def scan(self, ctx: Any) -> ServiceFindings: ...

    custom_grouping: Callable[[ServiceFindings], list[Group]] | None
