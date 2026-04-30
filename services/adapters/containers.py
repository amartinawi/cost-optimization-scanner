"""Multi-source adapter for container services (ECS, EKS, ECR) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.containers import get_container_services_analysis, get_enhanced_container_checks


class ContainersModule(BaseServiceModule):
    key: str = "containers"
    cli_aliases: tuple[str, ...] = ("containers",)
    display_name: str = "Containers"

    def required_clients(self) -> tuple[str, ...]:
        return ("ecs", "eks", "ecr")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/containers.py] Containers module active")

        container_data = get_container_services_analysis(ctx)
        enhanced_result = get_enhanced_container_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        savings = 0.0
        for rec in enhanced_recs:
            savings_str = rec.get("EstimatedSavings", "")
            recommendation = rec.get("Recommendation", "").lower()
            if "spot instances" in savings_str or "spot" in recommendation:
                savings += 150
            elif "rightsizing" in savings_str.lower() or "rightsize" in recommendation:
                savings += 75
            elif "lifecycle" in savings_str.lower() or "lifecycle" in recommendation:
                savings += 25
            elif "unused" in recommendation or "empty" in recommendation:
                savings += 100
            elif "over-provisioned" in recommendation or "over provisioned" in recommendation:
                savings += 60

        return ServiceFindings(
            service_name="Containers",
            total_recommendations=len(enhanced_recs),
            total_monthly_savings=savings,
            sources={
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            extras={
                "service_counts": {
                    "ecs_clusters": container_data.get("ecs", {}).get("total_clusters", 0),
                    "eks_clusters": container_data.get("eks", {}).get("total_clusters", 0),
                    "ecr_repositories": container_data.get("ecr", {}).get("total_repositories", 0),
                    "ecs_services": container_data.get("ecs", {}).get("total_services", 0),
                }
            },
        )
