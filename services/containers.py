"""Container services (ECS, EKS, ECR) cost optimization checks.

Extracted from CostOptimizer as free functions taking ScanContext.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services.containers_logic import compute_untagged_reclaimable_bytes, quantify_fargate_rightsizing

logger = logging.getLogger(__name__)

HIGH_IMAGE_COUNT_THRESHOLD: int = 10
# Skip per-image manifest analysis above this repo size (cost guard); emit a
# Counted=False advisory instead so we never silently miss a huge repo.
ECR_MANIFEST_ANALYSIS_MAX_IMAGES: int = 600
# Accept every manifest media type so batch_get_image returns the raw manifest.
_ECR_ACCEPTED_MEDIA_TYPES: list[str] = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v1+json",
]

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
                logger.warning(f"\u26a0\ufe0f Error getting image count for ECR repo {repo_name}: {str(e)}")
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


def _fargate_launch_type(service: dict[str, Any], task_def: dict[str, Any]) -> str | None:
    """Return "FARGATE" if the service runs on Fargate, "EC2" if on EC2, else None.

    Checks the service launchType, its capacityProviderStrategy, and the task
    definition's requiresCompatibilities. EC2-launch-type tasks belong to the
    EC2 adapter and must not be Fargate-priced here.
    """
    launch = service.get("launchType", "")
    if launch == "FARGATE":
        return "FARGATE"
    if launch == "EC2":
        return "EC2"
    providers = [p.get("capacityProvider", "") for p in service.get("capacityProviderStrategy", [])]
    if any(p.startswith("FARGATE") for p in providers):
        return "FARGATE"
    if providers:  # named EC2 capacity provider
        return "EC2"
    compat = task_def.get("requiresCompatibilities", [])
    if "FARGATE" in compat:
        return "FARGATE"
    if "EC2" in compat:
        return "EC2"
    return None


def _arch_os(task_def: dict[str, Any]) -> tuple[str, str]:
    """Map a task definition's runtimePlatform to (architecture, os) labels."""
    rp = task_def.get("runtimePlatform", {}) or {}
    cpu_arch = str(rp.get("cpuArchitecture", "X86_64")).upper()
    os_family = str(rp.get("operatingSystemFamily", "LINUX")).upper()
    arch = "arm" if "ARM" in cpu_arch else "x86"
    os = "windows" if "WINDOWS" in os_family else "linux"
    return arch, os


def get_enhanced_container_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced ECS/ECR cost optimization checks.

    EKS is owned by the dedicated ``eks_cost`` adapter and is intentionally NOT
    analyzed here (avoids cross-adapter double display of EKS node groups).

    ECS rightsizing is metric-gated (Container Insights, 7-day window) and
    carries the task's real Cpu/Memory/TaskCount + launch type + architecture/OS
    so the adapter can compute a Fargate-priced saving snapped to a valid combo.
    ECR findings are advisory (no deduplicated-layer-storage data to quantify).
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "ecs_rightsizing": [],
        "ecr_lifecycle": [],
        "old_images": [],
    }

    # ECS checks ---------------------------------------------------------------
    checks["ecs_rightsizing"] = collect_ecs_fargate_rightsizing_recs(ctx)

    # ECR checks — realizable saving = deduplicated layer storage freed by
    # expiring untagged images (NOT the sum of imageSizeInBytes).
    try:
        ecr = ctx.client("ecr")
        try:
            repos: list[dict[str, Any]] = []
            paginator = ecr.get_paginator("describe_repositories")
            for page in paginator.paginate():
                repos.extend(page.get("repositories", []))
        except Exception:
            repos = ecr.describe_repositories().get("repositories", [])

        if ctx.fast_mode and repos:
            ctx.warn(
                "fast mode: skipping ECR image-manifest analysis; deduplicated "
                "layer-storage savings not quantified",
                "containers",
            )

        for repo in [] if ctx.fast_mode else repos:
            repo_name = repo.get("repositoryName")
            rec = _ecr_repo_reclaimable(ctx, ecr, repo_name)
            if rec:
                checks["old_images"].append(rec)
    except Exception as e:
        _ecr_failure(ctx, "describe_repositories", e)

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}


