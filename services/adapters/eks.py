"""EKS cost visibility and optimization adapter.

Provides dedicated EKS cost analysis beyond the general containers adapter:

- Control-plane cost (priced via the AWS Pricing API, not hardcoded)
- Extended Support surcharge: a large recurring cost charged once a cluster's
  Kubernetes version exits standard support — surfaced as a real $ saving
- Idle / empty cluster control-plane cost (billed even with no workloads)
- Node-group and Fargate-profile findings, surfaced as ADVISORY only: the
  node instances are EC2 resources owned by the EC2 adapter, and Fargate pod
  cost requires Container Insights evidence we do not fabricate
- Cost Optimization Hub integration for EKS-specific recommendations

AWS API cost: All EKS API calls are free (no per-request charge).
"""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

logger = logging.getLogger(__name__)

HOURS_PER_MONTH: int = 730

PREV_GEN_PREFIXES: tuple[str, ...] = ("m3.", "m4.", "c3.", "c4.", "r3.", "r4.", "t2.")
# AWS Graviton EC2 list-price delta vs x86 (m5→m6g, c5→c6g, etc.) ≈ 20%.
GRAVITON_SAVINGS_FACTOR: float = 0.20
SPOT_SAVINGS_FACTOR: float = 0.70


def _is_access_denied(exc: Exception) -> bool:
    """True when an exception looks like an IAM authorization failure."""
    text = str(exc)
    return "AccessDenied" in text or "UnauthorizedOperation" in text or "not authorized" in text


