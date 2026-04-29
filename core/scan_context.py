"""Scan execution context: region, account, warnings, permission issues.

Aggregates a ClientRegistry with structured warning/permission tracking,
replacing inline dicts in the monolith's add_warning / add_permission_issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.client_registry import ClientRegistry
from core.contracts import PermissionIssueRecord, WarningRecord


@dataclass
class ScanContext:
    region: str
    account_id: str
    profile: str | None
    fast_mode: bool
    clients: ClientRegistry
    _warnings: list[WarningRecord] = field(default_factory=list)
    _permission_issues: list[PermissionIssueRecord] = field(default_factory=list)

    def warn(self, message: str, service: str = "") -> None:
        rec = WarningRecord(
            message=message,
            service=service,
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._warnings.append(rec)
        print(f"\u26a0\ufe0f Warning: {message}")

    def permission_issue(self, message: str, service: str, action: str | None = None) -> None:
        rec = PermissionIssueRecord(
            message=message,
            service=service,
            action=action,
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._permission_issues.append(rec)
        print(f"\U0001f512 Permission Issue ({service}): {message}")

    def client(self, name: str, region: str | None = None) -> Any:
        return self.clients.client(name, region)
