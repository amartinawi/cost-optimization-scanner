"""Multi-source adapter for EFS and FSx file system optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.efs_fsx import (
    get_efs_file_system_count,
    get_efs_lifecycle_analysis,
    get_enhanced_efs_fsx_checks,
    get_file_system_optimization_descriptions,
    get_fsx_file_system_count,
    get_fsx_optimization_analysis,
)


class FileSystemsModule(BaseServiceModule):
    key: str = "file_systems"
    cli_aliases: tuple[str, ...] = ("efs", "file_systems")
    display_name: str = "File Systems"

    def required_clients(self) -> tuple[str, ...]:
        return ("efs", "fsx")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/file_systems.py] File Systems module active")

        efs_counts = get_efs_file_system_count(ctx)
        fsx_counts = get_fsx_file_system_count(ctx)

        efs_lifecycle_recs = get_efs_lifecycle_analysis(ctx, ctx.pricing_multiplier)
        fsx_optimization_recs = get_fsx_optimization_analysis(ctx, ctx.pricing_multiplier)
        enhanced_result = get_enhanced_efs_fsx_checks(ctx, ctx.pricing_multiplier)

        efs_recs = (
            efs_lifecycle_recs
            if isinstance(efs_lifecycle_recs, list)
            else efs_lifecycle_recs.get("recommendations", [])
        )
        fsx_recs = (
            fsx_optimization_recs
            if isinstance(fsx_optimization_recs, list)
            else fsx_optimization_recs.get("recommendations", [])
        )
        enhanced_recs = enhanced_result.get("recommendations", [])

        savings = sum(rec.get("EstimatedMonthlyCost", 0) * 0.3 for rec in efs_recs + fsx_recs)

        total_recs = len(efs_recs) + len(fsx_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="File Systems",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "efs_lifecycle_analysis": SourceBlock(count=len(efs_recs), recommendations=tuple(efs_recs)),
                "fsx_optimization_analysis": SourceBlock(count=len(fsx_recs), recommendations=tuple(fsx_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=get_file_system_optimization_descriptions(),
            extras={"efs_counts": efs_counts, "fsx_counts": fsx_counts},
        )
