"""Glue cost optimization checks.

Extracted from CostOptimizer.get_enhanced_glue_checks() as a free function.
This module will later become GlueModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

GLUE_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "job_optimization": {
        "title": "Optimize Glue Job Configuration",
        "description": "Right-size DPU allocation and enable auto-scaling for variable workloads.",
        "action": "Review DPU usage and enable Glue auto-scaling",
    }
}


def get_enhanced_glue_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Glue cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {"job_rightsizing": [], "dev_endpoints": [], "crawler_optimization": []}

    glue = ctx.client("glue")

    # Each AWS API is wrapped in its own try/except so an AccessDenied (or
    # throttle) on one call is classified via record_aws_error and never
    # silently aborts the remaining calls (glue L1).
    try:
        paginator = glue.get_paginator("get_jobs")
        for page in paginator.paginate():
            for job in page.get("Jobs", []):
                job_name = job.get("Name")
                max_capacity = job.get("MaxCapacity", 0)
                worker_type = job.get("WorkerType")
                number_of_workers = job.get("NumberOfWorkers", 0)

                if max_capacity > 10 or number_of_workers > 10:
                    checks["job_rightsizing"].append(
                        {
                            "JobName": job_name,
                            "MaxCapacity": max_capacity,
                            "WorkerType": worker_type,
                            "NumberOfWorkers": number_of_workers,
                            "Recommendation": "Review DPU allocation - enable auto-scaling",
                            "EstimatedSavings": "20-40% with auto-scaling",
                            "CheckCategory": "Glue Job Rightsizing",
                        }
                    )
    except Exception as e:
        record_aws_error(ctx, e, service="glue", context="glue:GetJobs")

    try:
        dev_endpoints = glue.get_dev_endpoints()
        for endpoint in dev_endpoints.get("DevEndpoints", []):
            endpoint_name = endpoint.get("EndpointName")
            status = endpoint.get("Status")

            if status == "READY":
                # A READY dev endpoint bills continuously at the DPU-hour rate.
                # Carry its own provisioned DPU footprint (WorkerType/
                # NumberOfWorkers for modern endpoints, legacy NumberOfNodes
                # otherwise) so the adapter prices the *actual* allocation
                # instead of a hardcoded flat string (glue H2). The hardcoded
                # "$316/month per endpoint" string is removed — the adapter
                # single-sources the dollar from the DPU footprint.
                checks["dev_endpoints"].append(
                    {
                        "EndpointName": endpoint_name,
                        "Status": status,
                        "WorkerType": endpoint.get("WorkerType"),
                        "NumberOfWorkers": endpoint.get("NumberOfWorkers"),
                        "NumberOfNodes": endpoint.get("NumberOfNodes"),
                        "Recommendation": (
                            "Dev endpoint billed continuously at $0.44/DPU-hour - "
                            "delete when not in use"
                        ),
                        "CheckCategory": "Glue Dev Endpoints",
                    }
                )
    except Exception as e:
        record_aws_error(ctx, e, service="glue", context="glue:GetDevEndpoints")

    try:
        paginator = glue.get_paginator("get_crawlers")
        for page in paginator.paginate():
            for crawler in page.get("Crawlers", []):
                crawler_name = crawler.get("Name")
                schedule = crawler.get("Schedule", {}).get("ScheduleExpression")

                # Crawler optimization finding removed: "Variable based on frequency" with
                # no concrete per-crawler quantification.
                _ = (crawler_name, schedule)
    except Exception as e:
        record_aws_error(ctx, e, service="glue", context="glue:GetCrawlers")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
