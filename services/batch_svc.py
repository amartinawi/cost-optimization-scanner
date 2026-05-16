"""Batch cost optimization checks.

Extracted from CostOptimizer.get_enhanced_batch_checks() as a free function.
This module will later become BatchModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

BATCH_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "compute_optimization": {
        "title": "Optimize Batch Compute Environments",
        "description": "Use Spot instances and Fargate Spot for fault-tolerant batch workloads.",
        "action": "Enable Spot instances for 60-90% cost savings",
    }
}


def get_enhanced_batch_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Batch cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {"compute_environments": [], "job_definitions": []}

    try:
        paginator = ctx.client("batch").get_paginator("describe_compute_environments")
        for page in paginator.paginate():
            for ce in page.get("computeEnvironments", []):
                ce_name = ce.get("computeEnvironmentName")
                ce_type = ce.get("type")
                state = ce.get("state")

                if state == "ENABLED":
                    compute_resources = ce.get("computeResources", {})
                    allocation_strategy = compute_resources.get("allocationStrategy", "BEST_FIT")
                    instance_types = compute_resources.get("instanceTypes", [])

                    is_fargate = ce_type == "MANAGED" and any(t.upper() == "FARGATE" for t in instance_types)

                    if is_fargate:
                        if allocation_strategy != "SPOT_CAPACITY_OPTIMIZED":
                            checks["compute_environments"].append(
                                {
                                    "ComputeEnvironmentName": ce_name,
                                    "Type": ce_type,
                                    "ComputeType": "FARGATE",
                                    "AllocationStrategy": allocation_strategy,
                                    "Recommendation": "Use Fargate Spot for fault-tolerant batch workloads",
                                    "EstimatedSavings": "70% with Fargate Spot",
                                    "CheckCategory": "Batch Fargate Spot Optimization",
                                }
                            )
                    else:
                        if allocation_strategy != "SPOT_CAPACITY_OPTIMIZED":
                            checks["compute_environments"].append(
                                {
                                    "ComputeEnvironmentName": ce_name,
                                    "Type": ce_type,
                                    "ComputeType": "EC2",
                                    "AllocationStrategy": allocation_strategy,
                                    "Recommendation": "Use SPOT_CAPACITY_OPTIMIZED for fault-tolerant workloads",
                                    "EstimatedSavings": "60-90% with Spot instances",
                                    "CheckCategory": "Batch Spot Optimization",
                                }
                            )

                        has_graviton = any("6g" in inst or "7g" in inst for inst in instance_types)
                        if not has_graviton and instance_types:
                            checks["compute_environments"].append(
                                {
                                    "ComputeEnvironmentName": ce_name,
                                    "InstanceTypes": instance_types,
                                    "Recommendation": "Consider Graviton instances for better price-performance",
                                    "EstimatedSavings": "20-40% cost reduction",
                                    "CheckCategory": "Batch Graviton Migration",
                                }
                            )

        try:
            job_paginator = ctx.client("batch").get_paginator("describe_job_definitions")
            for page in job_paginator.paginate(status="ACTIVE"):
                for job_def in page.get("jobDefinitions", []):
                    job_name = job_def.get("jobDefinitionName")
                    container_props = job_def.get("containerProperties", {})
                    vcpus = container_props.get("vcpus", 0)
                    memory = container_props.get("memory", 0)

                    if vcpus > 8 or memory > 16384:
                        checks["job_definitions"].append(
                            {
                                "JobDefinitionName": job_name,
                                "VCpus": vcpus,
                                "Memory": f"{memory} MB",
                                "Recommendation": "Large resource allocation - verify job requirements",
                                "EstimatedSavings": "Rightsize based on actual usage",
                                "CheckCategory": "Batch Job Rightsizing",
                            }
                        )
        except Exception:
            pass
    except Exception as e:
        ctx.warn(f"Could not analyze Batch resources: {e}", "batch")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
