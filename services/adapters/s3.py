"""Multi-source adapter for S3 with bucket analysis and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.s3 import S3_OPTIMIZATION_DESCRIPTIONS, get_enhanced_s3_checks, get_s3_bucket_analysis


class S3Module(BaseServiceModule):
    """ServiceModule adapter for S3. Multi-source savings strategy."""

    key: str = "s3"
    cli_aliases: tuple[str, ...] = ("s3",)
    display_name: str = "S3"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for S3 scanning."""
        return ("s3",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan S3 buckets for lifecycle, Intelligent-Tiering, and storage optimization.

        Consults S3 bucket analysis and enhanced S3 checks. Savings
        aggregated from bucket cost estimates.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with s3_bucket_analysis and enhanced_checks sources.
        """
        print("\U0001f50d [services/adapters/s3.py] S3 module active")

        s3_data = get_s3_bucket_analysis(ctx, ctx.fast_mode, ctx.pricing_multiplier)
        enhanced_result = get_enhanced_s3_checks(ctx, ctx.pricing_multiplier)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = s3_data.get("optimization_opportunities", [])
        savings = sum(rec.get("SavingsDelta", rec.get("EstimatedMonthlyCost", 0) * 0.40) for rec in opt_opps)
        savings += sum(rec.get("EstimatedMonthlySavings", rec.get("EstimatedSavings", 0)) for rec in enhanced_recs)

        total_recs = len(opt_opps) + len(enhanced_recs)

        return ServiceFindings(
            service_name="S3",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "s3_bucket_analysis": SourceBlock(
                    count=len(opt_opps),
                    recommendations=tuple(opt_opps),
                    extras={
                        "top_cost_buckets": s3_data.get("top_cost_buckets", []),
                        "top_size_buckets": s3_data.get("top_size_buckets", []),
                    },
                ),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=S3_OPTIMIZATION_DESCRIPTIONS,
            extras={
                "bucket_counts": {
                    "total": s3_data.get("total_buckets", 0),
                    "without_lifecycle": len(s3_data.get("buckets_without_lifecycle", [])),
                    "without_intelligent_tiering": len(s3_data.get("buckets_without_intelligent_tiering", [])),
                }
            },
        )
