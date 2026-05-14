"""Athena cost optimization checks.

Extracted from CostOptimizer.get_enhanced_athena_checks() as a free function.
This module will later become AthenaModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/athena.py] Athena module active")

ATHENA_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "query_optimization": {
        "title": "Optimize Athena Query Costs",
        "description": "Partition data, use columnar formats, and compress data to reduce scan costs.",
        "action": "Implement partitioning and use Parquet/ORC formats",
    }
}


def get_enhanced_athena_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Athena cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {"workgroup_optimization": [], "query_results": []}

    try:
        athena = ctx.client("athena")
        response = athena.list_work_groups()
        for wg in response.get("WorkGroups", []):
            wg_name = wg.get("Name")

            try:
                wg_details = athena.get_work_group(WorkGroup=wg_name)
                config = wg_details.get("WorkGroup", {}).get("Configuration", {})

                result_config = config.get("ResultConfiguration", {})
                output_location = result_config.get("OutputLocation", "")

                # Athena Query Results (lifecycle on output bucket) and Workgroup
                # Optimization (scan-limit) findings removed: each emitted vague
                # "Reduce S3 storage costs" / "Prevent runaway query costs" without
                # concrete per-workgroup quantification.
                _ = (output_location, config)
            except Exception:
                continue
    except Exception as e:
        ctx.warn(f"Could not analyze Athena resources: {e}", "athena")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, "checks": checks}
