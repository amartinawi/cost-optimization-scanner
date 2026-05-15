"""Flat-rate adapter for CloudFront."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.cloudfront import get_enhanced_cloudfront_checks


class CloudfrontModule(BaseServiceModule):
    """ServiceModule adapter for CloudFront. Flat-rate savings strategy."""

    key: str = "cloudfront"
    cli_aliases: tuple[str, ...] = ("cloudfront",)
    display_name: str = "CloudFront"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for CloudFront scanning."""
        return ("cloudfront",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan CloudFront distributions for cost optimization opportunities.

        Consults the cloudfront service module for price class optimization,
        disabled distributions, and origin shield review. Traffic-based savings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
        print("\U0001f50d [services/adapters/cloudfront.py] CloudFront module active")
        try:
            result = get_enhanced_cloudfront_checks(ctx)
        except Exception as e:
            ctx.warn(f"enhanced checks failed: {e}", "cloudfront")
            result = {}
        recs = result.get("recommendations", [])

        # CloudFront data-transfer-out pricing is tiered + regional ($0.085/GB
        # US/EU first 10TB, $0.080 next 40TB; $0.120 Asia tier-1; etc.). The
        # previous adapter used a flat $0.10/GB AND a fictional 0.5 KB/request
        # size assumption that produced numbers detached from reality.
        # Without per-rec PriceClass + measured bytes via the CloudFront
        # CW `BytesDownloaded` metric we cannot quantify honestly. Emit
        # 0 + PricingWarning so the recs surface for human review.
        savings = 0.0
        for rec in recs:
            weekly_requests_str = rec.get("WeeklyRequests", "")
            try:
                weekly_requests = float(weekly_requests_str)
            except (ValueError, TypeError):
                weekly_requests = 0.0
            if rec.get("CheckCategory") == "CloudFront Unused Distribution":
                # Unused distribution: $0 saving until traffic resumes.
                rec["EstimatedMonthlySavings"] = 0.0
                continue
            rec["EstimatedMonthlySavings"] = 0.0
            rec["PricingWarning"] = (
                "requires CW BytesDownloaded metric and distribution PriceClass "
                "for quantified savings"
            )
            _ = weekly_requests  # documented; not used in current honest path.

        savings = round(savings, 2)

        return ServiceFindings(
            service_name="CloudFront",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions={
                "enhanced_checks": {
                    "title": "CloudFront Distribution Optimization",
                    "description": (
                        "Price class optimization for traffic patterns, disabled distribution"
                        " detection, and origin shield configuration review."
                    ),
                    "action": (
                        "1. Review price class settings for each distribution\n"
                        "2. Delete or disable unused distributions\n"
                        "3. Enable Origin Shield where applicable\n"
                        "4. Estimated savings: varies by traffic volume"
                    ),
                },
            },
        )
