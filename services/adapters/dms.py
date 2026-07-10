"""Live-pricing adapter for DMS (AZ-pinned replication-instance pricing)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dms import DMS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_dms_checks

# An *unused* replication instance recovers its FULL cost on termination — the
# only lever whose saving is a fixed fraction of the instance price, because the
# fraction is 1.0 and the instance simply stops existing.
#
# A *rightsizing* candidate does NOT: its saving is the concrete price delta
# between the current class and the next size down, priced live per account and
# region. The previous flat 0.35 was a fabricated proxy — on bnc it credited
# $74.09/mo against a dms.r5.large, which is the SMALLEST size in the r5 family,
# so there was no one-size-down target for the 35% to represent (live-audit: same
# flat-% fabrication class as ElastiCache H3 and OpenSearch H4).
_DMS_TERMINATION_FACTOR: float = 1.0
_UNUSED_CATEGORY: str = "Unused DMS Instances"
_RIGHTSIZE_CATEGORY: str = "Instance Optimization"

# Ascending DMS replication-instance size ladder. A target that does not exist
# for a family (e.g. dms.r5.medium) simply prices to $0 and yields no counted
# saving — the quiet pricing lookup is the validator, so no per-family table is
# needed (mirrors opensearch._one_size_down).
_SIZE_LADDER: tuple[str, ...] = (
    "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge",
    "8xlarge", "9xlarge", "12xlarge", "16xlarge", "18xlarge", "24xlarge",
)


def _one_size_down(instance_class: str) -> str | None:
    """Return the DMS instance class one size smaller within the same family.

    ``dms.r5.xlarge`` -> ``dms.r5.large``. Returns ``None`` for the smallest rung
    of a family (``dms.r5.large``), an unknown size, or an unparseable class —
    the caller then emits a $0 advisory rather than a fabricated dollar.
    """
    parts = (instance_class or "").split(".")
    if len(parts) != 3 or parts[0] != "dms":
        return None
    size = parts[2]
    if size not in _SIZE_LADDER:
        return None
    idx = _SIZE_LADDER.index(size)
    if idx == 0:
        return None
    return f"{parts[0]}.{parts[1]}.{_SIZE_LADDER[idx - 1]}"


def _downsize_delta(ctx: Any, instance_class: str, *, multi_az: bool) -> tuple[float, str | None, float, float]:
    """$/month saved by moving a DMS instance one size down, with both prices.

    Returns ``(0.0, None)`` when the class is already the smallest in its family,
    the target does not exist for that family (prices to $0), or pricing is
    unavailable — we never assert a downsize saving we cannot substantiate from
    two live prices.

    Both lookups pass ``allow_fallback=False``. A size that does not exist for a
    family (``dms.r5.medium``) would otherwise price to the DMS fallback constant,
    and the delta against it would be pure fabrication — the exact defect this
    function replaces.
    """
    if ctx.pricing_engine is None or not instance_class:
        return 0.0, None, 0.0, 0.0
    target = _one_size_down(instance_class)
    if target is None:
        return 0.0, None, 0.0, 0.0
    current = ctx.pricing_engine.get_dms_instance_monthly_price(
        instance_class, multi_az=multi_az, allow_fallback=False
    )
    smaller = ctx.pricing_engine.get_dms_instance_monthly_price(target, multi_az=multi_az, allow_fallback=False)
    if current <= 0 or smaller <= 0 or smaller >= current:
        return 0.0, None, current, smaller
    return current - smaller, target, current, smaller


class DmsModule(BaseServiceModule):
    """ServiceModule adapter for DMS.

    Prices replication instances via the deterministic, AZ-pinned
    ``PricingEngine.get_dms_instance_monthly_price`` (Single-AZ ``InstanceUsg``
    vs Multi-AZ ``Multi-AZUsg`` SKU). Every counted dollar is a real price:
    an *unused* instance recovers its full monthly price on termination, a
    *rightsizing* candidate recovers the concrete current -> one-size-down delta,
    and the Multi-AZ -> Single-AZ lever recovers the real per-AZ delta. A
    rightsizing candidate with no priceable smaller target (e.g. ``dms.r5.large``,
    already the smallest ``r5``) is a $0 advisory. No flat fallbacks, no
    percentage proxies.
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

        # --- Instance rightsizing / unused: per-rec counted dollar ----------- #
        # Each heuristic rec is enriched (immutable copy) with the exact dollar
        # the tab headline sums, so the card renders the same figure (dms L5).
        # The factor is category-specific: an unused instance recovers its FULL
        # cost on termination (1.0); a rightsizing candidate recovers ~35% by
        # downsizing (dms L1). An instance already owned by the Multi-AZ lever
        # above is excluded so the same compute is never counted twice
        # (per-AZ delta + 35%).
        savings = 0.0
        sources: dict[str, SourceBlock] = {}
        for category, category_recs in checks.items():
            enriched: list[dict[str, Any]] = []
            for rec in category_recs:
                iid = rec.get("InstanceId") or rec.get("ReplicationInstanceIdentifier", "")
                if iid and iid in multi_az_ids:
                    continue
                if rec.get("Counted") is False:
                    # dms F3 — the shim already demoted this to a $0 advisory (e.g.
                    # a low-CPU instance with attached replication tasks). Render it
                    # but never price or count it.
                    enriched.append(dict(rec, EstimatedMonthlySavings=0.0))
                    continue
                instance_class = rec.get("InstanceClass", "")
                # NOT `category` — that is the source-block key from the enclosing
                # loop; shadowing it re-keys the SourceBlock by CheckCategory.
                rec_category = rec.get("CheckCategory", "")
                multi_az = bool(rec.get("MultiAZ"))
                per_rec_saving = 0.0
                audit_basis: dict[str, Any] | None = None
                advisory_reason: str | None = None

                if ctx.pricing_engine is not None and instance_class and rec_category == _UNUSED_CATEGORY:
                    # Terminating an unused instance recovers its whole price.
                    monthly = ctx.pricing_engine.get_dms_instance_monthly_price(instance_class, multi_az=multi_az)
                    if monthly > 0:
                        per_rec_saving = monthly * _DMS_TERMINATION_FACTOR
                        savings += per_rec_saving
                        audit_basis = {
                            "rate_source": "AWS Pricing API AWSDatabaseMigrationSvc / Replication Server",
                            "instance_monthly": round(monthly, 2),
                            "region": getattr(ctx, "region", "unknown"),
                            "formula": "full instance monthly (terminate)",
                        }
                elif ctx.pricing_engine is not None and instance_class and rec_category == _RIGHTSIZE_CATEGORY:
                    # The realizable saving is the concrete current -> one-size-down
                    # price delta, never a flat fraction of the instance price.
                    delta, target, current, smaller = _downsize_delta(ctx, instance_class, multi_az=multi_az)
                    if delta > 0 and target:
                        per_rec_saving = delta
                        savings += per_rec_saving
                        audit_basis = {
                            "rate_source": "AWS Pricing API AWSDatabaseMigrationSvc / Replication Server",
                            "current_class": instance_class,
                            "target_class": target,
                            "current_monthly": round(current, 2),
                            "target_monthly": round(smaller, 2),
                            "region": getattr(ctx, "region", "unknown"),
                            "formula": f"{instance_class} ${current:.2f}/mo - {target} ${smaller:.2f}/mo",
                        }
                    else:
                        target_class = _one_size_down(instance_class)
                        if target_class is None:
                            advisory_reason = (
                                f"{instance_class} is the smallest DMS size — no one-size-down target"
                            )
                        elif current <= 0:
                            advisory_reason = (
                                f"no live Pricing-API SKU for {instance_class}; refusing to price a "
                                f"downsize against a fallback rate"
                            )
                        elif smaller <= 0:
                            # Either the family starts at this size (dms.r5.large:
                            # dms.r5.medium does not exist) or the SKU is missing.
                            # Both mean the same thing here — nothing to price against.
                            advisory_reason = (
                                f"no live Pricing-API SKU for {target_class}; {instance_class} has no "
                                f"priceable one-size-down target"
                            )
                        else:
                            advisory_reason = f"{target_class} is not cheaper than {instance_class}"
                    # else: no concrete delta; render prose, count nothing.
                # else: instance class / category unknown; render prose, count nothing.
                if per_rec_saving <= 0 and advisory_reason:
                    enriched.append(
                        {
                            **rec,
                            "EstimatedMonthlySavings": 0.0,
                            "Counted": False,
                            "EstimatedSavings": f"$0.00/month — advisory: {advisory_reason}",
                            "AuditBasis": {"reconciliation": advisory_reason},
                        }
                    )
                    continue
                if per_rec_saving > 0:
                    enriched.append(
                        {
                            **rec,
                            "EstimatedMonthlySavings": per_rec_saving,
                            "Counted": True,
                            "EstimatedSavings": f"${per_rec_saving:.2f}/month",
                            "AuditBasis": audit_basis,
                        }
                    )
                else:
                    enriched.append(dict(rec))
            if enriched:
                sources[category] = SourceBlock(count=len(enriched), recommendations=tuple(enriched))

        savings += sum(r["EstimatedMonthlySavings"] for r in multi_az_recs if r.get("Counted"))

        if multi_az_recs:
            sources["multi_az_review"] = SourceBlock(count=len(multi_az_recs), recommendations=tuple(multi_az_recs))

        # Count hygiene: $0 advisory recs (Multi-AZ review, and dms F3 low-CPU-but-
        # task-attached instances) are rendered but excluded from the headline count.
        kept_heuristic = sum(
            1
            for key, block in sources.items()
            if key != "multi_az_review"
            for r in block.recommendations
            if r.get("Counted") is not False
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