def collect_ecs_fargate_rightsizing_recs(ctx: ScanContext) -> list[dict[str, Any]]:
    """Return metric-backed ECS Fargate rightsizing recs across all clusters/services.

    Shared by ``get_enhanced_container_checks`` (containers tab) and
    ``estimate_fargate_rightsizing_monthly`` (commitment_analysis reconciliation)
    so the ECS/CloudWatch scan happens once per consumer. Respects fast_mode.
    """
    recs: list[dict[str, Any]] = []
    try:
        ecs = ctx.client("ecs")

        cluster_arns: list[str] = []
        try:
            paginator = ecs.get_paginator("list_clusters")
            for page in paginator.paginate():
                cluster_arns.extend(page.get("clusterArns", []))
        except Exception as e:
            _ecs_failure(ctx, "list_clusters", e)
            cluster_arns = []
        cluster_arns = list(set(cluster_arns))

        if ctx.fast_mode and cluster_arns:
            ctx.warn(
                "fast mode: skipping ECS Container Insights utilization reads; "
                "Fargate rightsizing savings not quantified",
                "containers",
            )

        for cluster_arn in cluster_arns:
            cluster_name = cluster_arn.split("/")[-1]
            if ctx.fast_mode:
                continue  # no per-service metric reads in fast mode

            try:
                service_arns: list[str] = []
                paginator = ecs.get_paginator("list_services")
                for page in paginator.paginate(cluster=cluster_arn):
                    service_arns.extend(page.get("serviceArns", []))
            except Exception as e:
                _ecs_failure(ctx, f"list_services({cluster_name})", e)
                continue
            if not service_arns:
                continue

            container_insights_enabled = _container_insights_enabled(ctx, ecs, cluster_name)
            for i in range(0, len(service_arns), 10):
                batch = service_arns[i : i + 10]
                try:
                    services_details = ecs.describe_services(cluster=cluster_arn, services=batch)
                except Exception as e:
                    _ecs_failure(ctx, f"describe_services({cluster_name})", e)
                    continue
                for service in services_details.get("services", []):
                    if not service.get("serviceName") or not container_insights_enabled:
                        continue
                    rec = _ecs_service_rightsizing(ctx, ecs, cluster_name, service)
                    if rec:
                        recs.append(rec)
    except Exception as e:
        _ecs_failure(ctx, "enhanced ECS checks", e)
    return recs


def estimate_fargate_rightsizing_monthly(ctx: ScanContext) -> float:
    """Total $/mo of ECS Fargate rightsizing — a lightweight, ECR-free estimate.

    Lets the commitment_analysis adapter model the Savings Plan against the
    rightsized Fargate baseline even when the Containers adapter did not run in
    the same scan (e.g. ``--scan-only commitment_analysis``). Returns 0.0 in
    fast mode or when nothing is over-provisioned. Mirrors the Containers
    adapter's quantification (valid-combo snapping, arch/OS-aware pricing).
    """
    if getattr(ctx, "fast_mode", False):
        return 0.0
    total = 0.0
    for rec in collect_ecs_fargate_rightsizing_recs(ctx):
        if str(rec.get("LaunchType", "")).upper() != "FARGATE":
            continue
        arch = rec.get("Architecture", "x86")
        os = rec.get("OperatingSystem", "linux")
        try:
            vcpu_rate = ctx.pricing_engine.get_fargate_vcpu_hourly(architecture=arch, os=os)
            gb_rate = ctx.pricing_engine.get_fargate_gb_hourly(architecture=arch, os=os)
            win_os = (
                ctx.pricing_engine.get_fargate_windows_os_hourly()
                if str(os).lower().startswith("win")
                else 0.0
            )
        except Exception:
            continue
        try:
            cpu_units = float(rec.get("Cpu", 0))
            mem_mb = float(rec.get("Memory", 0))
            task_count = int(rec.get("TaskCount", 0))
        except (TypeError, ValueError):
            continue
        q = quantify_fargate_rightsizing(
            cpu_units,
            mem_mb,
            task_count,
            vcpu_rate,
            gb_rate,
            windows_os_rate=win_os,
            peak_cpu_pct=rec.get("PeakCPUPct"),
            peak_mem_pct=rec.get("PeakMemoryPct"),
        )
        if q:
            total += q["saving"]
    return round(total, 2)


def _ecs_failure(ctx: ScanContext, op: str, exc: Exception) -> None:
    """Record an ECS API failure via ctx (permission vs transient)."""
    if "AccessDenied" in str(exc) or "not authorized" in str(exc):
        ctx.permission_issue(f"ECS {op} denied", "containers", "ecs")
    else:
        ctx.warn(f"ECS {op} failed: {exc}", "containers")


def _ecr_failure(ctx: ScanContext, op: str, exc: Exception) -> None:
    """Record an ECR API failure via ctx (permission vs transient)."""
    if "AccessDenied" in str(exc) or "not authorized" in str(exc):
        ctx.permission_issue(f"ECR {op} denied", "containers", "ecr")
    else:
        ctx.warn(f"ECR {op} failed: {exc}", "containers")


