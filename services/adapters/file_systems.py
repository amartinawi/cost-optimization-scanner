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

# Per-opportunity savings factors (AWS-documented midpoints).
# Lifecycle Standard→IA: $0.30→$0.025 ≈ 92% delta; conservative midpoint 0.50
# applied to file systems WITH IA policy already enabled (the lifecycle delta
# benefits files that age into IA). For NO-IA file systems the absolute
# saving is larger but actual savings depend on access patterns.
FS_SAVINGS_FACTORS: dict[str, float] = {
    "lifecycle": 0.50,
    "archive": 0.85,        # Standard→Archive delta ≈ 97%, midpoint 0.85
    "one_zone": 0.47,       # AWS-documented 47% Regional→One Zone
    "idle_efs": 1.00,
    "throughput": 0.30,     # 20-50% Elastic Throughput midpoint
    "fsx_rightsize": 0.30,
    "fsx_intelligent_tiering": 0.40,
    "fsx_dedup": 0.50,      # 30-80% Windows dedup midpoint
    "fsx_single_az": 0.50,
    "fsx_backup_retention": 0.30,
    "default": 0.30,
}


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

        # EFS lifecycle savings (file systems WITHOUT lifecycle policy).
        # `EstimatedMonthlyCost` from the shim was computed with the shim's
        # _estimate_efs_cost helper which already applies pricing_multiplier;
        # do NOT re-multiply here.
        for rec in efs_recs:
            cost = rec.get("EstimatedMonthlyCost", 0)
            if ctx.pricing_engine is not None and cost == 0:
                size_gb = rec.get("SizeGB", rec.get("StorageCapacity", 0))
                if size_gb > 0:
                    # PricingEngine returns region-correct $/GB; no multiplier.
                    price = ctx.pricing_engine.get_efs_monthly_price_per_gb()
                    cost = size_gb * price
            # Lifecycle saves the access-pattern-tiered delta; conservative
            # midpoint factor applied per L2.3.x.
            savings += cost * FS_SAVINGS_FACTORS["lifecycle"]

        # FSx: `cost` from the shim's _estimate_fsx_cost is ALREADY multiplied
        # by pricing_multiplier internally (services/efs_fsx.py:116). The
        # previous adapter then multiplied again — double-application bug.
        for rec in fsx_recs:
            cost = rec.get("EstimatedMonthlyCost", 0)
            savings += cost * FS_SAVINGS_FACTORS["fsx_rightsize"]

        # Enhanced checks now read a numeric field set by the shim if present;
        # fall back to 0 if the rec is informational-only.
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
