"""Scan execution context: region, account, warnings, permission issues.

Aggregates a ClientRegistry with structured warning/permission tracking,
replacing inline dicts in the monolith's add_warning / add_permission_issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.pricing_engine import PricingEngine

from core.client_registry import ClientRegistry
from core.contracts import PermissionIssueRecord, WarningRecord


@dataclass
class ScanContext:
    """Mutable execution context shared across all service adapters.

    Attributes:
        region: AWS region being scanned.
        account_id: AWS account ID (resolved via STS).
        profile: Optional AWS CLI profile name.
        fast_mode: Whether --fast mode is enabled.
        clients: Caching boto3 client registry.
        pricing_multiplier: Regional pricing multiplier.
        old_snapshot_days: Age threshold for old snapshot checks.
        cost_hub_splits: Pre-fetched Cost Optimization Hub data keyed by service.
        _warnings: Accumulated non-fatal warnings.
        _permission_issues: Accumulated IAM permission gaps.
    """

    region: str
    account_id: str
    profile: str | None
    fast_mode: bool
    clients: ClientRegistry
    pricing_multiplier: float = 1.0
    pricing_engine: "PricingEngine | None" = field(default=None)
    old_snapshot_days: int = 90
    cost_hub_splits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _warnings: list[WarningRecord] = field(default_factory=list)
    _permission_issues: list[PermissionIssueRecord] = field(default_factory=list)

    def warn(self, message: str, service: str = "") -> None:
        """Record a non-fatal warning and print it to stdout."""
        rec = WarningRecord(
            message=message,
            service=service,
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._warnings.append(rec)
        print(f"\u26a0\ufe0f Warning: {message}")

    def permission_issue(self, message: str, service: str, action: str | None = None) -> None:
        """Record an IAM permission gap and print it to stdout."""
        rec = PermissionIssueRecord(
            message=message,
            service=service,
            action=action,
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._permission_issues.append(rec)
        print(f"\U0001f512 Permission Issue ({service}): {message}")

    def client(self, name: str, region: str | None = None) -> Any:
        """Return a cached boto3 client for the given service."""
        return self.clients.client(name, region)
