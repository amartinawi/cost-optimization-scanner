"""Live-pricing adapter for WorkSpaces."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.workspaces import (
    WORKSPACE_BUNDLE_MONTHLY,
    WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_workspaces_checks,
)

# AlwaysOn→AutoStop savings factor: conservative 30% midpoint of typical
# workday utilization deltas. AutoStop bills per-hour + monthly fee, so
# actual savings depend on user-login patterns the adapter doesn't measure.
_AUTOSTOP_SAVINGS_FACTOR: float = 0.30


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
        # AWS WorkSpaces Pricing API uses the `bundle` filter (not `instanceType`),
        # so the generic get_instance_monthly_price() lookup is structurally
        # dead. Use the AWS-published bundle price table in
        # WORKSPACE_BUNDLE_MONTHLY directly. The _AUTOSTOP_SAVINGS_FACTOR
        # constant declared at module level captures the delta midpoint.
        savings = 0.0
        for rec in recs:
            compute_type = rec.get("ComputeType", "")
            if not compute_type:
                props = rec.get("WorkspaceProperties", {})
                compute_type = props.get("ComputeTypeName", "STANDARD")
            savings_amount = rec.get("EstimatedSavingsAmount")
            if savings_amount and isinstance(savings_amount, (int, float)) and savings_amount > 0:
                # Shim already computed concrete bundle-delta savings.
                savings += float(savings_amount)
            else:
                bundle_monthly = WORKSPACE_BUNDLE_MONTHLY.get(compute_type.upper(), 0.0)
                if bundle_monthly > 0:
                    savings += bundle_monthly * ctx.pricing_multiplier * _AUTOSTOP_SAVINGS_FACTOR
                # else: unknown bundle; skip rather than fabricate $35.

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="WorkSpaces",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
        )
