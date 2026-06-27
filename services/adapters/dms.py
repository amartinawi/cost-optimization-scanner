"""Live-pricing adapter for DMS (AZ-pinned replication-instance pricing)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dms import DMS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_dms_checks


class DmsModule(BaseServiceModule):
    """ServiceModule adapter for DMS.

    Prices replication instances via the deterministic, AZ-pinned
    ``PricingEngine.get_dms_instance_monthly_price`` (Single-AZ ``InstanceUsg``
    vs Multi-AZ ``Multi-AZUsg`` SKU). Rightsizing/unused levers count 35% of
    the AZ-correct instance price; the Multi-AZ->Single-AZ lever counts the real
    per-AZ price delta. No flat fallbacks.
    """

    key: str = "dms"
    cli_aliases: tuple[str, ...] = ("dms",)
    display_name: str = "DMS"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for DMS scanning."""
        return ("dms", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan DMS replication instances for cost optimization opportunities.

        Consults the dms service module for instance rightsizing and Multi-AZ
        review. Savings are priced from the AZ-pinned AWS Pricing API SKU.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        result = get_enhanced_dms_checks(ctx)
        recs = result.get("recommendations", [])
        checks = result.get("checks", {})

        # --- Multi-AZ-in-non-prod lever (config-based; real per-AZ delta) ---- #
        # A Multi-AZ DMS instance bills at exactly 2x its Single-AZ rate
        # (validated us-east-1 dms.t3.medium: $0.0745/hr InstanceUsg vs
        # $0.149/hr Multi-AZUsg). The realizable saving is the *per-AZ delta*
        # (Multi-AZ monthly - Single-AZ monthly), NOT 50% of an ambiguous
        # lookup that may itself have returned the Single-AZ SKU (which halved
        # the lever — dms H1/H2). Use the deterministic AZ-pinned price method.
        multi_az_recs: list[dict[str, Any]] = []
        multi_az_ids: set[str] = set()
        for rec in recs:
            if not rec.get("MultiAZ"):
                continue
            instance_class = rec.get("InstanceClass", "unknown")
            name = rec.get("InstanceId") or rec.get("ReplicationInstanceIdentifier", "")
            tags = rec.get("Tags", [])
            tag_values = " ".join(str(t.get("Value", "")) for t in tags).lower()
            is_non_prod = any(
                kw in name.lower() or kw in tag_values for kw in ("dev", "test", "staging", "sandbox", "nonprod")
            )
            if not is_non_prod:
                continue

            az_savings = 0.0
            audit_basis: dict[str, Any] | None = None
            az_warning = None
            if ctx.pricing_engine is not None and instance_class != "unknown":
                # PricingEngine returns region-correct $/month; do NOT
                # re-multiply by ctx.pricing_multiplier (L2.3.1).
                multi_price = ctx.pricing_engine.get_dms_instance_monthly_price(instance_class, multi_az=True)
                single_price = ctx.pricing_engine.get_dms_instance_monthly_price(instance_class, multi_az=False)
                az_savings = round(max(multi_price - single_price, 0.0), 2)
                audit_basis = {
                    "rate_source": "AWS Pricing API AWSDatabaseMigrationSvc / Replication Server",
                    "single_az_usagetype": f"InstanceUsg:dms.{instance_class.replace('dms.', '')}",
                    "multi_az_usagetype": f"Multi-AZUsg:dms.{instance_class.replace('dms.', '')}",
                    "single_az_monthly": round(single_price, 2),
                    "multi_az_monthly": round(multi_price, 2),
                    "region": getattr(ctx, "region", "unknown"),
                    "formula": "Multi-AZ monthly - Single-AZ monthly (real per-AZ delta)",
                }
            if az_savings <= 0.0:
                az_warning = "instance class pricing unavailable"
            multi_az_recs.append(
                {
                    "Resource": name,
                    "InstanceId": name,
                    "InstanceClass": instance_class,
                    "MultiAZ": True,
                    "Recommendation": "Switch Multi-AZ DMS instance to Single-AZ for dev/test",
                    "EstimatedSavings": (
                        f"${az_savings:.2f}/month (Multi-AZ - Single-AZ per-AZ delta)"
                        if az_savings > 0
                        else "$0.00/month - advisory: instance class pricing unavailable"
                    ),
                    "EstimatedMonthlySavings": az_savings,
                    "Counted": az_savings > 0,
                    **({"AuditBasis": audit_basis} if audit_basis else {}),
                    **({"PricingWarning": az_warning} if az_warning else {}),
                    "CheckCategory": "DMS Multi-AZ in Non-Prod",
                }
            )
            if name:
                multi_az_ids.add(name)

        # --- Instance rightsizing / unused (35% of the AZ-correct price) ----- #
        # An instance already owned by the Multi-AZ lever above is excluded here
        # so the same compute is never counted twice (per-AZ delta + 35%).
        savings = 0.0
        for rec in recs:
            iid = rec.get("InstanceId") or rec.get("ReplicationInstanceIdentifier", "")
            if iid and iid in multi_az_ids:
                continue
            instance_class = rec.get("InstanceClass", "")
            if ctx.pricing_engine is not None and instance_class:
                monthly = ctx.pricing_engine.get_dms_instance_monthly_price(
                    instance_class, multi_az=bool(rec.get("MultiAZ"))
                )
                if monthly > 0:
                    savings += monthly * 0.35
                # else: pricing miss; skip rather than fabricate a $50 fallback.
            # else: instance_class unknown; skip rather than fabricate a fallback.

        savings += sum(r["EstimatedMonthlySavings"] for r in multi_az_recs if r.get("Counted"))

        # --- sources: drop Multi-AZ-owned instances from the heuristic blocks - #
        sources = {}
        for category, category_recs in checks.items():
            kept = tuple(
                r
                for r in category_recs
                if (r.get("InstanceId") or r.get("ReplicationInstanceIdentifier", "")) not in multi_az_ids
            )
            if kept:
                sources[category] = SourceBlock(count=len(kept), recommendations=kept)
        if multi_az_recs:
            sources["multi_az_review"] = SourceBlock(count=len(multi_az_recs), recommendations=tuple(multi_az_recs))

        # Count hygiene: $0 advisory Multi-AZ recs are rendered but not counted.
        kept_heuristic = sum(
            1
            for category_recs in checks.values()
            for r in category_recs
            if (r.get("InstanceId") or r.get("ReplicationInstanceIdentifier", "")) not in multi_az_ids
        )
        counted_multi_az = sum(1 for r in multi_az_recs if r.get("Counted"))
        total_count = kept_heuristic + counted_multi_az

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
