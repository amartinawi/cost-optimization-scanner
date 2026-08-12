"""OpenSearch cost optimization checks.

Extracted from CostOptimizer.get_enhanced_opensearch_checks() as a free function.
This module will later become OpenSearchModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

LOW_CPU_THRESHOLD: int = 20

# M360-3 / C18 — an OpenSearch data node is HEAP-bound, not CPU-bound: the JVM
# holds field data, indices and caches, and a one-rung downsize halves the node's
# RAM and therefore its heap. Low CPU is the normal profile of a search cluster
# serving from a warm heap, so it cannot on its own justify shrinking one.
#
# The bound is derived, not invented: halving the heap roughly doubles pressure,
# and AWS treats sustained JVMMemoryPressure above 75% as GC territory, so a node
# may only be downsized while its observed MAXIMUM sits below half of that. Same
# reasoning ElastiCache used for its 35% (services/elasticache.py:21).
MAX_JVM_PRESSURE_PCT: float = 37.5


def heap_headroom_ok(peak_jvm_pressure_pct: float | None) -> bool:
    """True if a one-size-down node would still hold this domain's heap.

    ``None`` (metric unreadable) returns False: absence of evidence is not
    evidence of headroom, and this gates a COUNTED dollar (C18).
    """
    if peak_jvm_pressure_pct is None:
        return False
    return peak_jvm_pressure_pct <= MAX_JVM_PRESSURE_PCT


def _es_max(
    cloudwatch: Any, metric: str, domain_name: str, ctx: Any, start_time: Any, end_time: Any
) -> float | None:
    """Highest datapoint Maximum for an AWS/ES metric, or None if unreadable.

    The MAXIMUM is what a downsize must survive: a node has to hold its heap at
    the domain's most pressured moment, so an average would understate it.
    """
    try:
        resp = cloudwatch.get_metric_statistics(
            Namespace="AWS/ES",
            MetricName=metric,
            Dimensions=[
                {"Name": "DomainName", "Value": domain_name},
                {"Name": "ClientId", "Value": ctx.account_id},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Maximum"],
        )
        dps = resp.get("Datapoints", [])
        return max(d["Maximum"] for d in dps) if dps else None
    except Exception:  # noqa: BLE001 — absence of evidence, not evidence of headroom
        return None


def _is_graviton_search_type(instance_type: str) -> bool:
    """True if an OpenSearch instance type is a Graviton (ARM) family.

    Graviton families carry a generation digit immediately followed by 'g'
    (m6g, r7g, c8g, t4g, m8g, and future gens). Detect by token pattern, not a
    static allowlist — the old list omitted 8th-gen m8g/r8g/c8g and so flagged
    already-Graviton nodes for a fabricated x86->Graviton migration (live-audit C1).
    """
    family = str(instance_type).split(".")[0]
    return bool(re.search(r"[0-9]g", family))


OPENSEARCH_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "reserved_instances": {
        "title": "Reserved Instances for OpenSearch",
        "description": "Consider Reserved Instances for stable, multi-instance OpenSearch domains.",
        "action": "Evaluate 1-3 year Reserved Instance commitments for production domains",
    },
    "underutilized_domains": {
        "title": "Right-size Underutilized Domains",
        "description": "Domains with low CPU utilization may be over-provisioned.",
        "action": "Downsize instance type for underutilized domains",
    },
    "old_versions": {
        "title": "Upgrade OpenSearch/Elasticsearch Version",
        "description": "Older engine versions miss performance improvements and cost optimizations.",
        "action": "Upgrade to latest OpenSearch 2.x or migrate from Elasticsearch",
    },
    "storage_optimization": {
        "title": "Migrate gp2 to gp3 Storage",
        "description": "OpenSearch-managed gp3 storage is priced lower per GB than gp2 "
        "(~$0.013/GB-month less, us-east-1) with equal or better performance.",
        "action": "Migrate EBS volumes from gp2 to gp3",
    },
    "idle_domains": {
        "title": "Remove Idle OpenSearch Domains",
        "description": "Domains with near-zero CPU utilization may be abandoned.",
        "action": "Delete idle domains to save 100% of domain cost",
    },
    "graviton_migration": {
        "title": "Migrate to Graviton Instances",
        "description": "Graviton instances offer 20-40% price-performance improvement over x86.",
        "action": "Migrate to Graviton-based instance types (e.g., r7g, m7g)",
    },
}


def get_enhanced_opensearch_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced OpenSearch cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "reserved_instances": [],
        "underutilized_domains": [],
        "old_versions": [],
        "storage_optimization": [],
        "idle_domains": [],
        "graviton_migration": [],
    }

    try:
        opensearch = ctx.client("opensearch")

        response = opensearch.list_domain_names()
        for domain_info in response.get("DomainNames", []):
            domain_name = domain_info["DomainName"]

            try:
                domain = opensearch.describe_domain(DomainName=domain_name)["DomainStatus"]

                engine_version = domain.get("EngineVersion", "")
                instance_type = domain.get("ClusterConfig", {}).get("InstanceType", "")
                instance_count = domain.get("ClusterConfig", {}).get("InstanceCount", 0)
                # OS-7 — dedicated master and UltraWarm nodes bill ON TOP of the
                # data nodes and are deleted with the domain, so an idle-domain
                # figure that prices only ClusterConfig.InstanceType/InstanceCount
                # under-counts every domain that has them (a 3-node master tier is
                # a common production default).
                cluster = domain.get("ClusterConfig", {}) or {}
                master_type = cluster.get("DedicatedMasterType") if cluster.get("DedicatedMasterEnabled") else None
                master_count = cluster.get("DedicatedMasterCount", 0) if master_type else 0
                warm_type = cluster.get("WarmType") if cluster.get("WarmEnabled") else None
                warm_count = cluster.get("WarmCount", 0) if warm_type else 0
                storage_type = domain.get("EBSOptions", {}).get("VolumeType", "")
                ebs_volume_size = domain.get("EBSOptions", {}).get("VolumeSize", 0)

                if instance_count >= 2:
                    checks["reserved_instances"].append(
                        {
                            "DomainName": domain_name,
                            "InstanceType": instance_type,
                            "InstanceCount": instance_count,
                            "Recommendation": "Consider Reserved Instances for stable workloads (1-3 year commitment)",
                            "EstimatedSavings": "30-60% vs On-Demand for committed usage",
                            "CheckCategory": "Reserved Instances Opportunity",
                        }
                    )

                if not _is_graviton_search_type(instance_type):
                    checks["graviton_migration"].append(
                        {
                            "DomainName": domain_name,
                            "InstanceType": instance_type,
                            "InstanceCount": instance_count,
                            # The master/warm tiers bill on top of the data nodes
                            # and migrate to Graviton independently, so the
                            # Graviton lever needs them for the same reason the
                            # idle-domain lever does (OS-7). Omitting them
                            # under-counted every domain whose master tier shares
                            # the data tier's x86 family — bnc production-bnc:
                            # 3 data + 3 master m5.xlarge.search, half the
                            # migration missing. The adapter prices each tier
                            # against its OWN Graviton counterpart and omits any
                            # tier that does not price.
                            "DedicatedMasterType": master_type,
                            "DedicatedMasterCount": master_count,
                            "WarmType": warm_type,
                            "WarmCount": warm_count,
                            "Recommendation": "Migrate to Graviton instances",
                            "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
                            "CheckCategory": "Graviton Migration",
                        }
                    )

                # Old OpenSearch / Elasticsearch version findings removed: version
                # upgrades emit freshness / EOL nudges with no concrete cost delta
                # (per-hour engine cost is identical across versions).

                if storage_type == "gp2":
                    checks["storage_optimization"].append(
                        {
                            "DomainName": domain_name,
                            "StorageType": storage_type,
                            # VolumeSize is PER DATA NODE (EBSOptions docs);
                            # the adapter multiplies by InstanceCount (OS-2).
                            "EBSVolumeSize": ebs_volume_size,
                            "InstanceCount": instance_count,
                            "Recommendation": "Migrate to gp3 volumes",
                            "EstimatedSavings": "20% storage cost",
                            "CheckCategory": "Storage Optimization",
                        }
                    )

                try:
                    end_time = datetime.now(UTC)
                    start_time = end_time - timedelta(days=14)

                    cloudwatch = ctx.client("cloudwatch")
                    cpu_response = cloudwatch.get_metric_statistics(
                        Namespace="AWS/ES",
                        MetricName="CPUUtilization",
                        Dimensions=[
                            {"Name": "DomainName", "Value": domain_name},
                            {"Name": "ClientId", "Value": ctx.account_id},
                        ],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,
                        Statistics=["Average"],
                    )

                    if cpu_response["Datapoints"]:
                        avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                            cpu_response["Datapoints"]
                        )

                        if avg_cpu < 5:
                            # Low CPU alone does NOT prove a domain is idle: a
                            # low-QPS search or log-analytics cluster sits at low
                            # CPU while still serving. Corroborate the irreversible
                            # DELETE with request-level activity (SearchRate +
                            # IndexingRate). Only a domain also doing ~no searches
                            # and ~no indexing is safely idle; otherwise the saving
                            # is rendered as a $0 advisory (adapter demotes on
                            # IdleCorroborated=False). No metric -> not corroborated
                            # (fail safe — never count a delete we cannot confirm).
                            def _es_avg(metric: str) -> float | None:
                                try:
                                    resp = cloudwatch.get_metric_statistics(
                                        Namespace="AWS/ES",
                                        MetricName=metric,
                                        Dimensions=[
                                            {"Name": "DomainName", "Value": domain_name},
                                            {"Name": "ClientId", "Value": ctx.account_id},
                                        ],
                                        StartTime=start_time,
                                        EndTime=end_time,
                                        Period=3600,
                                        Statistics=["Average"],
                                    )
                                    dps = resp.get("Datapoints", [])
                                    return (sum(d["Average"] for d in dps) / len(dps)) if dps else None
                                except Exception:
                                    return None

                            search_avg = _es_avg("SearchRate")
                            indexing_avg = _es_avg("IndexingRate")
                            # < 1 op/min averaged over 14d ≈ effectively unused.
                            corroborated = (
                                search_avg is not None
                                and indexing_avg is not None
                                and search_avg < 1.0
                                and indexing_avg < 1.0
                            )
                            checks["idle_domains"].append(
                                {
                                    "DomainName": domain_name,
                                    "InstanceType": instance_type,
                                    "InstanceCount": instance_count,
                                    "EBSVolumeSize": ebs_volume_size,
                                    # OS-7 — extra node tiers, deleted with the domain.
                                    "DedicatedMasterType": master_type,
                                    "DedicatedMasterCount": master_count,
                                    "WarmType": warm_type,
                                    "WarmCount": warm_count,
                                    "AvgCPU": round(avg_cpu, 2),
                                    "AvgSearchRate": round(search_avg, 3) if search_avg is not None else None,
                                    "AvgIndexingRate": round(indexing_avg, 3) if indexing_avg is not None else None,
                                    "IdleCorroborated": corroborated,
                                    "Recommendation": (
                                        "Delete idle domain"
                                        if corroborated
                                        else "Low CPU but search/indexing activity (or no metric) "
                                        "detected — verify the domain is unused before deleting"
                                    ),
                                    "EstimatedSavings": "100% of domain cost",
                                    "CheckCategory": "Idle Domain",
                                }
                            )
                        elif avg_cpu < LOW_CPU_THRESHOLD:
                            # M360-3 / C18 — low CPU alone cannot justify halving
                            # a data node's heap. JVMMemoryPressure is published
                            # free on AWS/ES, so this GATES on it; an unreadable
                            # metric withholds the lever and says so rather than
                            # counting a downsize it cannot defend.
                            peak_jvm = _es_max(
                                cloudwatch, "JVMMemoryPressure", domain_name,
                                ctx, start_time, end_time,
                            )
                            if not heap_headroom_ok(peak_jvm):
                                ctx.warn(
                                    f"OpenSearch domain {domain_name}: CPU is low "
                                    f"({avg_cpu:.0f}%) but "
                                    + (
                                        f"peak JVMMemoryPressure {peak_jvm:.0f}% exceeds "
                                        f"the {MAX_JVM_PRESSURE_PCT:.0f}% a halved heap allows"
                                        if peak_jvm is not None
                                        else "JVMMemoryPressure could not be read"
                                    )
                                    + " — downsize not counted",
                                    "opensearch",
                                )
                                continue
                            checks["underutilized_domains"].append(
                                {
                                    "DomainName": domain_name,
                                    "InstanceType": instance_type,
                                    "InstanceCount": instance_count,
                                    "AvgCPU": round(avg_cpu, 2),
                                    # The heap evidence that permits this downsize,
                                    # on the card (C18 — a reader must see WHY).
                                    "PeakJVMMemoryPressure": round(peak_jvm, 2),
                                    "Recommendation": "Downsize instance type",
                                    "EstimatedSavings": "30-50%",
                                    "CheckCategory": "Underutilized Domain",
                                }
                            )
                except Exception as e:
                    logger.warning(f"Warning: Could not get metrics for domain {domain_name}: {e}")
                    continue

            except Exception as e:
                logger.warning(f"\u26a0\ufe0f Error analyzing OpenSearch domain {domain_name}: {str(e)}")

    except Exception as e:
        ctx.warn(f"Could not analyze OpenSearch domains: {e}", "opensearch")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
