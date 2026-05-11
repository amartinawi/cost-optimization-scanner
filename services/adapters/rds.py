"""Multi-source adapter for RDS with Compute Optimizer and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import compute_optimizer_savings, parse_dollar_savings
from services.rds import (
    RDS_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_rds_checks,
    get_rds_compute_optimizer_recommendations,
    get_rds_instance_count,
)

logger = logging.getLogger(__name__)


def _aggregate_rds_savings(
    co_recs: list[dict[str, Any]],
    enhanced_recs: list[dict[str, Any]],
) -> float:
    """Compute total RDS monthly savings with per-resource deduplication.

    Compute Optimizer (rightsizing) and enhanced checks (Multi-AZ disable,
    non-prod scheduling, Reserved Instances) can fire concurrently on the
    same DB instance. Their remediations are not freely additive — the user
    can only realistically pick one large remediation per instance. To
    avoid inflating the headline, this aggregator groups by ``resourceArn``
    and counts only the **maximum** savings per resource.

    Old-snapshot recs use a snapshot ARN (``arn:…:snapshot:…``) which is in
    a different namespace from DB instance ARNs, so they don't dedup against
    instance-level recs — that's the desired behaviour.
    """
    by_resource: dict[str, float] = {}
    untagged_total = 0.0

    for rec in co_recs:
        arn = rec.get("resourceArn") or ""
        amount = compute_optimizer_savings(rec)
        if arn:
            by_resource[arn] = max(by_resource.get(arn, 0.0), amount)
        else:
            untagged_total += amount

    for rec in enhanced_recs:
        arn = rec.get("resourceArn") or ""
        est = rec.get("EstimatedSavings", "")
        amount = parse_dollar_savings(est) if "$" in est else 0.0
        if arn:
            by_resource[arn] = max(by_resource.get(arn, 0.0), amount)
        else:
            untagged_total += amount

    return sum(by_resource.values()) + untagged_total


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

        Consults Compute Optimizer and enhanced RDS checks. Savings are
        aggregated per-resource (``resourceArn``) and only the maximum
        single-remediation value per DB instance is counted toward the
        headline — see :func:`_aggregate_rds_savings` for rationale.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with compute_optimizer and enhanced_checks sources.
        """
        logger.debug("RDS adapter scan starting")

        co_recs: list[dict[str, Any]] = []
        try:
            co_recs = get_rds_compute_optimizer_recommendations(ctx)
        except ClientError as ec:
            code = ec.response.get("Error", {}).get("Code", "")
            if code in ("AccessDenied", "UnauthorizedOperation", "OptInRequiredException"):
                ctx.permission_issue(
                    f"Compute Optimizer denied: {code}",
                    service="rds",
                    action="compute-optimizer:GetRDSDatabaseRecommendations",
                )
            else:
                ctx.warn(f"[rds] Compute Optimizer check failed: {ec}", service="rds")
        except Exception as e:
            ctx.warn(f"[rds] Compute Optimizer check failed: {e}", service="rds")

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

        savings = _aggregate_rds_savings(co_recs, enhanced_recs)
        total_recs = len(co_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="RDS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=RDS_OPTIMIZATION_DESCRIPTIONS,
            extras={"instance_counts": rds_counts},
        )
