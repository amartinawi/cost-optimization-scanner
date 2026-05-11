"""Athena adapter with CloudWatch ProcessedBytes pricing."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.athena import ATHENA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_athena_checks

ATHENA_SCAN_FALLBACK_MONTHLY: float = 50.0


class AthenaModule(BaseServiceModule):
    """ServiceModule adapter for Athena. CloudWatch ProcessedBytes pricing."""

    key: str = "athena"
    cli_aliases: tuple[str, ...] = ("athena",)
    display_name: str = "Athena"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Athena scanning."""
        return ("athena",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/athena.py] Athena module active")
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
                        monthly_tb = total_bytes / (1024**4) if total_bytes > 0 else 0
                    except Exception as e:
                        print(f"Warning: [athena] CloudWatch ProcessedBytes metric check failed: {e}")
                        monthly_tb = 0

                if monthly_tb > 0:
                    # 75% savings factor based on AWS benchmarks showing 70-91% compression for columnar formats
                    savings += monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75
                else:
                    # Fallback $50 estimate when CloudWatch data unavailable
                    savings += ATHENA_SCAN_FALLBACK_MONTHLY * ctx.pricing_multiplier
            else:
                savings += ATHENA_SCAN_FALLBACK_MONTHLY * ctx.pricing_multiplier

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Athena",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=ATHENA_OPTIMIZATION_DESCRIPTIONS,
        )
