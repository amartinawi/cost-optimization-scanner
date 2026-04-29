"""WorkSpaces cost optimization checks.

Extracted from CostOptimizer.get_enhanced_workspaces_checks() as a free function.
This module will later become WorkspacesModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/workspaces.py] WorkSpaces module active")

WORKSPACES_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "billing_mode_optimization": {
        "title": "Optimize WorkSpaces Billing Mode",
        "description": "Use AUTO_STOP mode for occasional users instead of ALWAYS_ON.",
        "action": "Switch to AUTO_STOP billing mode",
    }
}


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

    except Exception as e:
        ctx.warn(f"Could not analyze WorkSpaces resources: {e}", "workspaces")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
