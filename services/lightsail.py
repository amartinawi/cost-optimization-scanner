"""Lightsail cost optimization checks.

Extracted from CostOptimizer.get_enhanced_lightsail_checks() as a free function.
This module will later become LightsailModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/lightsail.py] Lightsail module active")

LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "idle_instances": {
        "title": "Delete Idle Lightsail Instances",
        "description": "Stopped Lightsail instances still incur charges. Delete unused instances.",
        "action": "Delete stopped instances or restart if needed",
    }
}

_BUNDLE_COSTS: dict[str, float] = {
    "nano_2_0": 3.50,
    "micro_2_0": 6.86,
    "small_2_0": 13.72,
    "medium_2_0": 27.45,
    "large_2_0": 54.90,
    "xlarge_2_0": 80.00,
    "2xlarge_2_0": 160.00,
}

_DEFAULT_BUNDLE_COST = 20.00


def get_lightsail_bundle_cost(bundle_id: str) -> float:
    """Get estimated monthly cost for Lightsail bundle."""
    return _BUNDLE_COSTS.get(bundle_id, _DEFAULT_BUNDLE_COST)


def get_enhanced_lightsail_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Lightsail cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "idle_instances": [],
        "oversized_instances": [],
        "unused_static_ips": [],
        "load_balancer_optimization": [],
        "database_optimization": [],
    }

    try:
        lightsail = ctx.client("lightsail")
        paginator = lightsail.get_paginator("get_instances")
        instances: list[dict[str, Any]] = []
        for page in paginator.paginate():
            instances.extend(page.get("instances", []))

        for instance in instances:
            instance_name = instance.get("name")
            instance_state = instance.get("state", {}).get("name")
            bundle_id: str = instance.get("bundleId") or "medium_2_0"

            if instance_state == "stopped":
                checks["idle_instances"].append(
                    {
                        "InstanceName": instance_name,
                        "State": instance_state,
                        "BundleId": bundle_id,
                        "Recommendation": "Delete stopped Lightsail instance to eliminate costs",
                        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id):.2f}/month",
                        "CheckCategory": "Idle Resource Cleanup",
                    }
                )

            if instance_state == "running" and ("xlarge" in bundle_id.lower() or "large" in bundle_id.lower()):
                checks["oversized_instances"].append(
                    {
                        "InstanceName": instance_name,
                        "BundleId": bundle_id,
                        "State": instance_state,
                        "Recommendation": (
                            "Review instance utilization - consider downsizing if CPU/memory usage is consistently low"
                        ),
                        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id) * 0.3:.2f}/month potential",
                        "CheckCategory": "Instance Rightsizing",
                        "Note": ("Recommendation based on instance size - verify actual utilization before downsizing"),
                    }
                )

        static_ips_response = lightsail.get_static_ips()
        static_ips = static_ips_response.get("staticIps", [])

        for static_ip in static_ips:
            if not static_ip.get("attachedTo"):
                checks["unused_static_ips"].append(
                    {
                        "StaticIpName": static_ip.get("name"),
                        "IpAddress": static_ip.get("ipAddress"),
                        "Recommendation": "Release unused static IP to avoid charges",
                        "EstimatedSavings": "$3.65/month",
                        "CheckCategory": "Unused Resource Cleanup",
                    }
                )

    except Exception as e:
        ctx.warn(f"Could not analyze Lightsail resources: {e}", "lightsail")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
