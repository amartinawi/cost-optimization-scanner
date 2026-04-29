"""Glue cost optimization checks.

Extracted from CostOptimizer.get_enhanced_glue_checks() as a free function.
This module will later become GlueModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/glue.py] Glue module active")

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

    try:
        glue = ctx.client("glue")

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

        dev_endpoints = glue.get_dev_endpoints()
        for endpoint in dev_endpoints.get("DevEndpoints", []):
            endpoint_name = endpoint.get("EndpointName")
            status = endpoint.get("Status")

            if status == "READY":
                checks["dev_endpoints"].append(
                    {
                        "EndpointName": endpoint_name,
                        "Status": status,
                        "Recommendation": "Dev endpoints cost $0.44/hour - delete when not in use",
                        "EstimatedSavings": "$316/month per endpoint",
                        "CheckCategory": "Glue Dev Endpoints",
                    }
                )

        paginator = glue.get_paginator("get_crawlers")
        for page in paginator.paginate():
            for crawler in page.get("Crawlers", []):
                crawler_name = crawler.get("Name")
                schedule = crawler.get("Schedule", {}).get("ScheduleExpression")

                if schedule and "cron" in schedule.lower():
                    checks["crawler_optimization"].append(
                        {
                            "CrawlerName": crawler_name,
                            "Schedule": schedule,
                            "Recommendation": "Review crawler frequency - run on-demand if possible",
                            "EstimatedSavings": "Variable based on frequency",
                            "CheckCategory": "Glue Crawler Optimization",
                        }
                    )
    except Exception as e:
        ctx.warn(f"Could not analyze Glue resources: {e}", "glue")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
