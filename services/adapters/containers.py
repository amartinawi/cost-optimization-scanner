"""Multi-source adapter for container services (ECS, EKS, ECR) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.containers import get_container_services_analysis, get_enhanced_container_checks


class ContainersModule(BaseServiceModule):
    """ServiceModule adapter for container services (ECS, EKS, ECR). Fargate CPU+memory pricing."""

    key: str = "containers"
    cli_aliases: tuple[str, ...] = ("containers",)
    display_name: str = "Containers"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for container infrastructure scanning."""
        return ("ecs", "eks", "ecr")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan container infrastructure (ECS, EKS, ECR) for cost optimization.

        Consults the containers service module for cluster analysis and enhanced
        checks. Savings calculated via Fargate CPU+memory pricing with rec-type
        discount factors and pricing_multiplier regional adjustment.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock and service_counts extras.
        """
        print("\U0001f50d [services/adapters/containers.py] Containers module active")

        try:
            container_data = get_container_services_analysis(ctx)
        except Exception as e:
            print(f"Warning: [containers] container services analysis failed: {e}")
            container_data = {}

        try:
            enhanced_result = get_enhanced_container_checks(ctx)
        except Exception as e:
            print(f"Warning: [containers] enhanced checks failed: {e}")
            enhanced_result = {}
        enhanced_recs = enhanced_result.get("recommendations", [])

        FARGATE_VCPU_HOURLY = 0.04048
        FARGATE_MEM_GB_HOURLY = 0.004445
        # TODO: ARM/Graviton tasks are ~20% cheaper than x86. Add cpuArchitecture check
        # and ARM pricing constants for full accuracy.

        savings = 0.0
        for rec in enhanced_recs:
            savings_str = rec.get("EstimatedSavings", "")
            recommendation = rec.get("Recommendation", "").lower()

            cpu_units = rec.get("Cpu", 256)
            mem_mb = rec.get("Memory", 512)
            task_count = rec.get("TaskCount", 1)

            try:
                task_vcpu = float(cpu_units) / 1024.0
            except (TypeError, ValueError):
                task_vcpu = 0.25
            try:
                task_mem_gb = float(mem_mb) / 1024.0
            except (TypeError, ValueError):
                task_mem_gb = 0.5

            monthly_ondemand = (
                (task_vcpu * FARGATE_VCPU_HOURLY + task_mem_gb * FARGATE_MEM_GB_HOURLY)
                * 730
                * task_count
                * ctx.pricing_multiplier
            )

            if "spot" in recommendation or "spot instances" in savings_str.lower():
                savings += monthly_ondemand * 0.70 if monthly_ondemand > 0 else 150.0 * ctx.pricing_multiplier
            elif (
                "rightsizing" in savings_str.lower()
                or "rightsize" in recommendation
                or "over-provisioned" in recommendation
            ):
                savings += monthly_ondemand * 0.30 if monthly_ondemand > 0 else 75.0 * ctx.pricing_multiplier
            elif "lifecycle" in savings_str.lower() or "lifecycle" in recommendation:
                savings += 25.0 * ctx.pricing_multiplier
            elif "unused" in recommendation or "empty" in recommendation:
                savings += monthly_ondemand if monthly_ondemand > 0 else 100.0 * ctx.pricing_multiplier
            else:
                savings += monthly_ondemand * 0.30 if monthly_ondemand > 0 else 60.0 * ctx.pricing_multiplier

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
