"""Container services (ECS, EKS, ECR) cost optimization checks.

Extracted from CostOptimizer as free functions taking ScanContext.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

print("\U0001f50d [services/containers.py] Containers module active")

HIGH_IMAGE_COUNT_THRESHOLD: int = 10

CONTAINER_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "ecs_rightsizing": {
        "title": "Optimize ECS Task and Service Configuration",
        "description": "Rightsize CPU and memory allocation, implement auto scaling, and use Spot capacity.",
        "action": (
            "1. Monitor task resource utilization\n"
            "2. Adjust CPU/memory allocation based on usage\n"
            "3. Implement service auto scaling\n"
            "4. Use Spot capacity for fault-tolerant workloads\n"
            "5. Estimated savings: 30-70% through optimization"
        ),
    },
    "eks_node_optimization": {
        "title": "Optimize EKS Node Groups and Scaling",
        "description": "Use appropriate instance types, implement cluster autoscaling, and leverage Spot instances.",
        "action": (
            "1. Implement cluster autoscaler\n"
            "2. Use Spot instances for non-critical workloads\n"
            "3. Rightsize node group instance types\n"
            "4. Configure horizontal pod autoscaling\n"
            "5. Estimated savings: 60-90% with Spot instances"
        ),
    },
    "ecr_lifecycle": {
        "title": "Implement ECR Lifecycle Policies",
        "description": "Automatically delete old container images to reduce storage costs.",
        "action": (
            "1. Configure lifecycle policies for image retention\n"
            "2. Delete untagged images automatically\n"
            "3. Limit number of images per repository\n"
            "4. Implement image scanning and cleanup\n"
            "5. Estimated savings: 50-80% on storage costs"
        ),
    },
    "container_scheduling": {
        "title": "Optimize Container Scheduling and Placement",
        "description": "Improve resource utilization through better scheduling and placement strategies.",
        "action": (
            "1. Use appropriate scheduling constraints\n"
            "2. Implement resource quotas and limits\n"
            "3. Optimize container placement for cost efficiency\n"
            "4. Consider Fargate vs EC2 launch types\n"
            "5. Estimated savings: 20-50% through better utilization"
        ),
    },
    "monitoring_optimization": {
        "title": "Implement Container Cost Monitoring",
        "description": "Use CloudWatch Container Insights and cost allocation tags for visibility.",
        "action": (
            "1. Enable Container Insights for detailed monitoring\n"
            "2. Implement cost allocation tags\n"
            "3. Set up cost alerts and budgets\n"
            "4. Regular cost review and optimization\n"
            "5. Estimated savings: 15-30% through visibility and control"
        ),
    },
}


def get_ecs_analysis(ctx: ScanContext) -> dict[str, Any]:
    """Analyze ECS clusters and services."""
    try:
        ecs = ctx.client("ecs")

        paginator = ecs.get_paginator("list_clusters")
        cluster_arns: list[str] = []
        for page in paginator.paginate():
            cluster_arns.extend(page.get("clusterArns", []))

        analysis: dict[str, Any] = {
            "total_clusters": len(cluster_arns),
            "clusters": [],
            "total_services": 0,
            "optimization_opportunities": [],
        }

        for cluster_arn in cluster_arns:
            cluster_name = cluster_arn.split("/")[-1]

            cluster_details = ecs.describe_clusters(clusters=[cluster_arn])
            cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}

            paginator = ecs.get_paginator("list_services")
            service_arns: list[str] = []
            for page in paginator.paginate(cluster=cluster_arn):
                service_arns.extend(page.get("serviceArns", []))

            cluster_info: dict[str, Any] = {
                "ClusterName": cluster_name,
                "Status": cluster.get("status", "UNKNOWN"),
                "RunningTasksCount": cluster.get("runningTasksCount", 0),
                "PendingTasksCount": cluster.get("pendingTasksCount", 0),
                "ActiveServicesCount": cluster.get("activeServicesCount", 0),
                "ServicesCount": len(service_arns),
                "OptimizationOpportunities": [],
            }

            if cluster_info["RunningTasksCount"] == 0 and cluster_info["PendingTasksCount"] == 0:
                cluster_info["OptimizationOpportunities"].append("Empty cluster - consider deletion if unused")
                cluster_info["CheckCategory"] = "Idle Resources"

            if cluster_info["ServicesCount"] > 0:
                cluster_info["OptimizationOpportunities"].append(
                    "Review service resource allocation and scaling policies"
                )
                cluster_info["CheckCategory"] = "Over-provisioned Containers"

            cluster_info["OptimizationOpportunities"].extend(
                [
                    "Consider Fargate Spot for fault-tolerant tasks (Save 70%)",
                    "Implement task auto scaling based on metrics",
                    "Review CPU and memory allocation for rightsizing",
                ]
            )

            analysis["clusters"].append(cluster_info)
            analysis["total_services"] += cluster_info["ServicesCount"]

        return analysis

    except Exception as e:
        ctx.warn(f"Could not analyze ECS: {e}", "ecs")
        return {"total_clusters": 0, "clusters": [], "total_services": 0, "optimization_opportunities": []}


def get_eks_analysis(ctx: ScanContext) -> dict[str, Any]:
    """Analyze EKS clusters."""
    try:
        eks = ctx.client("eks")

        paginator = eks.get_paginator("list_clusters")
        cluster_names: list[str] = []
        for page in paginator.paginate():
            cluster_names.extend(page.get("clusters", []))

        analysis: dict[str, Any] = {
            "total_clusters": len(cluster_names),
            "clusters": [],
            "optimization_opportunities": [],
        }

        for cluster_name in cluster_names:
            try:
                cluster_details = eks.describe_cluster(name=cluster_name)
                cluster = cluster_details["cluster"]

                paginator = eks.get_paginator("list_nodegroups")
                nodegroup_names: list[str] = []
                for page in paginator.paginate(clusterName=cluster_name):
                    nodegroup_names.extend(page.get("nodegroups", []))

                cluster_info: dict[str, Any] = {
                    "ClusterName": cluster_name,
                    "Status": cluster.get("status", "UNKNOWN"),
                    "Version": cluster.get("version", "Unknown"),
                    "NodeGroupsCount": len(nodegroup_names),
                    "EstimatedMonthlyCost": 144,
                    "OptimizationOpportunities": [],
                }

                if cluster_info["Status"] != "ACTIVE":
                    cluster_info["OptimizationOpportunities"].append(
                        "Inactive cluster - consider deletion if not needed"
                    )

                if cluster_info["NodeGroupsCount"] == 0:
                    cluster_info["OptimizationOpportunities"].append("No node groups - cluster may be unused")

                cluster_info["OptimizationOpportunities"].extend(
                    [
                        "Consider Spot instances for non-critical workloads (Save 60-90%)",
                        "Implement cluster autoscaling to optimize node utilization",
                        "Review node group instance types for rightsizing opportunities",
                    ]
                )

                analysis["clusters"].append(cluster_info)

            except Exception as e:
                ctx.warn(f"Could not analyze EKS cluster {cluster_name}: {e}", "eks")

        return analysis

    except Exception as e:
        ctx.warn(f"Could not analyze EKS: {e}", "eks")
        return {"total_clusters": 0, "clusters": [], "optimization_opportunities": []}


def get_ecr_analysis(ctx: ScanContext) -> dict[str, Any]:
    """Analyze ECR repositories."""
    try:
        ecr = ctx.client("ecr")

        repos_response = ecr.describe_repositories()
        repositories = repos_response.get("repositories", [])

        analysis: dict[str, Any] = {
            "total_repositories": len(repositories),
            "repositories": [],
            "optimization_opportunities": [],
        }

        for repo in repositories:
            repo_name = repo["repositoryName"]

            try:
                paginator = ecr.get_paginator("list_images")
                image_count = 0
                for page in paginator.paginate(repositoryName=repo_name):
                    image_count += len(page.get("imageIds", []))
            except Exception as e:
                print(f"\u26a0\ufe0f Error getting image count for ECR repo {repo_name}: {str(e)}")
                image_count = 0

            repo_info: dict[str, Any] = {
                "RepositoryName": repo_name,
                "CreatedAt": repo["createdAt"].isoformat() if "createdAt" in repo else "Unknown",
                "ImageCount": image_count,
                "RepositoryUri": repo.get("repositoryUri", ""),
                "OptimizationOpportunities": [],
            }

            if image_count == 0:
                repo_info["OptimizationOpportunities"].append("Empty repository - consider deletion if unused")
            elif image_count > 100:
                repo_info["OptimizationOpportunities"].append("Large number of images - implement lifecycle policies")

            repo_info["OptimizationOpportunities"].extend(
                [
                    "Configure lifecycle policies to automatically delete old images",
                    "Use image scanning to identify vulnerabilities and reduce storage",
                ]
            )

            analysis["repositories"].append(repo_info)

        return analysis

    except Exception as e:
        ctx.warn(f"Could not analyze ECR: {e}", "ecr")
        return {"total_repositories": 0, "repositories": [], "optimization_opportunities": []}


def get_container_services_analysis(ctx: ScanContext) -> dict[str, Any]:
    """Get ECS, EKS, and ECR analysis for cost optimization."""
    return {
        "ecs": get_ecs_analysis(ctx),
        "eks": get_eks_analysis(ctx),
        "ecr": get_ecr_analysis(ctx),
    }


def get_enhanced_container_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Container Services (ECS/EKS/ECR) cost optimization checks."""
    checks: dict[str, list[dict[str, Any]]] = {
        "ecs_rightsizing": [],
        "eks_rightsizing": [],
        "ecr_lifecycle": [],
        "unused_clusters": [],
        "over_provisioned_services": [],
        "old_images": [],
    }

    try:
        ecs = ctx.client("ecs")

        # ECS Checks
        paginator = ecs.get_paginator("list_clusters")
        cluster_arns: list[str] = []
        for page in paginator.paginate():
            cluster_arns.extend(page.get("clusterArns", []))

        cluster_arns = list(set(cluster_arns))

        for cluster_arn in cluster_arns:
            cluster_name = cluster_arn.split("/")[-1]

            cluster_details = ecs.describe_clusters(clusters=[cluster_arn])
            cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}

            active_services = cluster.get("activeServicesCount", 0)
            running_tasks = cluster.get("runningTasksCount", 0)

            logger.debug(
                "Cluster %s active services=%d running tasks=%d",
                cluster_name,
                active_services,
                running_tasks,
            )

            if active_services == 0 and running_tasks == 0:
                checks["unused_clusters"].append(
                    {
                        "ClusterName": cluster_name,
                        "ClusterArn": cluster_arn,
                        "ActiveServices": active_services,
                        "RunningTasks": running_tasks,
                        "Recommendation": "Empty ECS cluster - consider deletion",
                        "EstimatedSavings": "100% of cluster overhead costs",
                        "CheckCategory": "Unused ECS Clusters",
                    }
                )
            elif running_tasks > 0 or active_services > 0:
                checks["ecs_rightsizing"].append(
                    {
                        "ClusterName": cluster_name,
                        "ClusterArn": cluster_arn,
                        "ActiveServices": active_services,
                        "RunningTasks": running_tasks,
                        "Recommendation": (
                            f"Active ECS cluster with {running_tasks} running tasks and {active_services} services"
                        ),
                        "EstimatedSavings": "Review for rightsizing opportunities",
                        "CheckCategory": "Active ECS Clusters",
                    }
                )

            try:
                paginator = ecs.get_paginator("list_services")
                service_arns: list[str] = []
                for page in paginator.paginate(cluster=cluster_arn):
                    service_arns.extend(page.get("serviceArns", []))

                if service_arns:
                    for i in range(0, len(service_arns), 10):
                        batch_arns = service_arns[i : i + 10]
                        services_details = ecs.describe_services(cluster=cluster_arn, services=batch_arns)

                        service_name = ""
                        for service in services_details.get("services", []):
                            service_name = service.get("serviceName", "")
                            desired_count = service.get("desiredCount", 0)
                            running_count = service.get("runningCount", 0)

                            if desired_count > running_count and desired_count > 1:
                                checks["over_provisioned_services"].append(
                                    {
                                        "ClusterName": cluster_name,
                                        "ServiceName": service_name,
                                        "DesiredCount": desired_count,
                                        "RunningCount": running_count,
                                        "Recommendation": "Service desired count exceeds running count",
                                        "EstimatedSavings": "Reduce desired count to match actual needs",
                                        "CheckCategory": "ECS Over-Provisioned Services",
                                    }
                                )

                        if not service_name:
                            continue

                        try:
                            cluster_details = ecs.describe_clusters(clusters=[cluster_name], include=["SETTINGS"])
                            cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}
                            settings = cluster.get("settings", [])

                            container_insights_enabled = False
                            for setting in settings:
                                if setting.get("name") == "containerInsights" and setting.get("value") in (
                                    "enabled",
                                    "enhanced",
                                ):
                                    container_insights_enabled = True
                                    break

                            if container_insights_enabled:
                                cloudwatch = ctx.client("cloudwatch")
                                end_time = datetime.now(UTC)
                                start_time = end_time - timedelta(days=7)

                                cpu_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/ECS",
                                    MetricName="CPUUtilization",
                                    Dimensions=[
                                        {"Name": "ServiceName", "Value": service_name},
                                        {"Name": "ClusterName", "Value": cluster_name},
                                    ],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                memory_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/ECS",
                                    MetricName="MemoryUtilization",
                                    Dimensions=[
                                        {"Name": "ServiceName", "Value": service_name},
                                        {"Name": "ClusterName", "Value": cluster_name},
                                    ],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                cpu_datapoints = cpu_response.get("Datapoints", [])
                                memory_datapoints = memory_response.get("Datapoints", [])

                                if cpu_datapoints and memory_datapoints:
                                    avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                    avg_memory = sum(dp["Average"] for dp in memory_datapoints) / len(memory_datapoints)
                                    max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)
                                    max_memory = max(dp["Maximum"] for dp in memory_datapoints)

                                    if avg_cpu < 20 and avg_memory < 30:
                                        checks["ecs_rightsizing"].append(
                                            {
                                                "ClusterName": cluster_name,
                                                "ServiceName": service_name,
                                                "Recommendation": (
                                                    f"Measured low utilization over 7 days "
                                                    f"(CPU: {avg_cpu:.1f}%, Memory: {avg_memory:.1f}%) "
                                                    "- consider downsizing task definition"
                                                ),
                                                "EstimatedSavings": (
                                                    "20-50% cost reduction based on measured over-provisioning"
                                                ),
                                                "CheckCategory": "ECS Rightsizing - Metric-Backed",
                                                "MetricsPeriod": "7 days",
                                                "AvgCPU": f"{avg_cpu:.1f}%",
                                                "AvgMemory": f"{avg_memory:.1f}%",
                                            }
                                        )
                                    elif max_cpu > 80 or max_memory > 80:
                                        pass
                            # No metrics available branch removed: "Enable Container Insights
                            # first" is a monitoring-enablement nudge, not a cost saving.

                        except Exception as e:
                            print(f"Warning: Could not check Container Insights for ECS cluster {cluster_name}: {e}")

            except Exception as e:
                print(f"Warning: Could not analyze ECS services for {cluster_name}: {e}")

        # EKS Checks
        try:
            eks = ctx.client("eks")

            paginator = eks.get_paginator("list_clusters")
            eks_cluster_names: list[str] = []
            for page in paginator.paginate():
                eks_cluster_names.extend(page.get("clusters", []))

            for cluster_name in eks_cluster_names:
                try:
                    cluster_response = eks.describe_cluster(name=cluster_name)
                    cluster = cluster_response.get("cluster", {})

                    container_insights_enabled = False
                    try:
                        try:
                            addons_response = eks.list_addons(clusterName=cluster_name)
                            addons = addons_response.get("addons", [])
                            container_insights_enabled = "amazon-cloudwatch-observability" in addons
                        except Exception:
                            pass

                        if not container_insights_enabled:
                            cloudwatch = ctx.client("cloudwatch")
                            end_time = datetime.now(UTC)
                            start_time = end_time - timedelta(days=1)

                            test_response = cloudwatch.get_metric_statistics(
                                Namespace="ContainerInsights",
                                MetricName="cluster_node_cpu_utilization",
                                Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average"],
                            )
                            container_insights_enabled = len(test_response.get("Datapoints", [])) > 0

                    except Exception:
                        container_insights_enabled = False

                    cluster_metrics_available = False
                    avg_cpu = max_cpu = avg_memory = max_memory = 0

                    if container_insights_enabled:
                        try:
                            cloudwatch = ctx.client("cloudwatch")
                            end_time = datetime.now(UTC)
                            start_time = end_time - timedelta(days=7)

                            cpu_response = cloudwatch.get_metric_statistics(
                                Namespace="ContainerInsights",
                                MetricName="cluster_node_cpu_utilization",
                                Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            memory_response = cloudwatch.get_metric_statistics(
                                Namespace="ContainerInsights",
                                MetricName="cluster_node_memory_utilization",
                                Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            cpu_datapoints = cpu_response.get("Datapoints", [])
                            memory_datapoints = memory_response.get("Datapoints", [])

                            if cpu_datapoints and memory_datapoints:
                                cluster_metrics_available = True
                                avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                avg_memory = sum(dp["Average"] for dp in memory_datapoints) / len(memory_datapoints)
                                max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)
                                max_memory = max(dp["Maximum"] for dp in memory_datapoints)

                        except Exception:
                            pass

                    paginator = eks.get_paginator("list_nodegroups")
                    nodegroup_names: list[str] = []
                    for page in paginator.paginate(clusterName=cluster_name):
                        nodegroup_names.extend(page.get("nodegroups", []))

                    if cluster_metrics_available:
                        if avg_cpu < 25 and avg_memory < 35:
                            checks["eks_rightsizing"].append(
                                {
                                    "ClusterName": cluster_name,
                                    "Recommendation": (
                                        f"Measured low cluster utilization over 7 days "
                                        f"(CPU: {avg_cpu:.1f}%, Memory: {avg_memory:.1f}%) "
                                        "- consider smaller instance types"
                                    ),
                                    "EstimatedSavings": ("30-60% cost reduction based on measured over-provisioning"),
                                    "CheckCategory": "EKS Rightsizing - Metric-Backed",
                                    "MetricsPeriod": "7 days",
                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                    "AvgMemory": f"{avg_memory:.1f}%",
                                }
                            )
                        # EKS Performance Optimization (peak usage scale-up suggestion):
                        # removed — explicitly "potential cost increase", not a saving.
                    # No-metrics fallback removed: "Enable Container Insights" is monitoring
                    # enablement, not a cost saving.

                    for nodegroup_name in nodegroup_names:
                        try:
                            ng_response = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name)
                            nodegroup = ng_response.get("nodegroup", {})

                            instance_types = nodegroup.get("instanceTypes", [])
                            scaling_config = nodegroup.get("scalingConfig", {})

                            desired_size = scaling_config.get("desiredSize", 0)

                            if instance_types and any("xlarge" in inst_type for inst_type in instance_types):
                                checks["eks_rightsizing"].append(
                                    {
                                        "ClusterName": cluster_name,
                                        "NodeGroupName": nodegroup_name,
                                        "InstanceTypes": instance_types,
                                        "DesiredSize": desired_size,
                                        "Recommendation": "Large instance types - verify rightsizing",
                                        "EstimatedSavings": "Potential 20-50% savings",
                                        "CheckCategory": "EKS Instance Rightsizing",
                                    }
                                )

                        except Exception as e:
                            print(f"Warning: Could not analyze EKS nodegroup {nodegroup_name}: {e}")

                except Exception as e:
                    print(f"Warning: Could not analyze EKS cluster {cluster_name}: {e}")

        except Exception as e:
            print(f"Warning: Could not perform EKS checks: {e}")

        # ECR Checks
        try:
            ecr = ctx.client("ecr")
            ecr_repos_response = ecr.describe_repositories()
            repositories = ecr_repos_response.get("repositories", [])

            for repo in repositories:
                repo_name = repo.get("repositoryName")

                try:
                    paginator = ecr.get_paginator("list_images")
                    images: list[dict[str, Any]] = []
                    for page in paginator.paginate(repositoryName=repo_name):
                        images.extend(page.get("imageIds", []))

                    if len(images) > HIGH_IMAGE_COUNT_THRESHOLD:
                        checks["old_images"].append(
                            {
                                "RepositoryName": repo_name,
                                "ImageCount": len(images),
                                "Recommendation": f"Repository has {len(images)} images - implement lifecycle policy",
                                "EstimatedSavings": "Reduce storage costs by cleaning old images",
                                "CheckCategory": "ECR Lifecycle Management",
                            }
                        )

                except Exception as e:
                    print(f"Warning: Could not analyze ECR repository {repo_name}: {e}")

        except Exception as e:
            print(f"Warning: Could not perform ECR checks: {e}")

    except Exception as e:
        print(f"Warning: Could not perform enhanced Container checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}
