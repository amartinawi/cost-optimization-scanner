"""Multi-source adapter for EFS and FSx file system optimization.

All findings render under the single File Systems tab. Counted findings carry a
concrete dollar saving (real price delta or measured storage); advisory findings
are best-practice opportunities with no account-specific dollar figure and are
NOT counted toward the tab's savings or recommendation total.

EFS/FSx are not covered by AWS Cost Optimization Hub or Compute Optimizer, so
there is no CoH/CO source to consume — every number is derived locally.
"""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.efs_fsx import (
    get_efs_file_system_count,
    get_efs_findings,
    get_file_system_optimization_descriptions,
    get_fsx_file_system_count,
    get_fsx_findings,
)
from services.file_systems_logic import dedupe_counted


class FileSystemsModule(BaseServiceModule):
    """ServiceModule adapter for file systems (EFS, FSx)."""

    key: str = "file_systems"
    cli_aliases: tuple[str, ...] = ("efs", "fsx", "file_systems")
    display_name: str = "File Systems"
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for file system scanning."""
        return ("efs", "fsx", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EFS and FSx file systems for cost optimization opportunities.

        Returns:
            ServiceFindings with ``efs_lifecycle_analysis`` (counted EFS),
            ``fsx_optimization_analysis`` (counted FSx), and ``advisory``
            (uncounted best-practice) SourceBlock entries — all rendered under
            the single File Systems tab.
        """
        efs_counts = get_efs_file_system_count(ctx)
        fsx_counts = get_fsx_file_system_count(ctx)

        efs = get_efs_findings(ctx, ctx.pricing_multiplier, getattr(ctx, "fast_mode", False))
        fsx = get_fsx_findings(ctx, ctx.pricing_multiplier)

        # One counted finding per file system (highest saving wins) — never stack
        # idle + lifecycle + one-zone on the same EFS beyond 100% of its cost.
        efs_counted = dedupe_counted(efs["counted"])
        fsx_counted = dedupe_counted(fsx["counted"])
        advisory = list(efs["advisory"]) + list(fsx["advisory"])

        # file_systems L4: sum the full-precision ``_savings`` float the counted
        # recs carry (the same field the renderer reads), not a re-parse of the
        # rounded EstimatedSavings display string — the two diverged by rounding.
        savings = sum(float(r.get("_savings", 0.0) or 0.0) for r in efs_counted)
        savings += sum(float(r.get("_savings", 0.0) or 0.0) for r in fsx_counted)

        total_recs = len(efs_counted) + len(fsx_counted)

        return ServiceFindings(
            service_name="File Systems",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "efs_lifecycle_analysis": SourceBlock(count=len(efs_counted), recommendations=tuple(efs_counted)),
                "fsx_optimization_analysis": SourceBlock(count=len(fsx_counted), recommendations=tuple(fsx_counted)),
                # Uncounted: best-practice opportunities with no account-specific $.
                "advisory": SourceBlock(count=len(advisory), recommendations=tuple(advisory)),
            },
            optimization_descriptions=get_file_system_optimization_descriptions(),
            extras={"efs_counts": efs_counts, "fsx_counts": fsx_counts},
        )
