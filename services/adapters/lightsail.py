"""Bundle-based pricing adapter for Lightsail."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.lightsail import (
    HOURS_PER_MONTH,
    LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS,
    LIGHTSAIL_UNUSED_STATIC_IP_HOURLY,
    get_enhanced_lightsail_checks,
    get_lightsail_bundle_cost,
)


class LightsailModule(BaseServiceModule):
    """ServiceModule adapter for Lightsail. Bundle-based savings strategy."""

    key: str = "lightsail"
    cli_aliases: tuple[str, ...] = ("lightsail",)
    display_name: str = "Lightsail"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Lightsail scanning."""
        return ("lightsail",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Lightsail instances and static IPs for cost optimization.

        Consults enhanced Lightsail checks. Savings calculated via bundle-based
        pricing when available, flat-rate heuristic as fallback.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        result = get_enhanced_lightsail_checks(ctx)
        recs = result.get("recommendations", [])
        # AWS Lightsail Pricing API uses `bundle` / `bundleGroup` filters,
        # NOT `instanceType` (verified via get_pricing_service_attributes).
        # The previous get_instance_monthly_price() path was structurally dead
        # (returned 0 every time). Pricing comes from the shim's live-validated
        # per-bundle monthly tables (OS-aware), which are the authoritative
        # source for Lightsail in this codebase.
        #
        # Every counted dollar is single-sourced HERE (number + string,
        # region-scaled once) so the card never desyncs from the headline:
        #   - Idle Resource Cleanup (stopped instance) → full bundle price
        #     (lightsail H1/H2/H3); unknown bundle id → $0 advisory + warn (H3).
        #   - Unused Resource Cleanup (unattached static IP) → $0.005/hr × 730
        #     (lightsail H4 — previously displayed-but-uncounted).
        #   - Instance Rightsizing (name-based oversized, no utilization metric)
        #     → $0 advisory (lightsail C1 / Cluster F desync).
        multiplier = getattr(ctx, "pricing_multiplier", 1.0)
        region = getattr(ctx, "region", None)
        savings = 0.0
        for rec in recs:
            category = rec.get("CheckCategory", "")

            if category == "Instance Rightsizing":
                # Name-based oversized (no utilization metric). The adapter
                # previously summed the FULL bundle cost while the card text said
                # ``× 0.3`` — a 3.33× over-count plus a headline-vs-card desync.
                # Without a utilization signal we cannot quantify the
                # current→target bundle delta, so demote to $0 advisory.
                rec["Counted"] = False
                rec["EstimatedMonthlySavings"] = 0.0
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: verify utilization before downsizing; "
                    "realized saving needs the current−one-size-down bundle delta "
                    "gated on a CPU/utilization metric"
                )
                rec["AuditBasis"] = {
                    "unmeasured_inputs": ["instance_utilization"],
                    "reason": "name-based oversized; advisory per cost-scope rule",
                }
                continue

            if category == "Unused Resource Cleanup":
                # H4: unattached static IP billed at $0.005/hr after the first
                # free hour. Region-scaled so counted == displayed.
                monthly = round(
                    LIGHTSAIL_UNUSED_STATIC_IP_HOURLY * HOURS_PER_MONTH * multiplier, 2
                )
                rec["EstimatedMonthlySavings"] = monthly
                rec["EstimatedSavings"] = f"${monthly:.2f}/month"
                rec["AuditBasis"] = {
                    "rate": LIGHTSAIL_UNUSED_STATIC_IP_HOURLY,
                    "metric_window": "flat hourly charge while unattached",
                    "region": region,
                    "multiplier": multiplier,
                    "formula": "$0.005/hr × 730 hr × region multiplier",
                    "source": "AmazonLightsail Pricing API usagetype USE1-UnusedStaticIP",
                }
                savings += monthly
                continue

            # Idle Resource Cleanup (stopped instance) — deleting it recovers its
            # full bundle cost, so the bundle price IS the realized saving.
            bundle_name = rec.get("BundleId", "")
            monthly_base = get_lightsail_bundle_cost(bundle_name)
            if monthly_base is None:
                # H3: missing or unrecognized bundle id (unknown gen/optimized
                # family) — fail safe to a $0 advisory and surface the gap;
                # never fabricate a default dollar into the headline.
                ctx.warn(
                    f"Unrecognized Lightsail bundle id '{bundle_name}'; cannot "
                    "price deletion saving — emitting $0 advisory",
                    "lightsail",
                )
                rec["Counted"] = False
                rec["EstimatedMonthlySavings"] = 0.0
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: unrecognized bundle id; live price unavailable"
                )
                rec["AuditBasis"] = {
                    "unmeasured_inputs": ["bundle_monthly_price"],
                    "bundle_id": bundle_name,
                    "reason": "bundle id not in validated Lightsail price tables",
                }
                continue

            monthly = round(monthly_base * multiplier, 2)
            rec["EstimatedMonthlySavings"] = monthly
            rec["EstimatedSavings"] = f"${monthly:.2f}/month"
            rec["AuditBasis"] = {
                "rate": monthly_base,
                "bundle_id": bundle_name,
                "operating_system": rec.get("OperatingSystem", "Linux"),
                "region": region,
                "multiplier": multiplier,
                "formula": "AWS Lightsail published monthly bundle price × region multiplier",
                "source": "AmazonLightsail Pricing API usagetype USE1-BundleUsage:<mem>[_win]",
            }
            savings += monthly

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        # Count hygiene: $0 advisory recs (oversized, unknown-bundle) render but
        # are excluded from the rec headline, mirroring _savings.mark_zero_savings_advisory.
        counted = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="Lightsail",
            total_recommendations=counted,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS,
        )
