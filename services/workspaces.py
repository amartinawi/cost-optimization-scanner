"""WorkSpaces cost optimization checks.

Extracted from CostOptimizer.get_enhanced_workspaces_checks() as a free function.
This module will later become WorkspacesModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/workspaces.py] WorkSpaces module active")

WORKSPACE_BUNDLE_MAP: dict[str, str] = {
    "VALUE": "1",
    "STANDARD": "2",
    "PERFORMANCE": "3",
    "POWER": "8",
    "POWERPRO": "19",
    "GRAPHICS": "4",
    "GRAPHICSPRO": "5",
}

WORKSPACE_BUNDLE_RANK: dict[str, int] = {
    "VALUE": 0,
    "STANDARD": 1,
    "PERFORMANCE": 2,
    "POWER": 3,
    "POWERPRO": 4,
    "GRAPHICS": 5,
    "GRAPHICSPRO": 6,
}

WORKSPACES_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "billing_mode_optimization": {
        "title": "Optimize WorkSpaces Billing Mode",
        "description": "Use AUTO_STOP mode for occasional users instead of ALWAYS_ON.",
        "action": "Switch to AUTO_STOP billing mode",
    },
    "bundle_rightsizing": {
        "title": "Rightsize WorkSpaces Bundle",
        "description": "Downgrade over-provisioned WorkSpaces bundles based on utilization.",
        "action": "Downgrade to a smaller bundle type",
    },
}


def _get_bundle_price(ctx: ScanContext, bundle_id: str) -> float:
    """Look up monthly price for a WorkSpaces bundle via PricingEngine."""
    if not ctx.pricing_engine:
        return 35.0 * ctx.pricing_multiplier
    try:
        price = ctx.pricing_engine.get_instance_monthly_price("AmazonWorkSpaces", bundle_id)
        return price if price > 0 else 35.0 * ctx.pricing_multiplier
    except Exception:
        return 35.0 * ctx.pricing_multiplier


def get_enhanced_workspaces_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced WorkSpaces cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "billing_mode_optimization": [],
        "bundle_rightsizing": [],
        "unused_workspaces": [],
    }

    try:
        workspaces = ctx.client("workspaces")
        paginator = workspaces.get_paginator("describe_workspaces")

        for page in paginator.paginate():
            ws_list = page.get("Workspaces", [])

            for workspace in ws_list:
                workspace_id = workspace.get("WorkspaceId")
                state = workspace.get("State")
                running_mode = workspace.get("WorkspaceProperties", {}).get("RunningMode")

                if state == "AVAILABLE" and running_mode == "ALWAYS_ON":
                    checks["billing_mode_optimization"].append(
                        {
                            "WorkspaceId": workspace_id,
                            "CurrentMode": running_mode,
                            "Recommendation": (
                                "Consider AUTO_STOP mode for occasional users - monitor usage patterns first"
                            ),
                            "EstimatedSavings": "$50/month potential per workspace",
                            "CheckCategory": "Billing Mode Optimization",
                            "Note": "Verify user login patterns before switching to AUTO_STOP",
                        }
                    )

                if state in ["STOPPED", "ERROR", "SUSPENDED"]:
                    checks["unused_workspaces"].append(
                        {
                            "WorkspaceId": workspace_id,
                            "State": state,
                            "RunningMode": running_mode,
                            "Recommendation": f"Workspace in {state} state - terminate if no longer needed",
                            "EstimatedSavings": "Full workspace monthly cost",
                            "CheckCategory": "Unused WorkSpaces",
                        }
                    )

                if state == "AVAILABLE":
                    props = workspace.get("WorkspaceProperties", {})
                    compute_type = props.get("ComputeTypeName", "STANDARD")
                    current_rank = WORKSPACE_BUNDLE_RANK.get(compute_type, -1)
                    if current_rank <= 0:
                        continue

                    target_type = None
                    if current_rank >= 4 and ctx.pricing_engine:
                        target_type = "STANDARD"
                    elif current_rank >= 3:
                        target_type = "PERFORMANCE" if current_rank > 2 else None

                    if target_type and target_type != compute_type:
                        current_bundle = WORKSPACE_BUNDLE_MAP.get(compute_type, "2")
                        target_bundle = WORKSPACE_BUNDLE_MAP.get(target_type, "2")
                        current_price = _get_bundle_price(ctx, current_bundle)
                        target_price = _get_bundle_price(ctx, target_bundle)
                        savings = max(current_price - target_price, 0.0)

                        if savings > 0:
                            checks["bundle_rightsizing"].append(
                                {
                                    "WorkspaceId": workspace_id,
                                    "CurrentBundle": compute_type,
                                    "RecommendedBundle": target_type,
                                    "Recommendation": (
                                        f"Downgrade from {compute_type} to {target_type} based on utilization profile"
                                    ),
                                    "EstimatedSavings": f"${savings:.2f}/month",
                                    "EstimatedSavingsAmount": savings,
                                    "CheckCategory": "Bundle Rightsizing",
                                }
                            )

    except Exception as e:
        ctx.warn(f"Could not analyze WorkSpaces resources: {e}", "workspaces")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
