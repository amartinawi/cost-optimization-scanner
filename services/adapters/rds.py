"""Multi-source adapter for RDS with Compute Optimizer and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.advisor import get_rds_backup_actuals
from services.rds import (
    RDS_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_rds_checks,
    get_rds_compute_optimizer_recommendations,
    get_rds_instance_count,
)
from services.commitment_coverage import demote_coh_by_commitment, demote_covered_in_place
from services.rds_logic import normalize_rds_arn, resolve_rds_findings

logger = logging.getLogger(__name__)


def _coh_is_renderable(rec: dict[str, Any]) -> bool:
    """Filter Cost Optimization Hub recs down to ones the RDS tab should render.

    Reserved-Instance / Savings-Plan purchase recommendations are routed to the
    commitment_analysis tab by the orchestrator and must not be re-counted here;
    N/A-resource rows carry no concrete instance. Everything else (rightsizing,
    idle, storage findings for RdsDbInstance / RdsDbCluster) is renderable.
    """
    if rec.get("actionType") == "PurchaseReservedInstances":
        return False
    if rec.get("actionType") == "PurchaseSavingsPlans":
        return False
    if rec.get("resourceId") == "N/A":
        return False
    return True


class RdsModule(BaseServiceModule):
    """ServiceModule adapter for RDS. Multi-source savings strategy."""

    key: str = "rds"
    cli_aliases: tuple[str, ...] = ("rds",)
    display_name: str = "RDS"
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for RDS scanning."""
        return ("rds", "compute-optimizer", "cloudwatch", "ce")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan RDS instances for cost optimization opportunities.

        Consults Compute Optimizer and enhanced RDS checks, then de-duplicates
        across sources so that only one single-remediation finding survives per
        DB instance (authority Compute Optimizer > heuristic). The surviving
        recommendations are the ones emitted, so the rendered cards, the
        recommendation count, and the savings total all agree — see
        :func:`services.rds_logic.resolve_rds_findings`. Reserved-Instance recs
        are kept for display but excluded from the savings total.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with compute_optimizer and enhanced_checks sources.
        """
        logger.debug("RDS adapter scan starting")

        co_raw: list[dict[str, Any]] = []
        try:
            co_raw = get_rds_compute_optimizer_recommendations(ctx)
        except Exception as e:
            # Permission / opt-in errors are classified inside the advisor and
            # recorded on ctx; this guard only catches unexpected failures.
            ctx.warn(f"[rds] Compute Optimizer check failed: {e}", service="rds")

        # The advisor returns a synthetic $0 "enable Compute Optimizer" placeholder
        # when CO is not opted in. That is an informational signal, not a cost
        # recommendation — surface it as a warning instead of a $0-savings finding
        # that would inflate the recommendation count and render to nothing
        # (the renderer skips recs without a resourceArn). Mirrors EC2Module.
        if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_raw):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — RDS rightsizing recommendations "
                "from Compute Optimizer are unavailable (enable it for additional savings detection).",
                service="rds",
            )
        co_recs = [r for r in co_raw if r.get("ResourceId") != "compute-optimizer-service"]

        enhanced_recs: list[dict[str, Any]] = []
        try:
            enhanced_result = get_enhanced_rds_checks(
                ctx, ctx.pricing_multiplier, ctx.old_snapshot_days, ctx.fast_mode
            )
            enhanced_recs = enhanced_result.get("recommendations", [])
        except Exception as e:
            ctx.warn(f"[rds] enhanced checks failed: {e}", service="rds")

        rds_counts: dict[str, int] = {}
        try:
            rds_counts = get_rds_instance_count(ctx)
        except Exception as e:
            ctx.warn(f"[rds] instance count failed: {e}", service="rds")

        # Cost Optimization Hub re-surfaces RDS rightsizing/idle findings the
        # orchestrator bucketed into ctx.cost_hub_splits["rds"]. Consume them as
        # the authoritative source: a DB covered by CoH suppresses that DB's
        # Compute Optimizer and heuristic findings (avoids double-counting).
        coh_recs = [
            r for r in getattr(ctx, "cost_hub_splits", {}).get("rds", []) if _coh_is_renderable(r)
        ]

        # Cap the snapshot upper-bound savings at the actual billed backup spend
        # (Cost Explorer, last complete month). Skipped in fast_mode to avoid the
        # extra paid CE call. Empty result leaves the upper bounds untouched.
        backup_actuals: dict[str, float] = {}
        if not ctx.fast_mode:
            try:
                backup_actuals = get_rds_backup_actuals(ctx)
            except Exception as e:
                ctx.warn(f"[rds] backup actuals lookup failed: {e}", service="rds")

        coh_kept, co_kept, enhanced_kept, savings, total_recs = resolve_rds_findings(
            co_recs, enhanced_recs, coh_recs=coh_recs, backup_actuals=backup_actuals
        )

        # Active-commitment demotion: a DB instance already covered by an RDS
        # Reserved DB Instance bills the reservation regardless of rightsizing,
        # so its on-demand CoH figure is not realizable. Demote covered CoH recs
        # to advisory (Counted=False) and remove their gross from the headline.
        # resolve_rds_findings already suppressed the CO/heuristic recs for these
        # DBs (via coh_keys over ALL coh_recs), so none re-enters counted here.
        # Empty coverage → all counted (no change for un-reserved accounts).
        coverage = getattr(ctx, "commitment_coverage", None)

        def _coh_gross(r: dict[str, Any]) -> float:
            return float(r.get("estimatedMonthlySavings", 0.0) or 0.0)

        # NOTE: a CoH rec carries only `dbInstanceClass` — no engine — so this
        # match is engine-agnostic (family-only). RDS RIs ARE engine-scoped, so
        # this can over-demote (e.g. an aurora-postgresql RI flagging a sqlserver
        # instance of the same family). That errs toward under-reporting, never
        # toward a phantom saving. The enhanced_checks gate below, which does see
        # the engine, matches on (family, engine).
        coh_counted, coh_demoted = demote_coh_by_commitment(coh_kept, coverage, "rds", _coh_gross)
        savings -= sum(_coh_gross(r) for r in coh_demoted)
        total_recs -= len(coh_demoted)
        coh_kept = coh_counted + coh_demoted

        # Same gate for the locally-derived levers: demote_coh_by_commitment only
        # sees CoH recs, so an instance-rightsizing enhanced check against an
        # RI-covered DB would otherwise be counted at full on-demand basis.
        # Snapshot/storage recs carry no instance class and pass through untouched
        # (backup storage is not reservation-covered).
        enhanced_removed = demote_covered_in_place(
            enhanced_kept,
            coverage,
            "rds",
            lambda r: str(r.get("DBInstanceClass") or r.get("InstanceClass") or ""),
            engine_of=lambda r: str(r.get("Engine") or ""),
        )
        savings -= enhanced_removed

        # Cross-adapter dedup (rds H1 / aurora H3). Publish the normalized ids of
        # the DB instances this tab actually counts (Cost Optimization Hub +
        # Compute Optimizer) so the Aurora adapter — which runs after RDS and
        # would otherwise re-count provisioned Aurora members via its own
        # heuristic rightsizing/Graviton levers — can suppress those duplicates.
        # Single owner: RDS (authority CoH > CO) outranks the Aurora heuristic.
        covered_ids: set[str] = set()
        for r in coh_kept + co_kept:
            nid = normalize_rds_arn(r.get("resourceArn") or r.get("resourceId") or "")
            if nid:
                covered_ids.add(nid)
        existing = getattr(ctx, "rds_covered_instance_ids", None)
        ctx.rds_covered_instance_ids = (existing | covered_ids) if isinstance(existing, set) else covered_ids

        sources = {
            "compute_optimizer": SourceBlock(count=len(co_kept), recommendations=tuple(co_kept)),
            "enhanced_checks": SourceBlock(count=len(enhanced_kept), recommendations=tuple(enhanced_kept)),
        }
        if coh_kept:
            sources["cost_optimization_hub"] = SourceBlock(
                count=len(coh_kept), recommendations=tuple(coh_kept)
            )

        return ServiceFindings(
            service_name="RDS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=RDS_OPTIMIZATION_DESCRIPTIONS,
            extras={"instance_counts": rds_counts},
        )
