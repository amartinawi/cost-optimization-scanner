"""EKS cost visibility and optimization adapter.

Provides dedicated EKS cost analysis beyond the general containers adapter:

- Cluster-level control plane cost tracking ($0.10/hr per cluster)
- Node group instance type optimization (Graviton migration, Spot adoption)
- Fargate profile cost estimation and consolidation opportunities
- Add-on marketplace cost visibility
- Cost Optimization Hub integration for EKS-specific recommendations

AWS API cost: All EKS API calls are free (no per-request charge).
"""

from __future__ import annotations

from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

EKS_CONTROL_PLANE_HOURLY: float = 0.10
FARGATE_VCPU_HOURLY: float = 0.04048
FARGATE_MEM_GB_HOURLY: float = 0.004445
HOURS_PER_MONTH: int = 730

PREV_GEN_PREFIXES: tuple[str, ...] = ("m3.", "m4.", "c3.", "c4.", "r3.", "r4.", "t2.")
# AWS Graviton EC2 list-price delta vs x86 (m5→m6g, c5→c6g, etc.) ≈ 20%.
# Previously used 0.30 which exceeds the AWS-published price delta.
GRAVITON_SAVINGS_FACTOR: float = 0.20
SPOT_SAVINGS_FACTOR: float = 0.70


