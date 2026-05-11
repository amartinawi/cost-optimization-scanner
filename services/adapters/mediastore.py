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
        print("\U0001f50d [services/adapters/mediastore.py] MediaStore module active")
        result = get_enhanced_mediastore_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        for rec in recs:
            estimated_gb = rec.get("EstimatedStorageGB", 0)
            if ctx.pricing_engine:
                price_per_gb = ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")
            else:
                price_per_gb = 0.023 * ctx.pricing_multiplier
            if estimated_gb > 0:
                savings += estimated_gb * price_per_gb
            else:
                savings += 20.0 * ctx.pricing_multiplier

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="MediaStore",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MEDIASTORE_OPTIMIZATION_DESCRIPTIONS,
        )
