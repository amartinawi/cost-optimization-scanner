"""Athena cost optimization checks.

Extracted from CostOptimizer.get_enhanced_athena_checks() as a free function.
This module will later become AthenaModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

ATHENA_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "query_optimization": {
        "title": "Optimize Athena Query Costs",
        "description": "Partition data, use columnar formats, and compress data to reduce scan costs.",
        "action": "Implement partitioning and use Parquet/ORC formats",
    }
}


def get_enhanced_athena_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Athena cost optimization checks.

    Emits one rec per ENABLED workgroup carrying ``WorkGroup`` so the adapter's
    CloudWatch ``ProcessedBytes`` pricing loop can price the partitioning /
    columnar-format opportunity (athena C1 — the ``checks`` dict was previously
    initialized and never appended to, so the tab was permanently empty).
    """
    checks: dict[str, list[dict[str, Any]]] = {"workgroup_optimization": []}

    try:
        athena = ctx.client("athena")
        response = athena.list_work_groups()
        for wg in response.get("WorkGroups", []):
            wg_name = wg.get("Name")
            state = wg.get("State", "ENABLED")
            if not wg_name or state != "ENABLED":
                continue
            checks["workgroup_optimization"].append(
                {
                    "WorkGroup": wg_name,
                    "State": state,
                    "Recommendation": (
                        "Reduce Athena scan costs via partitioning + columnar formats (Parquet/ORC) + compression"
                    ),
                    "EstimatedSavings": ("Up to 75% scan-cost reduction (priced from CW ProcessedBytes)"),
                    "CheckCategory": "Workgroup Scan Optimization",
                }
            )
    except Exception as e:
        ctx.warn(f"Could not analyze Athena resources: {e}", "athena")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, "checks": checks}
