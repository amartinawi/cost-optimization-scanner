"""Cost Optimization Hub adapter for Redshift (heuristic levers are $0 advisories)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
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
        coh_keys = {coh_key(r) for r in coh_recs} - {""}
        coh_total = sum(coh_savings(r) for r in coh_recs)

        # CoH is the only counted Redshift source. Every heuristic lever the shim
        # emits is either a commitment advisory (RI / Serverless Reservation) or an
        # unpriceable rightsizing hint that carries no live node price / usage
        # metric — so none is ever counted (Redshift L2: the dead per-node pricing
        # path and its REDSHIFT_SAVINGS_FACTORS were removed).
        savings = coh_total
        for rec in recs:
            category = rec.get("CheckCategory", "")

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

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        if coh_recs:
            sources["cost_optimization_hub"] = SourceBlock(count=len(coh_recs), recommendations=tuple(coh_recs))

        return ServiceFindings(
            service_name="Redshift",
            total_recommendations=len(recs) + len(coh_recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=REDSHIFT_OPTIMIZATION_DESCRIPTIONS,
        )
