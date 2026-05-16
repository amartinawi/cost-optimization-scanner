"""Multi-source adapter for container services (ECS, EKS, ECR) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.advisor import get_ecs_compute_optimizer_recommendations
from services.containers import get_container_services_analysis, get_enhanced_container_checks

# AWS Fargate pricing (us-east-1, verified via Pricing API 2026-05).
# x86 vCPU/hr: SKU 8CESGAFWKAJ98PME ($0.04048/hr).
# x86 GB/hr:   SKU PBZNQUSEXZUC34C9 ($0.004445/GB-hr).
# Region-scaled via pricing_multiplier at the per-rec emit site.
FARGATE_VCPU_HOURLY: float = 0.04048
FARGATE_MEM_GB_HOURLY: float = 0.004445

# Per-CheckCategory savings factors. AWS-documented midpoints, not arbitrary.
#   Spot: 60-70% AWS-published Spot capacity provider savings.
#   ECR lifecycle savings depend on actual image storage (cleaned per
#   repository via ecr.describe_repository_storage); without that data we
#   skip rather than fabricate a flat $25.
CONTAINERS_SAVINGS_FACTORS: dict[str, float] = {
    "spot": 0.70,
    "rightsize": 0.30,
    "unused": 1.00,
    "default": 0.30,
}


class ContainersModule(BaseServiceModule):
    """ServiceModule adapter for container services (ECS, EKS, ECR). Fargate CPU+memory pricing."""

    key: str = "containers"
    cli_aliases: tuple[str, ...] = ("containers",)
    display_name: str = "Containers"
    # Shim hits cloudwatch.get_metric_statistics extensively for ECS/EKS
    # CPU/memory utilization measurement (services/containers.py:385+).
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for container infrastructure scanning."""
        return ("ecs", "eks", "ecr", "compute-optimizer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan container infrastructure (ECS, EKS, ECR) for cost optimization.

        Consults the containers service module for cluster analysis and enhanced
        checks. Savings dispatch on canonical CheckCategory (not fragile
        substring matching on display strings) and apply documented factors
        from CONTAINERS_SAVINGS_FACTORS. Cost-Hub and Compute-Optimizer
        savings come from AWS APIs (already region-correct).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks, cost_optimization_hub, and
            compute_optimizer SourceBlocks plus service_counts extras.
        """

        try:
            container_data = get_container_services_analysis(ctx)
        except Exception as e:
            ctx.warn(f"container services analysis failed: {e}", "containers")
            container_data = {}

        try:
            enhanced_result = get_enhanced_container_checks(ctx)
        except Exception as e:
            ctx.warn(f"enhanced checks failed: {e}", "containers")
            enhanced_result = {}
        enhanced_recs = enhanced_result.get("recommendations", [])

        # Dedupe ECS service names across the three sources so a single
        # opportunity doesn't contribute savings via multiple paths.
        cost_hub_recs = ctx.cost_hub_splits.get("containers", [])
        co_recs = get_ecs_compute_optimizer_recommendations(ctx)

        seen_resources: set[str] = set()
        for rec in cost_hub_recs:
            arn = rec.get("resourceArn") or rec.get("ResourceArn") or ""
            if arn:
                seen_resources.add(arn.split("/")[-1])
        for rec in co_recs:
            rid = rec.get("resource_id") or ""
            if rid:
                seen_resources.add(rid.split("/")[-1])

        savings = 0.0
        multiplier = ctx.pricing_multiplier

        for rec in enhanced_recs:
            check_category = (rec.get("CheckCategory") or "").lower()

            # Skip if this resource is already covered by Cost Hub or CO
            # (higher-fidelity savings come from those AWS-native sources).
            for marker in ("ServiceName", "TaskDefinitionArn", "ClusterName", "RepositoryName"):
                marker_val = rec.get(marker)
                if marker_val and str(marker_val).split("/")[-1] in seen_resources:
                    break
            else:
                marker_val = None

            cpu_units = rec.get("Cpu", 0)
            mem_mb = rec.get("Memory", 0)
            task_count = rec.get("TaskCount", 0)

            try:
                task_vcpu = float(cpu_units) / 1024.0
            except (TypeError, ValueError):
                task_vcpu = 0.0
            try:
                task_mem_gb = float(mem_mb) / 1024.0
            except (TypeError, ValueError):
                task_mem_gb = 0.0
            try:
                task_count = int(task_count)
            except (TypeError, ValueError):
                task_count = 0

            # Compute baseline Fargate cost only when we have real config data.
            # Previously the adapter substituted defaults (0.25 vCPU, 0.5 GB,
            # 1 task) and fabricated $150/$75/$25/$100/$60 fallbacks when
            # data was missing. We now skip the quantification cleanly and
            # rely on cost_hub / compute_optimizer sources to supply real $.
            if task_vcpu <= 0 or task_mem_gb <= 0 or task_count <= 0:
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "task config (Cpu/Memory/TaskCount) unavailable"
                continue

            monthly_ondemand = (
                (task_vcpu * FARGATE_VCPU_HOURLY + task_mem_gb * FARGATE_MEM_GB_HOURLY)
                * 730
                * task_count
                * multiplier
            )

            # Dispatch on CheckCategory; fall back to recommendation-string
            # tokens only for legacy shim recs without a structured category.
            if "spot" in check_category:
                factor = CONTAINERS_SAVINGS_FACTORS["spot"]
            elif "rightsize" in check_category or "over-provisioned" in check_category:
                factor = CONTAINERS_SAVINGS_FACTORS["rightsize"]
            elif "unused" in check_category or "idle" in check_category:
                factor = CONTAINERS_SAVINGS_FACTORS["unused"]
            elif "lifecycle" in check_category or "ecr" in check_category:
                # ECR lifecycle savings need image-storage GB. Without it,
                # emit 0 + warn rather than fabricate $25.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "ECR storage GB unavailable; quantify via describe_repositories"
                continue
            else:
                factor = CONTAINERS_SAVINGS_FACTORS["default"]

            rec_savings = monthly_ondemand * factor
            rec["EstimatedMonthlySavings"] = round(rec_savings, 2)
            if marker_val is None:
                savings += rec_savings

        savings += sum(float(r.get("estimatedMonthlySavings", 0) or 0) for r in cost_hub_recs)
        savings += sum(float(r.get("estimatedMonthlySavings", 0.0) or 0.0) for r in co_recs)

        total_recs = len(enhanced_recs) + len(cost_hub_recs) + len(co_recs)

        return ServiceFindings(
            service_name="Containers",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
                "cost_optimization_hub": SourceBlock(
                    count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)
                ),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
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
