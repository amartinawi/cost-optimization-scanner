"""MediaStore adapter with S3-equivalent storage pricing."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.mediastore import MEDIASTORE_OPTIMIZATION_DESCRIPTIONS, get_enhanced_mediastore_checks


class MediastoreModule(BaseServiceModule):
    """ServiceModule adapter for MediaStore. S3-equivalent storage pricing."""

    key: str = "mediastore"
    cli_aliases: tuple[str, ...] = ("mediastore",)
    display_name: str = "MediaStore"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for MediaStore scanning."""
        return ("mediastore", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan MediaStore containers for cost optimization opportunities.

        Consults enhanced MediaStore checks. Savings use S3-equivalent
        storage pricing per GB when available, flat-rate fallback otherwise.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        result = get_enhanced_mediastore_checks(ctx)
        source_recs = result.get("recommendations", [])

        if ctx.pricing_engine:
            # PricingEngine returns region-correct $/GB; no multiplier.
            price_per_gb = ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")
            rate_source = "S3 STANDARD via PricingEngine (region-correct)"
        else:
            # Module-const fallback path → apply multiplier (L2.3.2).
            price_per_gb = 0.023 * ctx.pricing_multiplier
            rate_source = f"$0.023/GB-Mo S3 STANDARD fallback x{ctx.pricing_multiplier} multiplier"

        recs: list[dict[str, Any]] = []
        savings = 0.0
        for src in source_recs:
            # Immutability: build a NEW rec; never mutate the shim's dict.
            rec = dict(src)
            estimated_gb = rec.get("EstimatedStorageGB", 0) or 0
            if estimated_gb > 0:
                rec_savings = round(estimated_gb * price_per_gb, 2)
                rec["EstimatedMonthlySavings"] = rec_savings
                # Each counted dollar carries a defensible AuditBasis.
                rec["AuditBasis"] = {
                    "rate": f"${price_per_gb:.4f}/GB-Mo",
                    "region": getattr(ctx, "region", "us-east-1"),
                    "metric_window": "BucketSizeBytes 14-day Average (AWS/MediaStore)",
                    "formula": (
                        f"{estimated_gb:.2f} GB x ${price_per_gb:.4f}/GB-Mo "
                        f"= ${rec_savings:.2f}/mo"
                    ),
                    "rate_source": rate_source,
                }
                savings += rec_savings
            else:
                # No storage figure → cannot quantify a real dollar. Demote to a
                # $0 advisory (Counted=False) so the card still renders but is
                # excluded from BOTH the dollar total AND total_recommendations
                # (mediastore H1 count hygiene) — never fabricate a saving.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: requires EstimatedStorageGB for quantified savings"
                )
                rec["PricingWarning"] = "requires EstimatedStorageGB for quantified savings"
            recs.append(rec)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        # Count hygiene: a $0 advisory (Counted=False) renders but must not
        # inflate the rec headline (mirrors services/_savings.mark_zero_savings_advisory).
        counted = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="MediaStore",
            total_recommendations=counted,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MEDIASTORE_OPTIMIZATION_DESCRIPTIONS,
        )
