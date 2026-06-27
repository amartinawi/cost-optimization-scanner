"""Keyword-rate adapter for OpenSearch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
from services.opensearch import (
    LOW_CPU_THRESHOLD,
    OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_opensearch_checks,
)

# OpenSearch-managed EBS storage rates ($/GB-month, us-east-1 baseline). Region-
# scaled via pricing_multiplier at the per-rec emit site. Validated 2026-06-27
# against the AWS Pricing API (service AmazonES, productFamily "Amazon OpenSearch
# Service Volume"): ES:GP3-Storage = $0.122/GB-Mo, ES:GP2-Storage = $0.135/GB-Mo.
GP3_PRICE_PER_GB_MONTH: float = 0.122
GP2_PRICE_PER_GB_MONTH: float = 0.135

# x86/Intel OpenSearch (AmazonES) instance family -> its same-size Graviton
# (ARM) equivalent. The realizable Graviton saving is the exact per-node price
# delta, NOT a flat 20-40% / 0.25 price-performance figure (that is a
# perf-per-dollar metric, not a cost reduction — the real node-price delta is
# ~5-10%). The old flat GRAVITON_RATE=0.25 overstated it ~3-5x (live-audit H4).
# Families with no clean same-size Graviton counterpart (storage i3/i2, etc.)
# are omitted so the caller emits a $0 advisory instead of guessing a target.
_X86_TO_GRAVITON_FAMILY: dict[str, str] = {
    "m3": "m6g",
    "m4": "m6g",
    "m5": "m6g",
    "c4": "c6g",
    "c5": "c6g",
    "r3": "r6g",
    "r4": "r6g",
    "r5": "r6g",
    "t2": "t4g",
    "t3": "t4g",
}

# Standard OpenSearch instance size ladder (ascending). Used to derive the
# one-size-down downsize target for an underutilized domain (OpenSearch C3).
_SIZE_LADDER: tuple[str, ...] = (
    "micro",
    "small",
    "medium",
    "large",
    "xlarge",
    "2xlarge",
    "4xlarge",
    "8xlarge",
    "12xlarge",
    "16xlarge",
    "24xlarge",
)


def _one_size_down(instance_type: str | None) -> str | None:
    """Return the OpenSearch instance type one size smaller, or None.

    OpenSearch types are ``<family>.<size>.<suffix>`` where suffix is ``search``
    (or legacy ``elasticsearch``) -- e.g. ``r6g.xlarge.search`` ->
    ``r6g.large.search``. Returns None for the smallest rung, an unknown size, or
    an unparseable type. The target is validated by a quiet pricing lookup
    downstream, so a size that does not exist for the family simply yields no
    counted saving (fail safe).
    """
    if not instance_type:
        return None
    parts = instance_type.split(".")
    if len(parts) < 2:
        return None
    size = parts[1]
    if size not in _SIZE_LADDER:
        return None
    idx = _SIZE_LADDER.index(size)
    if idx == 0:
        return None
    parts[1] = _SIZE_LADDER[idx - 1]
    return ".".join(parts)


def _downsize_node_delta(ctx: Any, instance_type: str | None) -> tuple[float, str | None]:
    """Per-node $/month saved by downsizing one OpenSearch instance size.

    Concrete current -> one-size-down node-price delta (replaces the flat 0.30
    reduction factor -- OpenSearch C3). Returns ``(0.0, None)`` (caller emits a
    $0 advisory) when pricing is unavailable, the type cannot be downsized, or
    the delta is non-positive -- we never assert a downsize saving we cannot
    substantiate from two live prices.

    Returns:
        Tuple of (per-node monthly delta, target instance type) -- the target is
        None whenever the delta is 0.0.
    """
    if ctx.pricing_engine is None or not instance_type:
        return 0.0, None
    target = _one_size_down(instance_type)
    if target is None:
        return 0.0, None
    current = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
    smaller = ctx.pricing_engine.get_instance_monthly_price("AmazonES", target)
    if current <= 0 or smaller <= 0 or smaller >= current:
        return 0.0, None
    return current - smaller, target


def _graviton_equivalent(instance_type: str | None) -> str | None:
    """Map an x86 OpenSearch instance type to its same-size Graviton equivalent.

    ``r5.xlarge.search`` -> ``r6g.xlarge.search``. Returns ``None`` when the
    family has no known Graviton counterpart, so the caller demotes the Graviton
    rec to a $0 advisory rather than fabricating a delta against an unknown
    node price (live-audit H4).
    """
    if not instance_type:
        return None
    parts = instance_type.split(".")
    if len(parts) < 2:
        return None
    graviton_family = _X86_TO_GRAVITON_FAMILY.get(parts[0])
    if graviton_family is None:
        return None
    parts[0] = graviton_family
    return ".".join(parts)


def _graviton_node_delta(ctx: Any, instance_type: str | None) -> tuple[float, str | None]:
    """Per-node $/month saved by migrating one OpenSearch node x86 -> Graviton.

    Concrete current -> same-size Graviton node-price delta (replaces the flat
    0.25 price-performance proxy -- live-audit H4). Returns ``(0.0, None)``
    (caller emits a $0 advisory) when pricing is unavailable, the family has no
    Graviton counterpart, or the delta is non-positive -- we never assert a
    migration saving we cannot substantiate from two live prices.

    Returns:
        Tuple of (per-node monthly delta, target instance type) -- the target is
        None whenever the delta is 0.0.
    """
    if ctx.pricing_engine is None or not instance_type:
        return 0.0, None
    target = _graviton_equivalent(instance_type)
    if target is None:
        return 0.0, None
    current = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
    graviton = ctx.pricing_engine.get_instance_monthly_price("AmazonES", target)
    if current <= 0 or graviton <= 0 or graviton >= current:
        return 0.0, None
    return current - graviton, target


class OpensearchModule(BaseServiceModule):
    """ServiceModule adapter for OpenSearch. Keyword-rate savings strategy."""

    key: str = "opensearch"
    cli_aliases: tuple[str, ...] = ("opensearch",)
    display_name: str = "OpenSearch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for OpenSearch scanning."""
        return ("opensearch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan OpenSearch domains for cost optimization opportunities.

        Consults enhanced OpenSearch checks and Cost Optimization Hub. CoH is
        the authoritative aggregator: a domain covered by CoH suppresses that
        domain's heuristic findings (avoids double-counting). Savings calculated
        via keyword-rate heuristics matching Reserved, Graviton, and storage
        patterns.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks and (when present)
            cost_optimization_hub SourceBlocks.
        """
        result = get_enhanced_opensearch_checks(ctx)
        recs = result.get("recommendations", [])

        # Cost Optimization Hub re-surfaces OpenSearch rightsizing/idle findings
        # the orchestrator bucketed into ctx.cost_hub_splits["opensearch"]. CoH
        # is authoritative: a domain it covers suppresses that domain's heuristic
        # levers (SR-3 / OpenSearch C1).
        coh_recs = [r for r in getattr(ctx, "cost_hub_splits", {}).get("opensearch", []) if is_renderable_coh_rec(r)]
        coh_keys = {coh_key(r) for r in coh_recs} - {""}
        coh_total = sum(coh_savings(r) for r in coh_recs)

        # Price every rec and attach the per-rec dollar figure (the report
        # previously showed only "30-50%", with no per-domain $). Each counted
        # dollar carries a structured AuditBasis so it is defensible from the
        # report alone.
        for rec in recs:
            category = rec.get("CheckCategory", "")
            instance_type = rec.get("InstanceType")
            instance_count = rec.get("InstanceCount", 1) or 1
            value = 0.0
            audit_basis: dict[str, Any] | None = None
            if category == "Idle Domain":
                # Deleting an idle domain recovers 100% of its cost: full
                # instance monthly × count + full EBS storage (opensearch C2 —
                # previously priced $0 because the rec carried no InstanceType).
                # Priced higher than Graviton (25% instance delta) so it wins the
                # per-domain best-lever dedup below.
                instance_monthly = 0.0
                if ctx.pricing_engine is not None and instance_type:
                    instance_monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
                ebs = rec.get("EBSVolumeSize", 0) or 0
                storage_monthly = ebs * GP3_PRICE_PER_GB_MONTH * ctx.pricing_multiplier
                value = (instance_monthly * instance_count) + storage_monthly
                audit_basis = {
                    "instance_rate_monthly": round(instance_monthly, 4),
                    "instance_count": instance_count,
                    "storage_gb": ebs,
                    "gp3_rate_per_gb_month": GP3_PRICE_PER_GB_MONTH,
                    "region_multiplier": round(ctx.pricing_multiplier, 4),
                    "formula": "instance_rate x count + storage_gb x gp3_rate x region_multiplier",
                }
            elif "storage" in category.lower():
                # gp2 -> gp3 migration delta (OpenSearch H3): the realizable
                # saving is the per-GB price *difference*, not a flat fraction of
                # the gp3 base. Both rates are OpenSearch-managed EBS, region-
                # scaled once.
                ebs = rec.get("EBSVolumeSize", 0) or 0
                delta_rate = GP2_PRICE_PER_GB_MONTH - GP3_PRICE_PER_GB_MONTH
                value = ebs * delta_rate * ctx.pricing_multiplier
                audit_basis = {
                    "storage_gb": ebs,
                    "gp2_rate_per_gb_month": GP2_PRICE_PER_GB_MONTH,
                    "gp3_rate_per_gb_month": GP3_PRICE_PER_GB_MONTH,
                    "delta_rate_per_gb_month": round(delta_rate, 4),
                    "region_multiplier": round(ctx.pricing_multiplier, 4),
                    "formula": "storage_gb x (gp2_rate - gp3_rate) x region_multiplier",
                }
            elif category == "Underutilized Domain":
                # Concrete current -> one-size-down node-price delta (OpenSearch
                # C3): replaces the flat 0.30 reduction factor. CloudWatch-gated
                # upstream (avg CPU < LOW_CPU_THRESHOLD). Abstains to a $0
                # advisory when the downsize target cannot be priced (fail safe).
                per_node_delta, target = _downsize_node_delta(ctx, instance_type)
                value = per_node_delta * instance_count
                if value > 0:
                    audit_basis = {
                        "current_type": instance_type,
                        "target_type": target,
                        "per_node_delta_monthly": round(per_node_delta, 4),
                        "instance_count": instance_count,
                        "metric": f"CloudWatch AWS/ES CPUUtilization avg < {LOW_CPU_THRESHOLD}% over 14d",
                        "formula": "(current_node_monthly - one_size_down_node_monthly) x count",
                    }
            elif category == "Graviton Migration":
                # Concrete current -> same-size Graviton node-price delta
                # (live-audit H4): replaces the flat 0.25 price-performance
                # proxy, which overstated the real ~5-10% node delta ~3-5x.
                # Abstains to a $0 advisory when no Graviton counterpart prices.
                per_node_delta, target = _graviton_node_delta(ctx, instance_type)
                value = per_node_delta * instance_count
                if value > 0:
                    audit_basis = {
                        "current_type": instance_type,
                        "target_type": target,
                        "per_node_delta_monthly": round(per_node_delta, 4),
                        "instance_count": instance_count,
                        "formula": "(current_node_monthly - same_size_graviton_node_monthly) x count",
                    }
            rec["EstimatedMonthlySavings"] = round(value, 2)
            if audit_basis is not None:
                rec["AuditBasis"] = audit_basis
            if "Reserved" in category:
                rec["Counted"] = False  # commitment lever — advisory
            elif value <= 0 and category in ("Underutilized Domain", "Graviton Migration"):
                # Could not quantify a concrete delta → explicit $0 advisory
                # (shown, not counted) — never a silent drop (OpenSearch C3).
                rec["Counted"] = False
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: no concrete price delta available "
                    "(downsize target / instance rate not priceable)"
                )
            # CoH already covers this domain → demote the heuristic lever so the
            # same saving is not counted twice.
            if rec.get("DomainName", "") in coh_keys:
                rec["Counted"] = False

        # Dedupe instance-axis levers (Graviton vs downsize) per domain — they are
        # alternatives on the same nodes. Storage is a separate axis (kept). A rec
        # that prices to $0 (e.g. underutilized with no InstanceType) is advisory.
        best_instance: dict[str, dict[str, Any]] = {}
        for rec in recs:
            if rec.get("Counted") is False or "storage" in rec.get("CheckCategory", "").lower():
                continue
            dom = rec.get("DomainName", "")
            cur = best_instance.get(dom)
            if cur is None or rec["EstimatedMonthlySavings"] > cur["EstimatedMonthlySavings"]:
                best_instance[dom] = rec

        best_ids = {id(r) for r in best_instance.values()}
        savings = 0.0
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            is_storage = "storage" in rec.get("CheckCategory", "").lower()
            keep = (is_storage or id(rec) in best_ids) and rec["EstimatedMonthlySavings"] > 0
            if keep:
                rec["Counted"] = True
                savings += rec["EstimatedMonthlySavings"]
            else:
                rec["Counted"] = False

        savings += coh_total

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        if coh_recs:
            sources["cost_optimization_hub"] = SourceBlock(count=len(coh_recs), recommendations=tuple(coh_recs))

        return ServiceFindings(
            service_name="OpenSearch",
            total_recommendations=len(recs) + len(coh_recs),
            total_monthly_savings=round(savings, 2),
            sources=sources,
            optimization_descriptions=OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
        )
