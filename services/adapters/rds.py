"""Multi-source adapter for RDS with Compute Optimizer and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.rds import (
    RDS_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_rds_checks,
    get_rds_compute_optimizer_recommendations,
    get_rds_instance_count,
)
from services.rds_logic import resolve_rds_findings

logger = logging.getLogger(__name__)


class RdsModule(BaseServiceModule):
    """ServiceModule adapter for RDS. Multi-source savings strategy."""

    key: str = "rds"
    cli_aliases: tuple[str, ...] = ("rds",)
    display_name: str = "RDS"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for RDS scanning."""
        return ("rds", "compute-optimizer")

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
            enhanced_result = get_enhanced_rds_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days)
            enhanced_recs = enhanced_result.get("recommendations", [])
        except Exception as e:
            ctx.warn(f"[rds] enhanced checks failed: {e}", service="rds")

        rds_counts: dict[str, int] = {}
        try:
            rds_counts = get_rds_instance_count(ctx)
        except Exception as e:
            ctx.warn(f"[rds] instance count failed: {e}", service="rds")

        # Cross-source de-duplication. CoH consumption is wired in a later step;
        # for now coh_recs is empty and the resolver dedups CO vs heuristics.
        _coh_kept, co_kept, enhanced_kept, savings, total_recs = resolve_rds_findings(
            co_recs, enhanced_recs
        )

        return ServiceFindings(
            service_name="RDS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "compute_optimizer": SourceBlock(count=len(co_kept), recommendations=tuple(co_kept)),
                "enhanced_checks": SourceBlock(count=len(enhanced_kept), recommendations=tuple(enhanced_kept)),
            },
            optimization_descriptions=RDS_OPTIMIZATION_DESCRIPTIONS,
            extras={"instance_counts": rds_counts},
        )
