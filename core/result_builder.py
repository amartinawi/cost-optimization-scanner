"""JSON result serialization for scan output.

Transforms ServiceFindings dicts and ScanContext metadata into the
canonical JSON structure consumed by the HTML report generator.
"""

from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, datetime
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from core.scan_context import ScanContext


class ScanResultBuilder:
    """Builds the canonical JSON dict from scan context and service findings."""

    def __init__(self, ctx: ScanContext) -> None:
        """Initialise with the scan context used throughout the scan."""
        self.ctx = ctx

    def build(self, findings: dict[str, ServiceFindings]) -> dict[str, Any]:
        """Produce the full JSON-serialisable result including metadata and summary."""
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
    def _serialize_source(sb: SourceBlock) -> dict[str, Any]:
        """Convert a SourceBlock to a JSON-friendly dict."""
        base = {"count": sb.count, "recommendations": list(sb.recommendations)}
        if sb.extras:
            return {**base, **dict(sb.extras)}
        return base

    @staticmethod
    def _serialize(f: ServiceFindings) -> dict[str, Any]:
        """Convert ServiceFindings to a dict, merging extras over base fields."""
        extras: dict[str, Any] = dict(f.extras) if f.extras else {}
        base: dict[str, Any] = {}
        for fld in fields(f):
            if fld.name in ("extras", "schema_version"):
                continue
            val = getattr(f, fld.name)
            if fld.name == "total_count" and val == 0:
                continue
            if fld.name == "sources" and isinstance(val, dict):
                val = {
                    k: ScanResultBuilder._serialize_source(v) if isinstance(v, SourceBlock) else v
                    for k, v in val.items()
                }
            base[fld.name] = val
        result = {**base, **extras}
        if extras:
            result["extras"] = extras
        return result

    @staticmethod
    def _summary(findings: dict[str, ServiceFindings]) -> dict[str, Any]:
        """Compute aggregate totals across all service findings."""
        scanned = sum(1 for f in findings.values() if f.total_recommendations > 0 or f.total_count > 0)
        return {
            "total_services_scanned": scanned,
            "total_recommendations": sum(f.total_recommendations for f in findings.values()),
            "total_monthly_savings": sum(f.total_monthly_savings for f in findings.values()),
        }
