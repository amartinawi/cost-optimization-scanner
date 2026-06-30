"""Athena adapter with CloudWatch ProcessedBytes pricing."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.athena import ATHENA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_athena_checks

logger = logging.getLogger(__name__)

# Athena per-TB scan rate (us-east-1, verified $5/TB AWS list).
# When CW ProcessedBytes is unavailable we emit 0 + PricingWarning
# rather than the previous $50 fabricated fallback.


class AthenaModule(BaseServiceModule):
    """ServiceModule adapter for Athena. CloudWatch ProcessedBytes pricing."""

    key: str = "athena"
    cli_aliases: tuple[str, ...] = ("athena",)
    display_name: str = "Athena"
    reads_fast_mode: bool = True
    requires_cloudwatch: bool = True  # adapter consults CW ProcessedBytes metric.

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Athena scanning."""
        return ("athena", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        result = get_enhanced_athena_checks(ctx)
        recs = result.get("recommendations", [])

        ATHENA_PRICE_PER_TB = 5.0

        savings = 0.0
        for rec in recs:
            if not ctx.fast_mode:
                workgroup = rec.get("WorkGroup", "primary")
                monthly_tb = rec.get("ProcessedBytesTB", 0)

                if monthly_tb <= 0:
                    try:
                        cw = ctx.client("cloudwatch")
                        from datetime import datetime, timedelta, timezone

                        end = datetime.now(timezone.utc)
                        start = end - timedelta(days=30)
                        resp = cw.get_metric_statistics(
                            Namespace="AWS/Athena",
                            MetricName="ProcessedBytes",
                            Dimensions=[{"Name": "WorkGroup", "Value": workgroup}],
                            StartTime=start,
                            EndTime=end,
                            Period=2592000,
                            Statistics=["Sum"],
                        )
                        total_bytes = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
                        # AWS bills Athena per TB = 10^12 bytes (H2: the previous
                        # 1024**4 / TiB divisor understated scans by ~9.05%).
                        monthly_tb = total_bytes / 1e12 if total_bytes > 0 else 0
                    except Exception as e:
                        logger.warning(f"[athena] CloudWatch ProcessedBytes metric check failed: {e}")
                        monthly_tb = 0

                if monthly_tb > 0:
                    # 75% savings factor based on AWS benchmarks showing
                    # 70-91% compression for columnar formats (Parquet/ORC).
                    rec_savings = monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75
                    rec["EstimatedMonthlySavings"] = round(rec_savings, 2)
                    # Replace the generic "Up to 75%" placeholder with the measured
                    # per-workgroup dollar so the string and the numeric agree. The
                    # base-scan-cost figure is region-scaled too (× pricing_multiplier)
                    # so it matches the savings basis in non-US regions.
                    scanned_cost = monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier
                    rec["EstimatedSavings"] = (
                        f"${round(rec_savings, 2):.2f}/month — partition/compress to "
                        f"Parquet/ORC (~75% of ${scanned_cost:.2f}/mo scanned)"
                    )
                    savings += rec_savings
                else:
                    # CW returned no data; emit a $0 advisory (Counted=False) so
                    # the metric-gap zero is not mistaken for a genuine "no
                    # savings" and is not summed/counted (live-audit H5).
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["Counted"] = False
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: up to 75% scan-cost reduction once "
                        "CW ProcessedBytes is measured"
                    )
                    rec["PricingWarning"] = "CW ProcessedBytes metric returned no data"
            else:
                # fast_mode: skip CW lookup. Emit a $0 advisory so a full scan
                # re-runs the metric query (live-audit H5).
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: up to 75% scan-cost reduction "
                    "(fast mode skipped CW; re-run without --fast)"
                )
                rec["PricingWarning"] = "fast mode skipped CW; re-run without --fast"

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        # Count hygiene: $0 advisory recs render but are excluded from the
        # rec-count headline (mirror lambda / batch / msk).
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="Athena",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=ATHENA_OPTIMIZATION_DESCRIPTIONS,
        )
