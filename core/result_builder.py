from __future__ import annotations

from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

from core.contracts import ServiceFindings
from core.scan_context import ScanContext


class ScanResultBuilder:
    def __init__(self, ctx: ScanContext) -> None:
        self.ctx = ctx

    def build(self, findings: dict[str, ServiceFindings]) -> dict[str, Any]:
        return {
            "account_id": self.ctx.account_id,
            "region": self.ctx.region,
            "profile": self.ctx.profile,
            "scan_time": datetime.now(UTC).isoformat(),
            "scan_warnings": [asdict(w) for w in self.ctx._warnings],
            "permission_issues": [asdict(p) for p in self.ctx._permission_issues],
            "services": {k: self._serialize(f) for k, f in findings.items()},
            "summary": self._summary(findings),
        }

    @staticmethod
    def _serialize(f: ServiceFindings) -> dict[str, Any]:
        extras: dict[str, Any] = dict(f.extras) if f.extras else {}
        base: dict[str, Any] = {}
        for fld in fields(f):
            if fld.name in ("extras", "schema_version"):
                continue
            val = getattr(f, fld.name)
            if fld.name == "total_count" and val == 0:
                continue
            base[fld.name] = val
        return {**base, **extras}

    @staticmethod
    def _summary(findings: dict[str, ServiceFindings]) -> dict[str, Any]:
        scanned = sum(1 for f in findings.values() if f.total_recommendations > 0 or f.total_count > 0)
        return {
            "total_services_scanned": scanned,
            "total_recommendations": sum(f.total_recommendations for f in findings.values()),
            "total_monthly_savings": sum(f.total_monthly_savings for f in findings.values()),
        }
