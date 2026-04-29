"""QuickSight BI service cost optimization checks.

Extracted from CostOptimizer.get_enhanced_quicksight_checks() as a free function.
This module will later become QuickSightModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "spice_optimization": {
        "title": "Optimize QuickSight SPICE Usage",
        "description": "Review SPICE capacity and optimize data refresh schedules.",
        "action": "Optimize SPICE capacity and refresh schedules",
    }
}


def get_enhanced_quicksight_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced QuickSight cost optimization checks."""
    print("🔍 [services/quicksight.py] QuickSight module active")
    checks: dict[str, list[dict[str, Any]]] = {
        "spice_optimization": [],
        "user_optimization": [],
        "capacity_optimization": [],
    }

    try:
        quicksight = ctx.client("quicksight")

        subscription = quicksight.describe_account_subscription(AwsAccountId=ctx.account_id)
        if subscription.get("AccountInfo", {}).get("AccountSubscriptionStatus") != "ACCOUNT_CREATED":
            return {"recommendations": [], "checks": checks}

        namespaces_paginator = quicksight.get_paginator("list_namespaces")
        namespaces: list[dict[str, Any]] = []
        for page in namespaces_paginator.paginate(AwsAccountId=ctx.account_id):
            namespaces.extend(page.get("Namespaces", []))

        total_users = 0
        for namespace in namespaces:
            namespace_name = namespace.get("Name")
            try:
                paginator = quicksight.get_paginator("list_users")
                for page in paginator.paginate(AwsAccountId=ctx.account_id, Namespace=namespace_name):
                    total_users += len(page.get("UserList", []))
            except Exception:
                continue

        if total_users > 0:
            try:
                spice_capacity = quicksight.describe_spice_capacity(AwsAccountId=ctx.account_id)
                capacity_config = spice_capacity.get("SpiceCapacityConfiguration", {})

                used_capacity = capacity_config.get("UsedCapacityInBytes", 0) / (1024**3)
                total_capacity = capacity_config.get("TotalCapacityInBytes", 0) / (1024**3)

                if total_capacity > 0 and used_capacity < total_capacity * 0.5:
                    checks["spice_optimization"].append(
                        {
                            "UserCount": total_users,
                            "UsedCapacityGB": round(used_capacity, 2),
                            "TotalCapacityGB": round(total_capacity, 2),
                            "UtilizationPercent": round((used_capacity / total_capacity) * 100, 1),
                            "Recommendation": (
                                f"SPICE capacity underutilized"
                                f" ({round(used_capacity, 1)}/{round(total_capacity, 1)}"
                                " GB) - consider reducing"
                            ),
                            "EstimatedSavings": (
                                f"~${(total_capacity - used_capacity) * 0.25:.0f}"
                                "/month (estimate - verify SPICE pricing)"
                            ),
                            "CheckCategory": "SPICE Optimization",
                        }
                    )
            except Exception:
                pass

    except Exception as e:
        error_str = str(e)
        if "ResourceNotFoundException" in error_str and "account does not exist" in error_str:
            print("ℹ️ QuickSight is not enabled in this account - skipping QuickSight analysis")
        else:
            ctx.warn(f"Could not analyze QuickSight resources: {e}", "quicksight")

    all_recommendations: list[dict[str, Any]] = []
    for _, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
