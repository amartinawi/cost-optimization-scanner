"""CloudFront adapter: one counted flat-fee lever, everything else advisory."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.cloudfront import get_enhanced_cloudfront_checks

# The single CloudFront lever with a flat, exact, account-specific dollar.
_COUNTED_CATEGORY = "CloudFront Dedicated IP SSL"


class CloudfrontModule(BaseServiceModule):
    """ServiceModule adapter for CloudFront.

    Every traffic-shaped lever is a $0 advisory (quantifying it needs the
    CloudWatch BytesDownloaded metric + per-distribution PriceClass), but the
    dedicated-IP custom SSL fee is a flat, exact, account-specific charge, so
    it counts (CF-4).
    """

    key: str = "cloudfront"
    cli_aliases: tuple[str, ...] = ("cloudfront",)
    display_name: str = "CloudFront"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for CloudFront scanning."""
        return ("cloudfront",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan CloudFront distributions for cost optimization opportunities.

        Consults the cloudfront service module for price class optimization,
        disabled distributions, origin shield review, and the dedicated-IP
        custom SSL fee. The traffic-shaped levers emit $0 advisories with a
        PricingWarning — honest data-transfer savings need the CloudWatch
        BytesDownloaded metric + per-distribution PriceClass. The dedicated-IP
        SSL fee is flat and exact, so it is counted.

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
            # CF-1 — an advisory-only lever must SAY so per rec: without an
            # explicit Counted=False every $0 card counts as a recommendation
            # (B1 / invariant sweep #3). CF-2 — the shim's percentage strings
            # ("20-50% on data transfer...") must not survive on a $0 advisory
            # (B2: string and numeric agree in every branch).
            #
            # CF-4 is the one exception, and the demotion must be a per-lever
            # decision rather than a blanket sweep: the dedicated-IP SSL fee is
            # a flat published rate the account is definitely paying, already
            # carrying its own AuditBasis and PricingWarning. Blanket-zeroing it
            # here would delete a real, exact dollar.
            if rec.get("CheckCategory") == _COUNTED_CATEGORY:
                continue
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

        counted = [r for r in recs if r.get("Counted", True)]
        return ServiceFindings(
            service_name="CloudFront",
            total_recommendations=len(counted),
            total_monthly_savings=round(
                sum(float(r.get("EstimatedMonthlySavings", 0.0)) for r in counted), 2
            ),
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions={
                "enhanced_checks": {
                    "title": "CloudFront Distribution Optimization",
                    "description": (
                        "Price class optimization for traffic patterns, disabled distribution"
                        " detection, origin shield configuration review, and dedicated-IP"
                        " custom SSL certificate fees."
                    ),
                    "action": (
                        "1. Switch dedicated-IP certificates to SNI where viewers allow\n"
                        "2. Review price class settings for each distribution\n"
                        "3. Delete or disable unused distributions\n"
                        "4. Enable Origin Shield where applicable\n"
                        "5. Estimated savings: varies by traffic volume"
                    ),
                },
            },
        )
