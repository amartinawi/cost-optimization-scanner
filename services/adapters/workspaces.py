"""Live-pricing adapter for WorkSpaces."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.workspaces import (
    WORKSPACE_BUNDLE_MAP,
    WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_workspaces_checks,
)


class WorkspacesModule(BaseServiceModule):
    """ServiceModule adapter for WorkSpaces. Live-pricing savings strategy."""

    key: str = "workspaces"
    cli_aliases: tuple[str, ...] = ("workspaces",)
    display_name: str = "WorkSpaces"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for WorkSpaces scanning."""
        return ("workspaces",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan WorkSpaces virtual desktops for cost optimization opportunities.

        Consults enhanced WorkSpaces checks. Savings calculated via live
        pricing engine when available, with flat-rate fallback targeting
        AlwaysOn-to-AutoStop migration.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/workspaces.py] WorkSpaces module active")
        result = get_enhanced_workspaces_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        for rec in recs:
            compute_type = rec.get("ComputeType", "")
            if not compute_type:
                props = rec.get("WorkspaceProperties", {})
                compute_type = props.get("ComputeTypeName", "STANDARD")
            bundle_id = WORKSPACE_BUNDLE_MAP.get(compute_type, "2")
            savings_amount = rec.get("EstimatedSavingsAmount")
            if savings_amount and isinstance(savings_amount, (int, float)) and savings_amount > 0:
                savings += float(savings_amount)
            elif ctx.pricing_engine and bundle_id:
                monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonWorkSpaces", bundle_id)
                savings += monthly * 0.40 if monthly > 0 else 35.0 * ctx.pricing_multiplier
            else:
                savings += 35.0 * ctx.pricing_multiplier

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="WorkSpaces",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
        )
