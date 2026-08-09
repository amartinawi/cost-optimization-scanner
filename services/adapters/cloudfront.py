"""Advisory-only adapter for CloudFront."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.cloudfront import get_enhanced_cloudfront_checks


class CloudfrontModule(BaseServiceModule):
    """ServiceModule adapter for CloudFront. Advisory-only ($0 + PricingWarning)."""

    key: str = "cloudfront"
    cli_aliases: tuple[str, ...] = ("cloudfront",)
    display_name: str = "CloudFront"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for CloudFront scanning."""
        return ("cloudfront",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan CloudFront distributions for cost optimization opportunities.

        Consults the cloudfront service module for price class optimization,
        disabled distributions, and origin shield review. Emits $0 advisories
        with a PricingWarning — honest data-transfer savings need the CloudWatch
        BytesDownloaded metric + per-distribution PriceClass.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
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
        for rec in recs:
            # CF-1 — an advisory-only adapter must SAY so per rec: without an
            # explicit Counted=False every $0 card counts as a recommendation
            # (B1 / invariant sweep #3), contradicting this class's own
            # docstring. CF-2 — the shim's percentage strings ("20-50% on data
            # transfer...") must not survive on a $0 advisory (B2: string and
            # numeric agree in every branch).
            rec["EstimatedMonthlySavings"] = 0.0
            rec["Counted"] = False
            rec["EstimatedSavings"] = (
                "$0.00/month — advisory: quantify with the CloudWatch "
                "BytesDownloaded metric + the distribution's PriceClass"
            )
            rec["PricingWarning"] = (
                "requires CW BytesDownloaded metric and distribution PriceClass "
                "for quantified savings"
            )

        return ServiceFindings(
            service_name="CloudFront",
            total_recommendations=sum(1 for r in recs if r.get("Counted") is not False),
            total_monthly_savings=0.0,
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
