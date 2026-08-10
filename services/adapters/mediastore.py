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

        # MS-1 — the counted branch that used to sit here priced
        # `EstimatedStorageGB` against an S3 rate. That field came from an
        # `AWS/MediaStore BucketSizeBytes` read, a metric MediaStore does not
        # publish, so the field was always 0 and the branch was unreachable on
        # every account since it was written. It is deleted rather than repaired:
        # MediaStore exposes no storage-size metric and `Container` carries no
        # size field, so the QUANTITY half of rate x quantity is unobtainable at
        # scan time — and a rate with no quantity is not a saving.
        #
        # Deleting it also removes MS-3 (an engine-priced string paired with a
        # fallback-priced numeric) and MS-4 (`Datapoints[-1]`, an ordering
        # CloudWatch does not guarantee) by construction. This tab's
        # total_monthly_savings is now a structural 0.0. Blast radius on existing
        # reports is zero: the branch never fired.
        recs: list[dict[str, Any]] = []
        savings = 0.0
        for src in source_recs:
            # Immutability: build a NEW rec; never mutate the shim's dict.
            rec = dict(src)
            rec.setdefault("EstimatedMonthlySavings", 0.0)
            rec.setdefault("Counted", False)
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
