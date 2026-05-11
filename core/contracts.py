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
    """Non-fatal warning emitted during a scan.

    Attributes:
        message: Human-readable warning text.
        service: Service key that produced the warning.
        timestamp: ISO-8601 UTC timestamp.
    """

    message: str
    service: str
    timestamp: str


@dataclass(frozen=True)
class PermissionIssueRecord:
    """IAM permission gap encountered during a scan.

    Attributes:
        message: Human-readable description of the missing permission.
        service: Service key that encountered the issue.
        action: The denied AWS API action, or None if unknown.
        timestamp: ISO-8601 UTC timestamp.
    """

    message: str
    service: str
    action: str | None
    timestamp: str


# -- Findings -----------------------------------------------------------------


@dataclass(frozen=True)
class SourceBlock:
    """A named bucket of recommendations from a single check source.

    Attributes:
        count: Number of recommendations in this source.
        recommendations: Immutable tuple of recommendation dicts.
        extras: Optional additional key-value metadata.
    """

    count: int
    recommendations: tuple[dict[str, Any], ...]
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatCardSpec:
    """Declaration for a summary stat card in the HTML report.

    Attributes:
        label: Display label for the card.
        source_path: Dot-separated path into the findings dict.
        formatter: Display format for the card value.
    """

    label: str
    source_path: str
    formatter: Literal["int", "currency", "percent", "size_gb"] = "int"


@dataclass(frozen=True)
class GroupingSpec:
    """Forward-looking: defines how recommendations should be grouped in the report.

    Not yet consumed by the rendering pipeline. Will be integrated when the
    reporter supports configurable grouping modes (by check_category, resource_type,
    or source) instead of the current hardcoded per-service dispatch.
    """

    by: Literal["check_category", "resource_type", "source"]
    label_path: str | None = None


@dataclass(frozen=True)
class Group:
    """Forward-looking: a named group of recommendations with aggregated savings.

    Companion to GroupingSpec — not yet consumed by the rendering pipeline.
    """

    label: str
    recommendations: tuple[dict[str, Any], ...]
    aggregated_savings: float = 0.0


@dataclass(frozen=True)
class ServiceFindings:
    """Typed result container returned by every ServiceModule.scan() call.

    Attributes:
        service_name: Human-readable display name of the service.
        total_recommendations: Number of optimization recommendations found.
        total_monthly_savings: Estimated monthly savings in USD.
        sources: Named check-source buckets keyed by source name.
        optimization_descriptions: Optional per-check description metadata.
        extras: Additional service-specific metadata.
        total_count: Total resource count (omitted from output when zero).
        schema_version: Contract schema version for forward compatibility.
    """

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
    """Interface every service adapter must implement.

    Attributes:
        key: Unique machine-readable identifier for the service.
        cli_aliases: CLI tokens accepted by --scan-only / --skip-service.
        display_name: Human-readable name for reports.
        stat_cards: Stat card declarations for the HTML report.
        grouping: Default grouping specification, or None.
        requires_cloudwatch: Whether the adapter needs CloudWatch metrics.
        reads_fast_mode: Whether the adapter respects the --fast flag.
        custom_grouping: Optional callable for custom recommendation grouping.
    """

    key: str
    cli_aliases: tuple[str, ...]
    display_name: str
    stat_cards: tuple[StatCardSpec, ...]
    grouping: GroupingSpec | None
    requires_cloudwatch: bool
    reads_fast_mode: bool

    def required_clients(self) -> tuple[str, ...]:
        """Return boto3 client names this adapter needs."""
        ...

    def scan(self, ctx: Any) -> ServiceFindings:
        """Execute the scan and return typed findings."""
        ...

    custom_grouping: Callable[[ServiceFindings], list[Group]] | None
