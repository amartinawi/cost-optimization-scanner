"""Flat-rate adapter for DMS."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dms import DMS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_dms_checks


class DmsModule(BaseServiceModule):
    """ServiceModule adapter for DMS. Flat-rate savings strategy."""

    key: str = "dms"
    cli_aliases: tuple[str, ...] = ("dms",)
    display_name: str = "DMS"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for DMS scanning."""
        return ("dms", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan DMS replication instances for cost optimization opportunities.

        Consults the dms service module for instance rightsizing and serverless
        configuration review. Savings calculated at a flat rate per recommendation.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        print("\U0001f50d [services/adapters/dms.py] DMS module active")
        result = get_enhanced_dms_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        for rec in recs:
            instance_class = rec.get("InstanceClass", "")
            if ctx.pricing_engine is not None and instance_class:
                # PricingEngine returns region-correct $/month; do NOT
                # re-multiply by ctx.pricing_multiplier (L2.3.1).
                monthly = ctx.pricing_engine.get_instance_monthly_price(
                    "AWSDatabaseMigrationSvc", instance_class.replace("dms.", "")
                )
                if monthly > 0:
                    savings += monthly * 0.35
                # else: pricing API miss; skip rather than fabricate $50 fallback.
            # else: instance_class unknown; skip rather than fabricate $50 fallback.

        multi_az_recs: list[dict[str, Any]] = []
        for rec in recs:
            if rec.get("MultiAZ"):
                instance_class = rec.get("InstanceClass", "unknown")
                tags = rec.get("Tags", [])
                name = rec.get("ReplicationInstanceIdentifier", "")
                tag_values = " ".join(str(t.get("Value", "")) for t in tags).lower()
                is_non_prod = any(
                    kw in name.lower() or kw in tag_values for kw in ("dev", "test", "staging", "sandbox", "nonprod")
                )
                if is_non_prod:
                    # Multi-AZ DMS doubles the instance hourly rate. Switching
                    # to Single-AZ saves 50% of the actual instance cost — use
                    # the live price for this class rather than a flat $50.
                    az_savings = 0.0
                    az_warning = None
                    if ctx.pricing_engine and instance_class != "unknown":
                        monthly = ctx.pricing_engine.get_instance_monthly_price(
                            "AWSDatabaseMigrationSvc", instance_class.replace("dms.", "")
                        )
                        if monthly > 0:
                            az_savings = monthly * 0.5
                    if az_savings == 0.0:
                        az_warning = "instance class pricing unavailable"
                    multi_az_recs.append(
                        {
                            "Resource": name,
                            "InstanceClass": instance_class,
                            "MultiAZ": True,
                            "Recommendation": "Switch Multi-AZ DMS instance to Single-AZ for dev/test",
                            "EstimatedSavings": (
                                f"${az_savings:.2f}/month (50% of instance cost)"
                                if az_savings > 0
                                else "~50% of instance cost (Multi-AZ doubles the price)"
                            ),
                            "EstimatedMonthlySavings": round(az_savings, 2),
                            **({"PricingWarning": az_warning} if az_warning else {}),
                            "CheckCategory": "DMS Multi-AZ in Non-Prod",
                        }
                    )
                    savings += az_savings

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}
        if multi_az_recs:
            sources["multi_az_review"] = SourceBlock(count=len(multi_az_recs), recommendations=tuple(multi_az_recs))

        total_count = len(recs) + len(multi_az_recs)

        return ServiceFindings(
            service_name="DMS",
            total_recommendations=total_count,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions={
                **DMS_OPTIMIZATION_DESCRIPTIONS,
                "multi_az_review": {
                    "title": "Multi-AZ in Non-Production",
                    "description": "Non-production DMS instances using Multi-AZ incur double the cost.",
                    "action": "Switch Multi-AZ instances to Single-AZ for dev/test environments",
                },
            },
        )
