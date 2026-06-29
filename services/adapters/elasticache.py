"""Live-priced adapter for ElastiCache (node-price deltas; CoH-authoritative)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
from services.elasticache import get_enhanced_elasticache_checks

# x86/Intel ElastiCache node family -> its same-size Graviton (ARM) equivalent.
# The realizable Graviton saving is the exact per-node price delta, NOT a flat
# 20-40% price-performance figure (that is a perf-per-dollar metric, not a cost
# reduction). Live AWS Pricing API, us-east-1 (NodeUsage, On-Demand, Redis,
# validated 2026-06-27): cache.r5.large $0.216/hr vs cache.r6g.large $0.206/hr
# -> $7.30/mo/node (~4.6% of node cost); cache.m5.large $0.156 vs cache.m6g.large
# $0.149 -> $5.11/mo/node (~4.5%). The old flat 0.20 overcounted ~4.3x
# (elasticache H2).
_X86_TO_GRAVITON_FAMILY: dict[str, str] = {
    "m4": "m6g",
    "m5": "m6g",
    "m6i": "m6g",
    "m6id": "m6g",
    "m7i": "m7g",
    "r4": "r6g",
    "r5": "r6g",
    "r6i": "r6g",
    "r6id": "r6g",
    "r7i": "r7g",
    "c4": "c6g",
    "c5": "c6g",
    "c6i": "c6g",
    "c7i": "c7g",
    "t2": "t4g",
    "t3": "t4g",
}


def graviton_equivalent(node_type: str) -> str | None:
    """Map an x86 ElastiCache node type to its same-size Graviton equivalent.

    ``cache.r5.large`` -> ``cache.r6g.large``. Returns ``None`` when the family
    has no known Graviton counterpart, so the caller demotes the Graviton rec to
    a $0 advisory rather than fabricating a delta against an unknown node price.

    Args:
        node_type: An ElastiCache node type, e.g. ``cache.r5.large``.

    Returns:
        The Graviton-family node type, or ``None`` if unmappable.
    """
    if not node_type.startswith("cache."):
        return None
    family_size = node_type[len("cache.") :]
    if "." not in family_size:
        return None
    family, size = family_size.split(".", 1)
    graviton_family = _X86_TO_GRAVITON_FAMILY.get(family)
    if graviton_family is None:
        return None
    return f"cache.{graviton_family}.{size}"


# ElastiCache node size ladder (smallest -> largest), within a family. Used to
# derive the one-size-down rightsizing target for an underutilized cluster. A
# size token outside this ladder (e.g. an unusual ``18xlarge``) maps to None so
# the caller demotes the lever to a $0 advisory rather than guess a target.
_NODE_SIZE_ORDER: tuple[str, ...] = (
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


def downsize_target(node_type: str) -> str | None:
    """Return the one-size-smaller ElastiCache node type within the same family.

    ``cache.r5.xlarge`` -> ``cache.r5.large``. Returns ``None`` for the smallest
    size, an unparseable type, or a size token not on ``_NODE_SIZE_ORDER`` so the
    caller can demote the downsize lever to a $0 advisory instead of fabricating
    a delta against an unknown target. One-size-down is the conservative,
    defensible rightsizing target (mirrors the EC2 rightsizing approach).

    Args:
        node_type: An ElastiCache node type, e.g. ``cache.r5.xlarge``.

    Returns:
        The one-size-smaller node type, or ``None`` if unmappable.
    """
    if not node_type.startswith("cache."):
        return None
    family_size = node_type[len("cache.") :]
    if "." not in family_size:
        return None
    family, size = family_size.split(".", 1)
    try:
        idx = _NODE_SIZE_ORDER.index(size)
    except ValueError:
        return None
    if idx == 0:
        return None
    return f"cache.{family}.{_NODE_SIZE_ORDER[idx - 1]}"


class ElasticacheModule(BaseServiceModule):
    """ServiceModule adapter for ElastiCache. Live node-price-delta savings strategy."""

    key: str = "elasticache"
    cli_aliases: tuple[str, ...] = ("elasticache",)
    display_name: str = "ElastiCache"
    # Shim hits cloudwatch.get_metric_statistics for CPU-utilization
    # analysis of underutilized clusters; in fast mode it skips that read and
    # suppresses the downsizing lever (elasticache H3).
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for ElastiCache scanning."""
        return ("elasticache", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan ElastiCache clusters for cost optimization opportunities.

        Consults the elasticache service module for Graviton migration, reserved
        nodes, underutilized clusters, engine version review, and Valkey
        evaluation, plus Cost Optimization Hub. CoH is the authoritative
        aggregator: a cluster it covers suppresses that cluster's heuristic
        levers (avoids double-counting). Savings calculated via keyword matching
        on EstimatedSavings text.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry and,
            when present, a "cost_optimization_hub" SourceBlock.
        """
        result = get_enhanced_elasticache_checks(ctx)
        recs = result.get("recommendations", [])

        # Cost Optimization Hub re-surfaces ElastiCache rightsizing/idle findings
        # the orchestrator bucketed into ctx.cost_hub_splits["elasticache"]. CoH
        # is authoritative: a cluster it covers suppresses that cluster's
        # heuristic levers (SR-3 / ElastiCache C1).
        coh_recs = [r for r in getattr(ctx, "cost_hub_splits", {}).get("elasticache", []) if is_renderable_coh_rec(r)]
        coh_keys = {coh_key(r) for r in coh_recs} - {""}
        coh_total = sum(coh_savings(r) for r in coh_recs)

        # Per-category discount rates keyed on the structured CheckCategory (not
        # fragile EstimatedSavings substrings). Reserved Nodes is a COMMITMENT
        # lever (overlaps the commitment tab) → advisory, never counted here.
        # Graviton Migration and Underutilized Cluster are NOT flat rates: each
        # counted dollar is an exact live node-price delta (see the per-branch
        # logic below). Valkey is AWS's published ~20% Redis→Valkey list-price
        # discount, a uniform real delta.
        rate_by_category = {
            "Valkey Migration": 0.20,
        }

        region = getattr(ctx, "region", None)

        # Price every rec and attach the per-rec dollar figure (fixes counted!=
        # rendered — the report previously showed only "30-50%").
        for rec in recs:
            category = rec.get("CheckCategory", "")
            node_type = rec.get("NodeType")
            num_nodes = rec.get("NumNodes", 1) or 1
            engine = rec.get("Engine")
            monthly_node = 0.0
            if ctx.pricing_engine is not None and node_type:
                # Engine is required to disambiguate the NodeUsage SKU
                # (Redis/Memcached/Valkey share the instance type — SR-1).
                monthly_node = ctx.pricing_engine.get_instance_monthly_price(
                    "AmazonElastiCache", node_type, engine=engine
                )

            if category == "Graviton Migration":
                # Exact realizable saving = (current node price − Graviton node
                # price) × node count. "20-40%" is a price-performance figure,
                # not a cost reduction; the real node-price delta is ~4-5%
                # (elasticache H2).
                graviton_type = graviton_equivalent(node_type or "")
                graviton_node = 0.0
                if graviton_type and ctx.pricing_engine is not None:
                    graviton_node = ctx.pricing_engine.get_instance_monthly_price(
                        "AmazonElastiCache", graviton_type, engine=engine
                    )
                delta = round((monthly_node - graviton_node) * num_nodes, 2)
                if graviton_type and delta > 0:
                    rec["EstimatedMonthlySavings"] = delta
                    rec["AuditBasis"] = {
                        "lever": "Graviton migration",
                        "rate": (
                            f"{node_type} ${round(monthly_node, 2)}/mo − "
                            f"{graviton_type} ${round(graviton_node, 2)}/mo per node "
                            "(AmazonElastiCache NodeUsage, On-Demand)"
                        ),
                        "region": region,
                        "metric_window": "n/a (node-price delta)",
                        "formula": (
                            f"({round(monthly_node, 2)} − {round(graviton_node, 2)}) × "
                            f"{num_nodes} node(s) = ${delta}/mo"
                        ),
                        "num_nodes": num_nodes,
                        "engine": engine,
                    }
                else:
                    # No Graviton counterpart / non-positive delta → cannot
                    # quantify a concrete saving → $0 advisory (keep "20-40%"
                    # only as qualitative wording).
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["Counted"] = False
            elif category == "Underutilized Cluster":
                # Exact realizable saving = (current node price − one-size-down
                # node price) × node count, NOT a flat 0.30 of node cost (a
                # fabricated proxy). The lever is already CloudWatch-CPU gated
                # upstream; here we ground its dollar in account-specific node
                # prices and one-size-down as the conservative target
                # (live-audit H3).
                target_type = downsize_target(node_type or "")
                target_node = 0.0
                if target_type and ctx.pricing_engine is not None:
                    target_node = ctx.pricing_engine.get_instance_monthly_price(
                        "AmazonElastiCache", target_type, engine=engine
                    )
                delta = round((monthly_node - target_node) * num_nodes, 2)
                if target_type and target_node > 0 and delta > 0:
                    rec["EstimatedMonthlySavings"] = delta
                    rec["AuditBasis"] = {
                        "lever": "Underutilized cluster downsizing",
                        "rate": (
                            f"{node_type} ${round(monthly_node, 2)}/mo − "
                            f"{target_type} ${round(target_node, 2)}/mo per node "
                            "(AmazonElastiCache NodeUsage, On-Demand)"
                        ),
                        "region": region,
                        "metric_window": "CloudWatch CPUUtilization (underutilized)",
                        "formula": (
                            f"({round(monthly_node, 2)} − {round(target_node, 2)}) × "
                            f"{num_nodes} node(s) = ${delta}/mo"
                        ),
                        "num_nodes": num_nodes,
                        "engine": engine,
                    }
                else:
                    # Smallest size, unmappable target, or unpriceable target →
                    # cannot quantify a concrete downsizing delta → $0 advisory.
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["Counted"] = False
            else:
                rate = rate_by_category.get(category, 0.0)
                rec["EstimatedMonthlySavings"] = round(monthly_node * num_nodes * rate, 2)

            if "Reserved" in category:
                rec["Counted"] = False  # commitment lever — advisory
            # CoH already covers this cluster → demote the heuristic lever so the
            # same saving is not counted twice.
            if rec.get("ClusterId", "") in coh_keys:
                rec["Counted"] = False

        # De-duplicate per cluster: Valkey / Graviton / downsize are alternative
        # remediations on the SAME node — counting all of them stacked overlapping
        # discounts on one node. Keep the single highest-$ lever counted per
        # cluster; mark the rest advisory.
        best_per_cluster: dict[str, dict[str, Any]] = {}
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            cid = rec.get("ClusterId", "")
            cur = best_per_cluster.get(cid)
            if cur is None or rec["EstimatedMonthlySavings"] > cur["EstimatedMonthlySavings"]:
                best_per_cluster[cid] = rec

        counted_ids = {id(r) for r in best_per_cluster.values()}
        savings = 0.0
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            if id(rec) in counted_ids and rec["EstimatedMonthlySavings"] > 0:
                rec["Counted"] = True
                savings += rec["EstimatedMonthlySavings"]
            else:
                rec["Counted"] = False  # superseded by a better lever on the same cluster

        savings += coh_total

        # Single-source the per-rec EstimatedSavings STRING from the finalized
        # counted dollar so the card the reporter renders (it reads
        # EstimatedSavings) matches the number summed into the headline
        # (counted == rendered). Advisory recs render an honest $0 line; the
        # qualitative "20-40%"/"20%"/"30-50%" wording is dropped from the savings
        # slot — it is a price-performance figure, not a $ saving.
        for rec in recs:
            if rec.get("Counted") is False:
                # Zero the numeric saving on advisory recs (mirrors s3.py / dms.py)
                # so a consumer reading EstimatedMonthlySavings without also
                # checking Counted cannot sum phantom advisory dollars; the
                # pre-demotion figure is kept under PotentialMonthlySavings for
                # transparency (elasticache EMV hygiene).
                if rec.get("EstimatedMonthlySavings", 0.0):
                    rec["PotentialMonthlySavings"] = rec["EstimatedMonthlySavings"]
                rec["EstimatedMonthlySavings"] = 0.0
                rec["EstimatedSavings"] = "$0.00/month — advisory (not counted toward total)"
            else:
                rec["EstimatedSavings"] = f"${rec.get('EstimatedMonthlySavings', 0.0):.2f}/month"

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        if coh_recs:
            sources["cost_optimization_hub"] = SourceBlock(count=len(coh_recs), recommendations=tuple(coh_recs))

        return ServiceFindings(
            service_name="ElastiCache",
            total_recommendations=len(recs) + len(coh_recs),
            total_monthly_savings=round(savings, 2),
            sources=sources,
        )
