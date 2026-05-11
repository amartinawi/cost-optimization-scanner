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
    """ServiceModule adapter for file systems (EFS, FSx). Composite savings strategy."""

    key: str = "file_systems"
    cli_aliases: tuple[str, ...] = ("efs", "file_systems")
    display_name: str = "File Systems"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for file system scanning."""
        return ("efs", "fsx")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EFS and FSx file systems for cost optimization opportunities.

        Consults EFS lifecycle analysis, FSx optimization analysis, and enhanced
        checks. Savings derived from 30% of estimated monthly cost per rec.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with "efs_lifecycle_analysis",
            "fsx_optimization_analysis", and "enhanced_checks" SourceBlock entries.
        """
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

        savings = 0.0
        for rec in efs_recs:
            cost = rec.get("EstimatedMonthlyCost", 0)
            if ctx.pricing_engine is not None and cost == 0:
                size_gb = rec.get("SizeGB", rec.get("StorageCapacity", 0))
                if size_gb > 0:
                    price = ctx.pricing_engine.get_efs_monthly_price_per_gb()
                    cost = size_gb * price
            savings += cost * 0.40
        for rec in fsx_recs:
            cost = rec.get("EstimatedMonthlyCost", 0)
            if ctx.pricing_multiplier:
                cost *= ctx.pricing_multiplier
            savings += cost * 0.40
        for rec in enhanced_recs:
            savings += rec.get("EstimatedMonthlySavings", rec.get("monthly_savings", 0))

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
