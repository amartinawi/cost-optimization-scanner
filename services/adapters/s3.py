"""Multi-source adapter for S3 with bucket analysis and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import parse_dollar_savings
from services.s3 import (
    S3_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_s3_checks,
    get_s3_bucket_analysis,
)

logger = logging.getLogger(__name__)

# Enhanced-checks categories that overlap with the s3_bucket_analysis source.
# These records exist for visibility only; the dollars are counted by
# bucket_analysis. Filtered out of `total_recommendations` to keep counts
# honest (audit L2-S3-002 + L3-S3-002).
_DEDICATED_CATEGORIES: frozenset[str] = frozenset({
    "Storage Class Optimization",
    "Static Website Optimization",
})


class S3Module(BaseServiceModule):
    """ServiceModule adapter for S3. Multi-source savings strategy."""

    key: str = "s3"
    cli_aliases: tuple[str, ...] = ("s3",)
    display_name: str = "S3"
    # Full-mode bucket-size estimation queries CloudWatch BucketSizeBytes per
    # bucket × 6 storage classes. Honest declaration so the orchestrator can
    # decide pre-fetch and so --no-cloudwatch can opt out (audit L1-S3-003).
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for S3 scanning."""
        return ("s3", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan S3 buckets for lifecycle, Intelligent-Tiering, and storage optimization.

        Per-source savings:

        - **s3_bucket_analysis**: dedicated dollar source. Each bucket's
          ``SavingsDelta`` is derived from a per-opportunity factor
          (``S3_SAVINGS_FACTORS``) applied to its CloudWatch-derived monthly
          storage cost — replaces the legacy blanket × 0.40 multiplier.
        - **enhanced_checks**: config-pattern flags (multipart uploads,
          versioning, replication, server-access logs, empty buckets).
          Every record carries a parseable ``EstimatedSavings`` string; the
          informational ``$0.00/month - <reason>`` form means the bucket-level
          dollars are counted by ``s3_bucket_analysis``, not here. Categories
          overlapping with bucket_analysis (``Storage Class Optimization``,
          ``Static Website Optimization``) are filtered out of
          ``total_recommendations`` to avoid double-counting.
        """
        logger.debug("S3 adapter scan starting")

        s3_data = get_s3_bucket_analysis(ctx, ctx.fast_mode, ctx.pricing_multiplier)
        enhanced_result = get_enhanced_s3_checks(ctx, ctx.pricing_multiplier)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = s3_data.get("optimization_opportunities", [])
        other_recs = [
            r for r in enhanced_recs
            if r.get("CheckCategory") not in _DEDICATED_CATEGORIES
        ]

        # Dedicated source — real per-opportunity dollars, no arbitrary
        # factor fallback (audit L2-S3-001).
        savings = sum(float(rec.get("SavingsDelta", 0.0) or 0.0) for rec in opt_opps)
        # Enhanced checks contribute only via parseable EstimatedSavings; the
        # informational $0.00/month form parses to 0.0 (audit L2-S3-002).
        savings += sum(
            parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in other_recs
        )

        total_recs = len(opt_opps) + len(other_recs)

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
                "enhanced_checks": SourceBlock(
                    count=len(other_recs),
                    recommendations=tuple(other_recs),
                ),
            },
            optimization_descriptions=S3_OPTIMIZATION_DESCRIPTIONS,
            extras={
                "bucket_counts": {
                    "total": s3_data.get("total_buckets", 0),
                    "without_lifecycle": len(s3_data.get("buckets_without_lifecycle", [])),
                    "without_intelligent_tiering": len(
                        s3_data.get("buckets_without_intelligent_tiering", [])
                    ),
                }
            },
        )