def _ecr_batch_get_manifests(
    ctx: ScanContext, ecr: Any, repo_name: str, digests: list[str]
) -> dict[str, dict[str, Any] | None]:
    """Fetch many image manifests in batched batch_get_image calls (<=100/call).

    Batching is essential for performance: one call per ~100 digests instead of
    one call per image (a 90-repo account otherwise issues thousands of calls).
    """
    out: dict[str, dict[str, Any] | None] = {}
    for i in range(0, len(digests), 100):
        chunk = digests[i : i + 100]
        try:
            resp = ecr.batch_get_image(
                repositoryName=repo_name,
                imageIds=[{"imageDigest": d} for d in chunk],
                acceptedMediaTypes=_ECR_ACCEPTED_MEDIA_TYPES,
            )
            for img in resp.get("images", []):
                digest = img.get("imageId", {}).get("imageDigest", "")
                raw = img.get("imageManifest", "")
                if digest:
                    try:
                        out[digest] = json.loads(raw) if raw else None
                    except (ValueError, TypeError):
                        out[digest] = None
        except Exception as e:
            _ecr_failure(ctx, f"batch_get_image({repo_name})", e)
    return out


def _ecr_manifest_fetcher(ctx: ScanContext, ecr: Any, repo_name: str, seed: dict[str, dict[str, Any] | None]) -> Any:
    """Return a memoized callable(digest) -> parsed manifest dict (or None).

    Seeded with the prefetched top-level manifests; child manifests of an image
    index that were not in the seed are fetched (batched) on first miss.
    """
    cache: dict[str, dict[str, Any] | None] = dict(seed)

    def fetch(digest: str) -> dict[str, Any] | None:
        if digest in cache:
            return cache[digest]
        fetched = _ecr_batch_get_manifests(ctx, ecr, repo_name, [digest])
        cache[digest] = fetched.get(digest)
        return cache[digest]

    return fetch


def _ecr_repo_reclaimable(ctx: ScanContext, ecr: Any, repo_name: str) -> dict[str, Any] | None:
    """Build a counted ECR rec from deduplicated layers freed by expiring untagged images.

    Lists image digests + tags, then walks manifests to compute the bytes of
    layers referenced ONLY by untagged images. Returns None when there is no
    untagged-only layer storage to reclaim (so a ~$0 finding is never emitted).
    The dollar value is priced by the adapter from ``ReclaimableBytes``.
    """
    images: list[dict[str, Any]] = []
    try:
        paginator = ecr.get_paginator("describe_images")
        for page in paginator.paginate(repositoryName=repo_name):
            for d in page.get("imageDetails", []):
                digest = d.get("imageDigest", "")
                if digest:
                    images.append({"digest": digest, "tagged": bool(d.get("imageTags"))})
    except Exception as e:
        _ecr_failure(ctx, f"describe_images({repo_name})", e)
        return None

    if not images:
        return None

    untagged = sum(1 for i in images if not i["tagged"])
    if untagged == 0:
        return None  # nothing a lifecycle "expire untagged" rule would remove

    if len(images) > ECR_MANIFEST_ANALYSIS_MAX_IMAGES:
        return {
            "RepositoryName": repo_name,
            "ImageCount": len(images),
            "Recommendation": (
                f"Repository has {len(images)} images ({untagged} untagged) - add a lifecycle "
                "policy to expire untagged/old images"
            ),
            "EstimatedSavings": (
                "Advisory: repo too large for per-image manifest analysis; savings not quantified"
            ),
            "CheckCategory": "ECR Lifecycle Management",
            "Counted": False,
            "Advisory": True,
        }

    # Prefetch every top-level image manifest in one batched pass, then walk.
    seed = _ecr_batch_get_manifests(ctx, ecr, repo_name, [i["digest"] for i in images])
    fetch = _ecr_manifest_fetcher(ctx, ecr, repo_name, seed)
    reclaimable_bytes = compute_untagged_reclaimable_bytes(images, fetch)
    if reclaimable_bytes <= 0:
        return None  # untagged images share all layers with tagged images → ~$0

    return {
        "RepositoryName": repo_name,
        "ImageCount": len(images),
        "UntaggedCount": untagged,
        "ReclaimableBytes": int(reclaimable_bytes),
        "Recommendation": (
            f"{untagged} untagged image(s) hold {reclaimable_bytes / 1024**3:.2f} GiB of "
            "layers not shared with tagged images - expire them via a lifecycle policy"
        ),
        "EstimatedSavings": "Computed from deduplicated layer storage freed",
        "CheckCategory": "ECR Lifecycle Management",
    }


