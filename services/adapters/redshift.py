"""Cost Optimization Hub adapter for Redshift (heuristic levers are $0 advisories)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
from services.commitment_coverage import demote_coh_by_commitment, demote_covered_in_place
from services.redshift import REDSHIFT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_redshift_checks

# Enhanced-check categories rendered for context but whose dollar value is NOT
# summed into the Redshift headline ("advisory"). Both are commitment/reservation
# purchases whose savings are the authoritative domain of the commitment_analysis
# tab (which renders Cost Hub RI / Savings-Plan purchase recs). Counting a
# commitment buy here would double-count it against commitment_analysis and
# overstate the realizable account saving (Redshift H1/H2). Mirrors
# services/rds_logic.ADVISORY_CATEGORIES and services/dynamodb.DYNAMODB_ADVISORY_CATEGORIES.
RI_CATEGORY = "Reserved Instance Optimization"
SERVERLESS_RESERVATION_CATEGORY = "Serverless Optimization"
ADVISORY_CATEGORIES: frozenset[str] = frozenset({RI_CATEGORY, SERVERLESS_RESERVATION_CATEGORY})

# Canonical advisory line for a commitment lever — single-sourced so the string
# the card shows equals the $0 the headline counts (Redshift H2).
RI_ADVISORY_SAVINGS: str = (
    "$0.00/month — advisory: Reserved Instance / Serverless Reservation is a "
    "commitment purchase (see Commitment Analysis); excluded from rightsizing savings"
)


IDLE_CATEGORY = "Idle Cluster"

# RS-1 — the COUNTED idle-pause figure is restricted to RA3. RA3 separates
# compute from managed storage, so a paused RA3 cluster keeps paying the same
# RMS bill and the compute-only delta is exact. DC2/DS2 bundle local SSD into
# the node-hour: pausing moves that data to backup storage, and the
# free-allocation treatment of a PAUSED DC2/DS2 cluster could not be verified —
# so those render their figure without counting it.
_COUNTED_IDLE_FAMILY_PREFIX = "ra3."


def _price_idle_cluster(ctx: Any, rec: dict[str, Any]) -> tuple[float, str | None]:
    """(monthly compute $, abstain reason). A reason means: render, do not count."""
    node_type = str(rec.get("NodeType") or "")
    nodes = rec.get("NumberOfNodes")
    engine = getattr(ctx, "pricing_engine", None)
    if not node_type or not nodes or engine is None:
        return 0.0, "node type or node count unavailable"
    try:
        monthly_node = engine.get_instance_monthly_price("AmazonRedshift", node_type)
    except Exception:
        monthly_node = 0.0
    if monthly_node <= 0:
        return 0.0, f"no live Redshift SKU for {node_type}"
    total = monthly_node * int(nodes)
    if not node_type.startswith(_COUNTED_IDLE_FAMILY_PREFIX):
        return total, (
            f"{node_type} bundles local SSD into the node-hour; pausing moves that data to "
            "backup storage and the free-allocation treatment of a paused DC2/DS2 cluster is "
            "unverified"
        )
    if not rec.get("PauseEligible"):
        return total, (
            "AWS cannot pause this cluster (automated snapshots disabled, or HSM), so the "
            "saving has no action behind it"
        )
    return total, None


class RedshiftModule(BaseServiceModule):
    """ServiceModule adapter for Redshift. Cost Optimization Hub is the only
    counted source; every heuristic lever is a $0 advisory."""

    key: str = "redshift"
    cli_aliases: tuple[str, ...] = ("redshift",)
    display_name: str = "Redshift"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Redshift scanning."""
        return ("redshift",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Redshift clusters for cost optimization opportunities.

        Consults enhanced Redshift checks and Cost Optimization Hub. CoH is the
        authoritative aggregator and the only counted source: a cluster it covers
        suppresses that cluster's heuristic levers (avoids double-counting). Every
        heuristic lever is rendered as a $0 advisory (commitment buys are owned by
        Commitment Analysis; rightsizing hints carry no live node price).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks and (when present)
            cost_optimization_hub SourceBlocks.
        """
        # TODO: RA3 node types charge managed storage at $0.024/GB/month.
        # Current calculation only covers instance pricing, not RMS storage.
        result = get_enhanced_redshift_checks(ctx)
        recs = result.get("recommendations", [])

        # Cost Optimization Hub re-surfaces Redshift rightsizing/idle findings
        # the orchestrator bucketed into ctx.cost_hub_splits["redshift"]. CoH
        # is authoritative: a cluster it covers suppresses that cluster's
        # heuristic levers (SR-3 / Redshift C1).
        coh_recs = [r for r in getattr(ctx, "cost_hub_splits", {}).get("redshift", []) if is_renderable_coh_rec(r)]
        # Suppression keys span ALL CoH recs (covered or not) so a Reserved-Node
        # demotion below never re-enables the heuristic lever for that cluster.
        coh_keys = {coh_key(r) for r in coh_recs} - {""}
        # Active-commitment demotion: a cluster already covered by a Redshift
        # Reserved Node bills the reservation regardless of rightsizing, so its
        # on-demand CoH figure is not realizable — demote to advisory (Counted=
        # False). Empty coverage → all counted (no change for un-reserved accounts).
        coverage = getattr(ctx, "commitment_coverage", None)
        coh_counted, coh_advisory = demote_coh_by_commitment(coh_recs, coverage, "redshift", coh_savings)
        coh_out = coh_counted + coh_advisory
        coh_total = sum(coh_savings(r) for r in coh_counted)

        # CoH is the only counted Redshift source. Every heuristic lever the shim
        # emits is either a commitment advisory (RI / Serverless Reservation) or an
        # unpriceable rightsizing hint that carries no live node price / usage
        # metric — so none is ever counted (Redshift L2: the dead per-node pricing
        # path and its REDSHIFT_SAVINGS_FACTORS were removed).
        savings = coh_total
        for rec in recs:
            category = rec.get("CheckCategory", "")

            # RS-1 — the one counted Redshift lever. An idle provisioned cluster
            # wastes 100% of its compute spend and PAUSE recovers exactly that,
            # so no target size has to be guessed (the MSK-1 argument).
            if category == IDLE_CATEGORY and rec.get("ClusterIdentifier", "") not in coh_keys:
                total, abstain = _price_idle_cluster(ctx, rec)
                basis: dict[str, Any] = {
                    "metric": "AWS/Redshift DatabaseConnections (Maximum), dimension ClusterIdentifier",
                    "metric_window_days": rec.get("MetricWindowDays"),
                    "node_type": rec.get("NodeType"),
                    "node_count": rec.get("NumberOfNodes"),
                    "monthly_compute": round(total, 2),
                    "pause_eligibility": (
                        "AutomatedSnapshotRetentionPeriod > 0 and no HsmStatus"
                        if rec.get("PauseEligible")
                        else "not pause-eligible"
                    ),
                    "residual_risk": (
                        "DatabaseConnections counts client connections, not internal query "
                        "activity: materialized-view auto-refresh and streaming ingestion can "
                        "run without one. The action is PAUSE (reversible), not delete."
                    ),
                }
                if abstain is None:
                    rec["EstimatedMonthlySavings"] = round(total, 2)
                    rec["EstimatedSavings"] = f"${total:,.2f}/month if paused"
                    rec["Counted"] = True
                    basis["formula"] = "node monthly price x node count (RA3 compute only)"
                    basis["counted"] = True
                else:
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["Counted"] = False
                    rec["EstimatedSavings"] = f"$0.00/month - advisory: {abstain}"
                    basis["counted"] = False
                    basis["reason"] = abstain
                    if total > 0:
                        rec["PotentialMonthlySavings"] = round(total, 2)
                rec["AuditBasis"] = basis
                savings += rec["EstimatedMonthlySavings"]
                continue

            # CoH already covers this cluster → demote the heuristic lever so the
            # same saving is not counted twice (authority CoH > heuristic).
            if rec.get("ClusterIdentifier", "") in coh_keys:
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                continue

            # Redshift H1/H2 — Reserved-Instance / Serverless-Reservation levers
            # are commitment purchases owned by the Commitment Analysis tab.
            # Counting them here double-counts the commitment and overstates the
            # realizable account saving; render as an honest $0 advisory so the
            # card dollar ($0) equals the counted contribution ($0).
            if category in ADVISORY_CATEGORIES:
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                rec["EstimatedSavings"] = RI_ADVISORY_SAVINGS
                rec["AuditBasis"] = {
                    "lever": "commitment_purchase",
                    "owner": "commitment_analysis",
                    "realizable_1yr_ri_discount": (
                        "~30% (No Upfront): ra3.xlplus on-demand $1.086/hr -> RI $0.7602/hr, "
                        "us-east-1, AWS Pricing API 2026-06"
                    ),
                    "note": "retired 0.52 factor overstated the 1-yr No-Upfront RI discount ~1.7x",
                }
                continue

            # No surviving heuristic lever carries a live node price or usage
            # metric to quantify a saving → render as an honest $0 advisory so a
            # shim-supplied "potential" string (e.g. cluster_rightsizing's
            # (nodes-2)x100) never renders as a counted-looking dollar while the
            # headline counts $0.
            rec["EstimatedMonthlySavings"] = 0.0
            rec["Counted"] = False
            rec["EstimatedSavings"] = (
                "$0.00/month — advisory: no live node price / usage metric to quantify the saving"
            )

        # A cluster whose exact node type carries a Redshift Reserved Node bills
        # the reservation whether it is paused or not, so the on-demand figure is
        # not realizable. Reserved Nodes are NOT size-flexible, so the exact-type
        # match is correct here.
        savings -= demote_covered_in_place(
            recs, coverage, "redshift",
            lambda r: r.get("NodeType", ""),
            zero_keys=("EstimatedMonthlySavings",),
        )

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        if coh_out:
            sources["cost_optimization_hub"] = SourceBlock(count=len(coh_out), recommendations=tuple(coh_out))

        return ServiceFindings(
            service_name="Redshift",
            total_recommendations=len(recs) + len(coh_counted),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=REDSHIFT_OPTIMIZATION_DESCRIPTIONS,
        )