class EksCostModule(BaseServiceModule):
    """ServiceModule adapter for EKS cost visibility and optimization.

    Counts only EKS-owned costs: control plane, Extended Support surcharge,
    idle empty-cluster control plane, and Cost Optimization Hub recs. Node
    groups (EC2 instances) and Fargate pod sizing are emitted as advisory
    (``Counted=False``) so they never double-count the EC2 adapter or fabricate
    savings from invented utilization.
    """

    key: str = "eks_cost"
    cli_aliases: tuple[str, ...] = ("eks_cost", "eks_cost_visibility", "eks")
    display_name: str = "EKS Cost Visibility"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="EKS Clusters", source_path="extras.cluster_count", formatter="int"),
        StatCardSpec(label="Control Plane Cost", source_path="extras.monthly_control_plane_cost", formatter="currency"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns EKS, EC2, and CloudWatch client names."""
        return ("eks", "ec2", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EKS clusters for cost optimization opportunities.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cluster_costs, node_group_optimization,
            fargate_analysis, addon_costs, and cost_hub_recommendations
            source blocks.
        """

        eks = ctx.client("eks")
        if not eks:
            return self._empty_findings()

        # Region-correct rates from the Pricing API (no hardcoded $/hr).
        try:
            control_plane_rate = ctx.pricing_engine.get_eks_control_plane_hourly()
            extended_support_rate = ctx.pricing_engine.get_eks_extended_support_hourly()
        except Exception as e:
            ctx.warn(f"EKS pricing lookup failed: {e}", "eks_cost")
            control_plane_rate = 0.0
            extended_support_rate = 0.0

        cluster_names = self._list_clusters(ctx, eks)

        cluster_recs: list[dict[str, Any]] = []
        node_group_recs: list[dict[str, Any]] = []
        fargate_recs: list[dict[str, Any]] = []
        addon_recs: list[dict[str, Any]] = []

        node_group_count = 0
        fargate_profile_count = 0
        addon_count = 0

        for name in cluster_names:
            details = self._describe_cluster(ctx, eks, name)
            if not details:
                continue

            cluster = details.get("cluster", {})

            ngs, ng_count = self._analyze_node_groups(ctx, eks, name)
            node_group_recs.extend(ngs)
            node_group_count += ng_count

            fgs, fp_count = self._analyze_fargate(ctx, eks, name)
            fargate_recs.extend(fgs)
            fargate_profile_count += fp_count

            cr = self._check_cluster_cost(
                name,
                cluster,
                control_plane_rate,
                extended_support_rate,
                is_idle=(ng_count == 0 and fp_count == 0),
            )
            cluster_recs.extend(cr)

            ads, ad_count = self._analyze_addons(ctx, eks, name)
            addon_recs.extend(ads)
            addon_count += ad_count

        cost_hub_recs = self._build_cost_hub_recs(ctx)

        all_recs = cluster_recs + node_group_recs + fargate_recs + addon_recs + cost_hub_recs
        # Counted savings exclude advisory (Counted=False) recs — node-level and
        # Fargate-profile findings are surfaced for visibility but their dollars
        # belong to the EC2 tab or require evidence we don't fabricate.
        total_savings = sum(
            r.get("monthly_savings", 0.0) for r in all_recs if r.get("Counted", True)
        )
        control_plane_total = len(cluster_names) * control_plane_rate * HOURS_PER_MONTH

        return ServiceFindings(
            service_name="EKS Cost Visibility",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "cluster_costs": SourceBlock(count=len(cluster_recs), recommendations=tuple(cluster_recs)),
                "node_group_optimization": SourceBlock(
                    count=len(node_group_recs), recommendations=tuple(node_group_recs)
                ),
                "fargate_analysis": SourceBlock(count=len(fargate_recs), recommendations=tuple(fargate_recs)),
                "addon_costs": SourceBlock(count=len(addon_recs), recommendations=tuple(addon_recs)),
                "cost_hub_recommendations": SourceBlock(
                    count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)
                ),
            },
            extras={
                "cluster_count": len(cluster_names),
                "node_group_count": node_group_count,
                "fargate_profile_count": fargate_profile_count,
                "addon_count": addon_count,
                "monthly_control_plane_cost": round(control_plane_total, 2),
            },
            optimization_descriptions=self._optimization_descriptions(),
        )

    def _list_clusters(self, ctx: Any, eks: Any) -> list[str]:
        """List all EKS cluster names with pagination."""
        names: list[str] = []
        try:
            paginator = eks.get_paginator("list_clusters")
            for page in paginator.paginate():
                names.extend(page.get("clusters", []))
        except Exception as e:
            if _is_access_denied(e):
                ctx.permission_issue("EKS list_clusters denied", "eks_cost", "eks:ListClusters")
            else:
                ctx.warn(f"EKS list_clusters failed: {e}", "eks_cost")
        return names

    def _describe_cluster(self, ctx: Any, eks: Any, name: str) -> dict[str, Any] | None:
        """Describe a single EKS cluster."""
        try:
            return eks.describe_cluster(name=name)
        except Exception as e:
            if _is_access_denied(e):
                ctx.permission_issue(f"EKS describe_cluster({name}) denied", "eks_cost", "eks:DescribeCluster")
            else:
                ctx.warn(f"EKS describe_cluster({name}) failed: {e}", "eks_cost")
            return None

    def _check_cluster_cost(
        self,
        name: str,
        cluster: dict[str, Any],
        control_plane_rate: float,
        extended_support_rate: float,
        *,
        is_idle: bool,
    ) -> list[dict[str, Any]]:
        """Cluster-level control-plane findings: Extended Support + idle control plane.

        Args:
            name: Cluster name.
            cluster: The ``cluster`` dict from describe_cluster.
            control_plane_rate: $/hr base control-plane fee (region-correct).
            extended_support_rate: $/hr Extended Support surcharge (region-correct).
            is_idle: True when the cluster has no node groups and no Fargate profiles.

        Returns:
            List of cluster cost recommendation dicts.
        """
        recs: list[dict[str, Any]] = []
        status = cluster.get("status", "UNKNOWN")
        version = cluster.get("version", "Unknown")
        monthly_control_plane = control_plane_rate * HOURS_PER_MONTH

        # Extended Support surcharge — evidence-based: charge only when AWS
        # reports the cluster is actually on extended support, not by guessing
        # from the version number. The surcharge ($0.50/hr ≈ $365/mo) is billed
        # ON TOP OF the base fee, so it is also the saving from upgrading off it.
        support_type = (cluster.get("upgradePolicy", {}) or {}).get("supportType", "")
        if support_type == "EXTENDED" and extended_support_rate > 0:
            monthly_surcharge = extended_support_rate * HOURS_PER_MONTH
            recs.append(
                {
                    "resource_id": name,
                    "check_type": "extended_support",
                    "check_category": "EKS Extended Support",
                    "current_value": f"Kubernetes {version} on Extended Support (+${extended_support_rate:.2f}/hr)",
                    "recommended_value": "Upgrade to a standard-support Kubernetes version",
                    "monthly_savings": round(monthly_surcharge, 2),
                    "severity": "HIGH",
                    "audit_basis": {
                        "rate": extended_support_rate,
                        "unit": "USD/cluster-hour",
                        "formula": f"{extended_support_rate} x {HOURS_PER_MONTH} hr",
                        "evidence": "cluster.upgradePolicy.supportType == EXTENDED",
                    },
                    "reason": (
                        f"EKS cluster '{name}' runs Kubernetes {version} on Extended Support; "
                        f"upgrading removes the ${extended_support_rate:.2f}/hr surcharge "
                        f"(~${monthly_surcharge:.2f}/mo)"
                    ),
                }
            )

        # Idle / empty cluster: ACTIVE control plane is billed even with no
        # node groups or Fargate profiles. Only count when truly idle; assumes
        # no self-managed/Karpenter nodes (which describe APIs cannot enumerate).
        if status == "ACTIVE" and is_idle and monthly_control_plane > 0:
            recs.append(
                {
                    "resource_id": name,
                    "check_type": "idle_cluster",
                    "check_category": "EKS Idle Control Plane",
                    "current_value": "Active cluster with no node groups or Fargate profiles",
                    "recommended_value": "Delete or consolidate the cluster to save control-plane cost",
                    "monthly_savings": round(monthly_control_plane, 2),
                    "severity": "HIGH",
                    "audit_basis": {
                        "rate": control_plane_rate,
                        "unit": "USD/cluster-hour",
                        "formula": f"{control_plane_rate} x {HOURS_PER_MONTH} hr",
                        "evidence": "0 node groups and 0 Fargate profiles via describe APIs",
                        "assumption": "no self-managed/Karpenter nodes",
                    },
                    "reason": (
                        f"EKS cluster '{name}' has no node groups or Fargate profiles; "
                        f"its control plane still bills ${monthly_control_plane:.2f}/mo"
                    ),
                }
            )
        elif status == "FAILED" and monthly_control_plane > 0:
            recs.append(
                {
                    "resource_id": name,
                    "check_type": "failed_cluster",
                    "check_category": "EKS Idle Control Plane",
                    "current_value": f"Cluster status: {status}",
                    "recommended_value": "Delete the failed cluster to stop control-plane charges",
                    "monthly_savings": round(monthly_control_plane, 2),
                    "severity": "HIGH",
                    "reason": (
                        f"EKS cluster '{name}' is in {status} state; deleting saves "
                        f"${monthly_control_plane:.2f}/mo control-plane cost"
                    ),
                }
            )

        return recs

    def _analyze_node_groups(
        self,
        ctx: Any,
        eks: Any,
        cluster_name: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Surface node-group optimization opportunities as ADVISORY only.

        Node-group instances are EC2 resources covered by the EC2 adapter
        (EC2 Compute Optimizer + ASG-member handling). We therefore emit
        Graviton/Spot opportunities with ``Counted=False`` and ``monthly_savings=0``
        so they are visible but never double-count the EC2 tab.

        Returns:
            Tuple of (advisory recommendation list, node group count).
        """
        recs: list[dict[str, Any]] = []
        ng_names: list[str] = []

        try:
            paginator = eks.get_paginator("list_nodegroups")
            for page in paginator.paginate(clusterName=cluster_name):
                ng_names.extend(page.get("nodegroups", []))
        except Exception as e:
            if _is_access_denied(e):
                ctx.permission_issue(
                    f"EKS list_nodegroups({cluster_name}) denied", "eks_cost", "eks:ListNodegroups"
                )
            else:
                ctx.warn(f"EKS list_nodegroups({cluster_name}) failed: {e}", "eks_cost")
            return recs, 0

        for ng_name in ng_names:
            try:
                resp = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)
                ng = resp.get("nodegroup", {})
                instance_types = ng.get("instanceTypes", [])
                capacity_type = ng.get("capacityType", "ON_DEMAND")
                resource_id = f"{cluster_name}/{ng_name}"

                has_prev_gen = any(
                    any(it.startswith(prefix) for prefix in PREV_GEN_PREFIXES) for it in instance_types
                )
                if has_prev_gen:
                    recs.append(
                        {
                            "resource_id": resource_id,
                            "check_type": "node_group",
                            "check_category": "Node Group Optimization",
                            "current_value": f"Previous-gen instance types: {instance_types}",
                            "recommended_value": (
                                f"Migrate to Graviton (ARM) for ~{int(GRAVITON_SAVINGS_FACTOR * 100)}% list-price savings"
                            ),
                            "monthly_savings": 0.0,
                            "Counted": False,
                            "severity": "MEDIUM",
                            "reason": (
                                f"Node group '{ng_name}' uses previous-generation instances; these are EC2 "
                                f"instances — quantified rightsizing/Graviton savings are counted in the EC2 tab"
                            ),
                        }
                    )

                if capacity_type != "SPOT":
                    recs.append(
                        {
                            "resource_id": resource_id,
                            "check_type": "node_group",
                            "check_category": "Node Group Optimization",
                            "current_value": f"{capacity_type} node group: {instance_types}",
                            "recommended_value": "Use Spot for fault-tolerant workloads (60-90% savings)",
                            "monthly_savings": 0.0,
                            "Counted": False,
                            "severity": "LOW",
                            "reason": (
                                f"Node group '{ng_name}' uses {capacity_type} instances; Spot savings on these "
                                f"EC2 instances are evaluated in the EC2 tab"
                            ),
                        }
                    )

            except Exception as e:
                if _is_access_denied(e):
                    ctx.permission_issue(
                        f"EKS describe_nodegroup({cluster_name}/{ng_name}) denied",
                        "eks_cost",
                        "eks:DescribeNodegroup",
                    )
                else:
                    ctx.warn(f"EKS describe_nodegroup({cluster_name}/{ng_name}) failed: {e}", "eks_cost")

        return recs, len(ng_names)

    def _analyze_fargate(
        self,
        ctx: Any,
        eks: Any,
        cluster_name: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Surface Fargate-profile presence as ADVISORY (no fabricated savings).

        Quantifying Fargate pod cost requires real pod count and per-pod
        vCPU/memory from Container Insights. Without that evidence we do NOT
        invent a "3 pods x 0.25 vCPU" estimate — we emit a Counted=False
        advisory so the opportunity is visible but contributes $0.

        Returns:
            Tuple of (advisory recommendation list, fargate profile count).
        """
        recs: list[dict[str, Any]] = []
        profile_names: list[str] = []

        try:
            paginator = eks.get_paginator("list_fargate_profiles")
            for page in paginator.paginate(clusterName=cluster_name):
                profile_names.extend(page.get("fargateProfileNames", []))
        except Exception as e:
            if _is_access_denied(e):
                ctx.permission_issue(
                    f"EKS list_fargate_profiles({cluster_name}) denied",
                    "eks_cost",
                    "eks:ListFargateProfiles",
                )
            else:
                ctx.warn(f"EKS list_fargate_profiles({cluster_name}) failed: {e}", "eks_cost")
            return recs, 0

        if profile_names:
            note = (
                "Fargate pod cost (vCPU/GB-hours) requires Container Insights pod metrics to quantify."
                if not ctx.fast_mode
                else "Skipped Fargate pod-cost quantification in fast mode."
            )
            recs.append(
                {
                    "resource_id": f"{cluster_name}/fargate",
                    "check_type": "fargate_analysis",
                    "check_category": "Fargate Cost Analysis",
                    "current_value": f"{len(profile_names)} Fargate profile(s)",
                    "recommended_value": "Right-size pods and consider Graviton (ARM) Fargate (~20% cheaper)",
                    "monthly_savings": 0.0,
                    "Counted": False,
                    "severity": "LOW",
                    "reason": (
                        f"Cluster '{cluster_name}' has {len(profile_names)} Fargate profile(s). {note}"
                    ),
                }
            )

        return recs, len(profile_names)

    def _analyze_addons(
        self,
        ctx: Any,
        eks: Any,
        cluster_name: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Count EKS add-ons. No cost findings (add-on cost varies per subscription)."""
        addon_names: list[str] = []
        try:
            resp = eks.list_addons(clusterName=cluster_name)
            addon_names = resp.get("addons", [])
        except Exception as e:
            if _is_access_denied(e):
                ctx.permission_issue(
                    f"EKS list_addons({cluster_name}) denied", "eks_cost", "eks:ListAddons"
                )
            else:
                ctx.warn(f"EKS list_addons({cluster_name}) failed: {e}", "eks_cost")
            return [], 0
        return [], len(addon_names)

    def _build_cost_hub_recs(self, ctx: Any) -> list[dict[str, Any]]:
        """Build recommendations from Cost Optimization Hub EKS data.

        Reads the orchestrator's ``cost_hub_splits["eks_cost"]`` bucket (the
        bucket name now matches this module's key — see scan_orchestrator).
        """
        recs: list[dict[str, Any]] = []
        try:
            hub_recs = ctx.cost_hub_splits.get("eks_cost", [])
        except Exception:
            return recs

        for rec in hub_recs:
            monthly_savings = float(rec.get("estimatedMonthlySavings", 0.0) or 0.0)
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
    def _optimization_descriptions() -> dict[str, dict[str, str]]:
        """Source-block descriptions shared by populated and empty findings."""
        return {
            "cluster_costs": {
                "title": "EKS Cluster Costs",
                "description": "Control-plane cost, Extended Support surcharge, and idle-cluster savings",
            },
            "node_group_optimization": {
                "title": "Node Group Optimization (advisory)",
                "description": "EC2 node-group opportunities — quantified savings counted in the EC2 tab",
            },
            "fargate_analysis": {
                "title": "Fargate Profile Analysis (advisory)",
                "description": "Fargate profile presence; pod-cost quantification needs Container Insights",
            },
            "addon_costs": {
                "title": "Add-on Costs",
                "description": "EKS managed add-on inventory",
            },
            "cost_hub_recommendations": {
                "title": "Cost Optimization Hub Recommendations",
                "description": "AWS Cost Optimization Hub recommendations specific to EKS resources",
            },
        }

    def _empty_findings(self) -> ServiceFindings:
        """Return empty ServiceFindings when EKS client is unavailable."""
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
            optimization_descriptions=self._optimization_descriptions(),
        )