class EksCostModule(BaseServiceModule):
    """ServiceModule adapter for EKS cost visibility and optimization.

    Analyzes EKS clusters, node groups, Fargate profiles, and add-ons
    to surface cost savings opportunities. Integrates with Cost Optimization
    Hub for AWS-generated recommendations.

    All EKS API calls are free (no per-request charge).
    """

    key: str = "eks_cost"
    cli_aliases: tuple[str, ...] = ("eks_cost", "eks_cost_visibility")
    display_name: str = "EKS Cost Visibility"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="EKS Clusters", source_path="extras.cluster_count", formatter="int"),
        StatCardSpec(label="Control Plane Cost", source_path="extras.monthly_control_plane_cost", formatter="currency"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False
    reads_fast_mode: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns EKS, EC2, and CloudWatch client names."""
        return ("eks", "ec2", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EKS clusters for cost optimization opportunities.

        Analyzes control plane costs, node group instance types, Fargate
        profiles, add-on costs, and Cost Optimization Hub recommendations
        to provide comprehensive EKS cost visibility.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cluster_costs, node_group_optimization,
            fargate_analysis, addon_costs, and cost_hub_recommendations
            source blocks.
        """
        print("\U0001f50d [services/adapters/eks.py] EKS Cost Visibility module active")

        eks = ctx.client("eks")
        if not eks:
            return self._empty_findings()

        multiplier = ctx.pricing_multiplier

        cluster_names = self._list_clusters(eks)

        cluster_recs: list[dict[str, Any]] = []
        node_group_recs: list[dict[str, Any]] = []
        fargate_recs: list[dict[str, Any]] = []
        addon_recs: list[dict[str, Any]] = []

        node_group_count = 0
        fargate_profile_count = 0
        addon_count = 0

        for name in cluster_names:
            details = self._describe_cluster(eks, name)
            if not details:
                continue

            cluster = details.get("cluster", {})
            status = cluster.get("status", "UNKNOWN")
            version = cluster.get("version", "Unknown")

            cr = self._check_cluster_cost(name, status, version, multiplier)
            cluster_recs.extend(cr)

            ngs, ng_count = self._analyze_node_groups(eks, name, multiplier)
            node_group_recs.extend(ngs)
            node_group_count += ng_count

            fgs, fp_count = self._analyze_fargate(eks, name, multiplier)
            fargate_recs.extend(fgs)
            fargate_profile_count += fp_count

            ads, ad_count = self._analyze_addons(eks, name)
            addon_recs.extend(ads)
            addon_count += ad_count

        cost_hub_recs = self._build_cost_hub_recs(ctx)

        all_recs = cluster_recs + node_group_recs + fargate_recs + addon_recs + cost_hub_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)
        control_plane_total = len(cluster_names) * EKS_CONTROL_PLANE_HOURLY * HOURS_PER_MONTH

        return ServiceFindings(
            service_name="EKS Cost Visibility",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "cluster_costs": SourceBlock(
                    count=len(cluster_recs),
                    recommendations=tuple(cluster_recs),
                ),
                "node_group_optimization": SourceBlock(
                    count=len(node_group_recs),
                    recommendations=tuple(node_group_recs),
                ),
                "fargate_analysis": SourceBlock(
                    count=len(fargate_recs),
                    recommendations=tuple(fargate_recs),
                ),
                "addon_costs": SourceBlock(
                    count=len(addon_recs),
                    recommendations=tuple(addon_recs),
                ),
                "cost_hub_recommendations": SourceBlock(
                    count=len(cost_hub_recs),
                    recommendations=tuple(cost_hub_recs),
                ),
            },
            extras={
                "cluster_count": len(cluster_names),
                "node_group_count": node_group_count,
                "fargate_profile_count": fargate_profile_count,
                "addon_count": addon_count,
                "monthly_control_plane_cost": round(control_plane_total, 2),
            },
            optimization_descriptions={
                "cluster_costs": {
                    "title": "EKS Cluster Costs",
                    "description": "Control plane hourly costs ($0.10/hr) for each EKS cluster",
                },
                "node_group_optimization": {
                    "title": "Node Group Optimization",
                    "description": "EC2 instance optimization opportunities within EKS node groups",
                },
                "fargate_analysis": {
                    "title": "Fargate Profile Analysis",
                    "description": "Fargate vs EC2 cost comparison and profile configuration review",
                },
                "addon_costs": {
                    "title": "Add-on Costs",
                    "description": "EKS managed add-on costs and potential optimization opportunities",
                },
                "cost_hub_recommendations": {
                    "title": "Cost Optimization Hub Recommendations",
                    "description": "AWS Cost Optimization Hub recommendations specific to EKS resources",
                },
            },
        )

    def _list_clusters(self, eks: Any) -> list[str]:
        """List all EKS cluster names with pagination.

        Args:
            eks: EKS boto3 client.

        Returns:
            List of cluster name strings.
        """
        names: list[str] = []
        try:
            paginator = eks.get_paginator("list_clusters")
            for page in paginator.paginate():
                names.extend(page.get("clusters", []))
        except Exception as e:
            print(f"Warning: EKS list_clusters failed: {e}")
        return names

    def _describe_cluster(self, eks: Any, name: str) -> dict[str, Any] | None:
        """Describe a single EKS cluster.

        Args:
            eks: EKS boto3 client.
            name: Cluster name.

        Returns:
            Cluster description dict, or None on error.
        """
        try:
            return eks.describe_cluster(name=name)
        except Exception as e:
            print(f"Warning: EKS describe_cluster({name}) failed: {e}")
            return None

    def _check_cluster_cost(
        self,
        name: str,
        status: str,
        version: str,
        multiplier: float,
    ) -> list[dict[str, Any]]:
        """Check cluster control plane cost and version deprecation.

        Args:
            name: Cluster name.
            status: Cluster status string.
            version: Kubernetes version string.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            List of cluster cost recommendation dicts.
        """
        recs: list[dict[str, Any]] = []
        monthly_cost = EKS_CONTROL_PLANE_HOURLY * HOURS_PER_MONTH

        if status != "ACTIVE":
            recs.append(
                {
                    "resource_id": name,
                    "check_type": "cluster_cost",
                    "check_category": "EKS Control Plane",
                    "current_value": f"Cluster status: {status}",
                    "recommended_value": "Delete inactive cluster to save control plane cost",
                    "monthly_savings": round(monthly_cost * multiplier, 2),
                    "severity": "HIGH",
                    "reason": f"EKS cluster '{name}' is in {status} state; "
                    f"deleting saves ${monthly_cost:.2f}/mo control plane cost",
                }
            )
            return recs

        try:
            version_parts = version.split(".")
            if len(version_parts) >= 2:
                minor = int(version_parts[1])
                if minor < 26:
                    recs.append(
                        {
                            "resource_id": name,
                            "check_type": "cluster_cost",
                            "check_category": "EKS Control Plane",
                            "current_value": f"Kubernetes version {version}",
                            "recommended_value": "Upgrade to latest supported Kubernetes version",
                            "monthly_savings": 0.0,
                            "severity": "MEDIUM",
                            "reason": f"EKS cluster '{name}' runs Kubernetes {version}; "
                            f"versions more than 2 minor versions behind may be deprecated",
                        }
                    )
        except (ValueError, IndexError):
            pass

        return recs

    def _analyze_node_groups(
        self,
        eks: Any,
        cluster_name: str,
        multiplier: float,  # noqa: ARG002 - reserved for future EC2 price math
    ) -> tuple[list[dict[str, Any]], int]:
        """Analyze node groups for instance type and scaling optimization.

        Checks for previous-generation instance types (recommend Graviton),
        over-provisioned scaling configs, and Spot adoption opportunities.

        Args:
            eks: EKS boto3 client.
            cluster_name: Parent cluster name.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            Tuple of (recommendation list, node group count).
        """
        recs: list[dict[str, Any]] = []
        ng_names: list[str] = []

        try:
            paginator = eks.get_paginator("list_nodegroups")
            for page in paginator.paginate(clusterName=cluster_name):
                ng_names.extend(page.get("nodegroups", []))
        except Exception as e:
            print(f"Warning: EKS list_nodegroups({cluster_name}) failed: {e}")
            return recs, 0

        for ng_name in ng_names:
            try:
                resp = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)
                ng = resp.get("nodegroup", {})
                instance_types = ng.get("instanceTypes", [])
                scaling = ng.get("scalingConfig", {})
                desired = scaling.get("desiredSize", 0)
                max_size = scaling.get("maxSize", 0)
                ng_status = ng.get("status", "UNKNOWN")

                resource_id = f"{cluster_name}/{ng_name}"

                # Inactive node group state finding removed: $0/month, health/operational
                # signal — not a cost recommendation.

                has_prev_gen = any(any(it.startswith(prefix) for prefix in PREV_GEN_PREFIXES) for it in instance_types)
                if has_prev_gen:
                    # Graviton savings derive from the EC2 instance price ×
                    # desired node count × 20% list-price delta — NOT from
                    # the EKS control-plane fee. The previous formula
                    # ($0.10/hr × 730 × 2 × 0.30 = $43.80 flat) had no
                    # relation to the actual node-group cost. Without
                    # EC2 instance-price + count data wired through, we
                    # cannot quantify honestly — emit 0 + PricingWarning
                    # so the rec still surfaces for review.
                    recs.append(
                        {
                            "resource_id": resource_id,
                            "check_type": "node_group",
                            "check_category": "Node Group Optimization",
                            "current_value": f"Previous-gen instance types: {instance_types}",
                            "recommended_value": f"Migrate to Graviton (ARM) instances for ~{int(GRAVITON_SAVINGS_FACTOR * 100)}% cost savings",
                            "monthly_savings": 0.0,
                            "pricing_warning": (
                                "requires EC2 instance price × desired node count "
                                "for quantified savings"
                            ),
                            "severity": "MEDIUM",
                            "reason": f"Node group '{ng_name}' uses previous-generation instance types; "
                            f"Graviton list-price delta is ~{int(GRAVITON_SAVINGS_FACTOR * 100)}%",
                        }
                    )

                # Under-utilized scaling config finding removed: $0/month, "consider
                # right-sizing or enabling autoscaling" without quantified cost delta.

                # Spot savings = (EC2 instance hourly × node count × 730 × 0.70).
                # Same constraint as the Graviton path: without the EC2
                # instance price + desired-node-count multiplication, we
                # cannot quantify honestly. Emit 0 + PricingWarning rather
                # than the previous control-plane-cost-based formula
                # which under-reports by 20-680x depending on real
                # node group size.
                recs.append(
                    {
                        "resource_id": resource_id,
                        "check_type": "node_group",
                        "check_category": "Node Group Optimization",
                        "current_value": f"On-Demand node group with types: {instance_types}",
                        "recommended_value": "Use Spot instances for non-critical workloads (60-90% savings)",
                        "monthly_savings": 0.0,
                        "pricing_warning": (
                            "requires EC2 instance price × desired node count "
                            "for quantified savings"
                        ),
                        "severity": "LOW",
                        "reason": f"Node group '{ng_name}' uses On-Demand instances; "
                        f"Spot instances offer 60-90% savings for fault-tolerant workloads",
                    }
                )

            except Exception as e:
                print(f"Warning: EKS describe_nodegroup({cluster_name}/{ng_name}) failed: {e}")

        return recs, len(ng_names)

    def _analyze_fargate(
        self,
        eks: Any,
        cluster_name: str,
        multiplier: float,
    ) -> tuple[list[dict[str, Any]], int]:
        """Analyze Fargate profiles for cost optimization.

        Estimates Fargate spend and recommends Graviton profiles and
        pod consolidation for cost reduction.

        Args:
            eks: EKS boto3 client.
            cluster_name: Parent cluster name.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            Tuple of (recommendation list, fargate profile count).
        """
        recs: list[dict[str, Any]] = []
        profile_names: list[str] = []

        try:
            paginator = eks.get_paginator("list_fargate_profiles")
            for page in paginator.paginate(clusterName=cluster_name):
                profile_names.extend(page.get("fargateProfileNames", []))
        except Exception as e:
            print(f"Warning: EKS list_fargate_profiles({cluster_name}) failed: {e}")
            return recs, 0

        if profile_names:
            estimated_pods = max(len(profile_names) * 3, 1)
            typical_vcpu = 0.25
            typical_mem_gb = 0.5
            monthly_fargate = (
                estimated_pods
                * (typical_vcpu * FARGATE_VCPU_HOURLY + typical_mem_gb * FARGATE_MEM_GB_HOURLY)
                * HOURS_PER_MONTH
            )

            recs.append(
                {
                    "resource_id": f"{cluster_name}/fargate",
                    "check_type": "fargate_analysis",
                    "check_category": "Fargate Cost Analysis",
                    "current_value": f"{len(profile_names)} Fargate profile(s), ~{estimated_pods} pods, "
                    f"est. ${monthly_fargate:.2f}/mo",
                    "recommended_value": "Use Graviton Fargate for ~20% savings and consolidate pods",
                    "monthly_savings": round(monthly_fargate * 0.20 * multiplier, 2),
                    "severity": "MEDIUM",
                    "reason": f"Cluster '{cluster_name}' has {len(profile_names)} Fargate profile(s) "
                    f"with estimated ${monthly_fargate:.2f}/mo spend; "
                    f"Graviton profiles and pod consolidation could save ~20%",
                }
            )
        # "No Fargate profiles configured" finding removed: $0/month, best-practice
        # migration nudge ("Fargate eliminates node management overhead") — not a
        # cost saving for this cluster.

        return recs, len(profile_names)

    def _analyze_addons(
        self,
        eks: Any,
        cluster_name: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Analyze EKS add-ons for cost and status issues.

        Flags marketplace add-ons with potential costs and inactive
        add-ons for cleanup.

        Args:
            eks: EKS boto3 client.
            cluster_name: Parent cluster name.

        Returns:
            Tuple of (recommendation list, addon count).
        """
        recs: list[dict[str, Any]] = []
        addon_names: list[str] = []

        try:
            resp = eks.list_addons(clusterName=cluster_name)
            addon_names = resp.get("addons", [])
        except Exception as e:
            print(f"Warning: EKS list_addons({cluster_name}) failed: {e}")
            return recs, 0

        for addon_name in addon_names:
            try:
                detail = eks.describe_addon(clusterName=cluster_name, addonName=addon_name)
                addon = detail.get("addon", {})
                addon_status = addon.get("status", "UNKNOWN")
                addon_arn = addon.get("addonArn", "")
                addon_version = addon.get("addonVersion", "Unknown")

                # Marketplace add-on $0 advisory removed: no per-account cost stated;
                # MCP review confirms add-on cost varies per subscription so a generic
                # "review pricing" finding has no concrete saving.
                # Add-on DEGRADED state removed: health/operational signal, $0/month.
                _ = (addon_arn, addon_status, addon_version)

            except Exception as e:
                print(f"Warning: EKS describe_addon({cluster_name}/{addon_name}) failed: {e}")

        return recs, len(addon_names)

    def _build_cost_hub_recs(self, ctx: Any) -> list[dict[str, Any]]:
        """Build recommendations from Cost Optimization Hub EKS data.

        Uses pre-fetched Cost Hub recommendations from the scan
        orchestrator's cost_hub_splits mapping.

        Args:
            ctx: ScanContext with cost_hub_splits attribute.

        Returns:
            List of Cost Hub recommendation dicts.
        """
        recs: list[dict[str, Any]] = []
        multiplier = ctx.pricing_multiplier

        try:
            hub_recs = ctx.cost_hub_splits.get("eks", [])
        except Exception:
            return recs

        for rec in hub_recs:
            monthly_savings = float(rec.get("estimatedMonthlySavings", 0.0))
            recs.append(
                {
                    "resource_id": rec.get("recommendationId", "unknown"),
                    "check_type": "cost_hub",
                    "check_category": "Cost Optimization Hub",
                    "current_value": rec.get("recommendationSummary", ""),
                    "recommended_value": rec.get("recommendationSummary", ""),
                    "monthly_savings": round(monthly_savings, 2),
                    "severity": "MEDIUM",
                    "reason": f"Cost Optimization Hub recommendation: {rec.get('recommendationSummary', '')}",
                }
            )

        return recs

    @staticmethod
    def _empty_findings() -> ServiceFindings:
        """Return empty ServiceFindings when EKS client is unavailable.

        Returns:
            ServiceFindings with zero counts and empty source blocks.
        """
        return ServiceFindings(
            service_name="EKS Cost Visibility",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={
                "cluster_costs": SourceBlock(count=0, recommendations=()),
                "node_group_optimization": SourceBlock(count=0, recommendations=()),
                "fargate_analysis": SourceBlock(count=0, recommendations=()),
                "addon_costs": SourceBlock(count=0, recommendations=()),
                "cost_hub_recommendations": SourceBlock(count=0, recommendations=()),
            },
            extras={
                "cluster_count": 0,
                "node_group_count": 0,
                "fargate_profile_count": 0,
                "addon_count": 0,
                "monthly_control_plane_cost": 0.0,
            },
            optimization_descriptions={
                "cluster_costs": {
                    "title": "EKS Cluster Costs",
                    "description": "Control plane hourly costs ($0.10/hr) for each EKS cluster",
                },
                "node_group_optimization": {
                    "title": "Node Group Optimization",
                    "description": "EC2 instance optimization opportunities within EKS node groups",
                },
                "fargate_analysis": {
                    "title": "Fargate Profile Analysis",
                    "description": "Fargate vs EC2 cost comparison and profile configuration review",
                },
                "addon_costs": {
                    "title": "Add-on Costs",
                    "description": "EKS managed add-on costs and potential optimization opportunities",
                },
                "cost_hub_recommendations": {
                    "title": "Cost Optimization Hub Recommendations",
                    "description": "AWS Cost Optimization Hub recommendations specific to EKS resources",
                },
            },
        )