def _container_insights_enabled(ctx: ScanContext, ecs: Any, cluster_name: str) -> bool:
    """Return True if Container Insights is enabled on the cluster."""
    try:
        details = ecs.describe_clusters(clusters=[cluster_name], include=["SETTINGS"])
        cluster = details["clusters"][0] if details.get("clusters") else {}
        for setting in cluster.get("settings", []):
            if setting.get("name") == "containerInsights" and setting.get("value") in ("enabled", "enhanced"):
                return True
    except Exception as e:
        _ecs_failure(ctx, f"describe_clusters({cluster_name})", e)
    return False


def _ecs_service_rightsizing(
    ctx: ScanContext, ecs: Any, cluster_name: str, service: dict[str, Any]
) -> dict[str, Any] | None:
    """Build a metric-backed Fargate rightsizing rec for an over-provisioned service.

    Reads AWS/ECS CPU/Memory utilization over 7 days; only emits when measured
    avg CPU < 20% and avg memory < 30%. Attaches the task's real Cpu/Memory/
    TaskCount + launch type + architecture/OS so the adapter can price it.
    Returns None when not over-provisioned or data is unavailable.
    """
    service_name = service.get("serviceName", "")
    running_count = service.get("runningCount", 0)
    desired_count = service.get("desiredCount", 0)
    task_count = running_count or desired_count
    if task_count <= 0:
        return None  # no running/desired tasks → no Fargate cost → no saving

    try:
        cloudwatch = ctx.client("cloudwatch")
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=7)
        dims = [
            {"Name": "ServiceName", "Value": service_name},
            {"Name": "ClusterName", "Value": cluster_name},
        ]
        cpu = cloudwatch.get_metric_statistics(
            Namespace="AWS/ECS", MetricName="CPUUtilization", Dimensions=dims,
            StartTime=start_time, EndTime=end_time, Period=3600, Statistics=["Average", "Maximum"],
        )
        mem = cloudwatch.get_metric_statistics(
            Namespace="AWS/ECS", MetricName="MemoryUtilization", Dimensions=dims,
            StartTime=start_time, EndTime=end_time, Period=3600, Statistics=["Average", "Maximum"],
        )
    except Exception as e:
        _ecs_failure(ctx, f"CloudWatch metrics({service_name})", e)
        return None

    cpu_dp = cpu.get("Datapoints", [])
    mem_dp = mem.get("Datapoints", [])
    if not cpu_dp or not mem_dp:
        return None  # no utilization evidence → do not fabricate a saving

    avg_cpu = sum(d["Average"] for d in cpu_dp) / len(cpu_dp)
    avg_mem = sum(d["Average"] for d in mem_dp) / len(mem_dp)
    if not (avg_cpu < 20 and avg_mem < 30):
        return None
    # Peak utilization gates how far we can safely downsize: the target must
    # still cover the measured peak (plus headroom), so a spiky task is not
    # cut to the tier minimum.
    max_cpu = max(d["Maximum"] for d in cpu_dp)
    max_mem = max(d["Maximum"] for d in mem_dp)

    # Resolve the task definition for real Cpu/Memory + launch type + platform.
    task_def_arn = service.get("taskDefinition", "")
    try:
        td_resp = ecs.describe_task_definition(taskDefinition=task_def_arn)
        task_def = td_resp.get("taskDefinition", {})
    except Exception as e:
        _ecs_failure(ctx, f"describe_task_definition({service_name})", e)
        return None

    launch_type = _fargate_launch_type(service, task_def)
    arch, os = _arch_os(task_def)
    try:
        cpu_units = int(task_def.get("cpu", 0))
        mem_mb = int(task_def.get("memory", 0))
    except (TypeError, ValueError):
        cpu_units = mem_mb = 0

    return {
        "ClusterName": cluster_name,
        "ServiceName": service_name,
        "Cpu": cpu_units,
        "Memory": mem_mb,
        "TaskCount": int(task_count),
        "LaunchType": launch_type or "UNKNOWN",
        "Architecture": arch,
        "OperatingSystem": os,
        "Recommendation": (
            f"Measured low utilization over 7 days (CPU {avg_cpu:.1f}%, Memory {avg_mem:.1f}%) "
            "- downsize the Fargate task to the next valid size"
        ),
        "EstimatedSavings": "Computed from task config snapped to a valid Fargate combo",
        "CheckCategory": "ECS Rightsizing - Metric-Backed",
        "MetricsPeriod": "7 days",
        "AvgCPU": f"{avg_cpu:.1f}%",
        "AvgMemory": f"{avg_mem:.1f}%",
        "PeakCPUPct": round(max_cpu, 1),
        "PeakMemoryPct": round(max_mem, 1),
    }
