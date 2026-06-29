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

# F2 — render-noise floor for the bucket_analysis source. A bucket with no counted
# saving and less than this much Standard storage has negligible optimization
# potential (e.g. a 4 MB bucket flagged "no lifecycle configured"). Such advisory
# cards are suppressed from the rendered source — they stay in the bucket_counts
# summary stats and the suppressed tally — while every counted bucket and every
# advisory bucket at or above the floor is still rendered.
_ADVISORY_RENDER_MIN_GB: float = 1.0


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
          ``SavingsDelta`` is evidence-gated — the real Standard→Standard-IA
          rate delta on its measured Standard bytes, credited only when request
          metrics show the data is cold (audit S3-A/S3-B). Buckets with a config
          gap but no access evidence are $0 advisories.
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
        # F1 — tag each enhanced rec with the standard Counted flag (True only when
        # its EstimatedSavings parses to a positive dollar) so the reporter and any
        # count-hygiene consumer treat the $0 informational rows as advisory, not
        # counted (matches the bucket_analysis source and every other adapter).
        other_recs = [
            dict(r, Counted=(parse_dollar_savings(r.get("EstimatedSavings", "")) > 0))
            for r in enhanced_recs
            if r.get("CheckCategory") not in _DEDICATED_CATEGORIES
        ]

        # Dedicated source — real evidence-gated dollars (audit S3-A/S3-B).
        savings = sum(float(rec.get("SavingsDelta", 0.0) or 0.0) for rec in opt_opps)
        # Enhanced checks contribute only via parseable EstimatedSavings; the
        # informational $0.00/month form parses to 0.0 (audit L2-S3-002).
        savings += sum(
            parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in other_recs
        )

        # Count hygiene (audit S3-C): only recommendations that carry a concrete
        # dollar saving count toward total_recommendations. Advisory/visibility
        # records (static-website, no-evidence gaps, $0 enhanced checks) remain
        # in the source blocks for the report but must not inflate the headline.
        savings_bearing_buckets = sum(
            1 for rec in opt_opps if float(rec.get("SavingsDelta", 0.0) or 0.0) > 0
        )
        savings_bearing_enhanced = sum(
            1 for rec in other_recs if parse_dollar_savings(rec.get("EstimatedSavings", "")) > 0
        )
        total_recs = savings_bearing_buckets + savings_bearing_enhanced
        advisory_count = (len(opt_opps) - savings_bearing_buckets) + (
            len(other_recs) - savings_bearing_enhanced
        )

        # F2 render-noise floor: drop sub-threshold $0 advisory buckets from the
        # rendered cards (they remain in the bucket_counts summary + the suppressed
        # tally). Counted buckets and advisory buckets >= the floor always render.
        def _render_bucket(rec: dict[str, Any]) -> bool:
            if float(rec.get("SavingsDelta", 0.0) or 0.0) > 0:
                return True
            return float(rec.get("SizeGB", 0.0) or 0.0) >= _ADVISORY_RENDER_MIN_GB

        rendered_opps = [r for r in opt_opps if _render_bucket(r)]
        suppressed_small = len(opt_opps) - len(rendered_opps)

        return ServiceFindings(
            service_name="S3",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "s3_bucket_analysis": SourceBlock(
                    count=len(rendered_opps),
                    recommendations=tuple(rendered_opps),
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
                },
                # Records shown for visibility but excluded from
                # total_recommendations because they carry no concrete $ saving
                # (audit S3-C).
                "advisory_count": advisory_count,
                # F2 — count of sub-threshold $0 advisory buckets not individually
                # rendered (still reflected in the bucket_counts above).
                "suppressed_small_advisory_buckets": suppressed_small,
                "advisory_render_floor_gb": _ADVISORY_RENDER_MIN_GB,
            },
        )
