"""OpenSearch cost optimization checks.

Extracted from CostOptimizer.get_enhanced_opensearch_checks() as a free function.
This module will later become OpenSearchModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

print("\U0001f50d [services/opensearch.py] OpenSearch module active")

LOW_CPU_THRESHOLD: int = 20

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
        "description": "gp3 volumes offer 20% cost savings over gp2 with better performance.",
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

                graviton_families = ["m7g", "r7g", "m6g", "r6g", "c7g", "c6g", "t4g"]
                is_graviton = any(instance_type.startswith(family) for family in graviton_families)

                if not is_graviton:
                    checks["graviton_migration"].append(
                        {
                            "DomainName": domain_name,
                            "InstanceType": instance_type,
                            "Recommendation": "Migrate to Graviton instances",
                            "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
                            "CheckCategory": "Graviton Migration",
                        }
                    )

                if "OpenSearch" in engine_version:
                    version = engine_version.replace("OpenSearch_", "")
                    major_version = float(version.split(".")[0]) if version else 0
                    if major_version < 2:
                        checks["old_versions"].append(
                            {
                                "DomainName": domain_name,
                                "EngineVersion": engine_version,
                                "Recommendation": "Upgrade to OpenSearch 2.x",
                                "CheckCategory": "Old OpenSearch Version",
                            }
                        )
                elif "Elasticsearch" in engine_version:
                    version = engine_version.replace("Elasticsearch_", "")
                    major_version = float(version.split(".")[0]) if version else 0
                    if major_version < 7:
                        checks["old_versions"].append(
                            {
                                "DomainName": domain_name,
                                "EngineVersion": engine_version,
                                "Recommendation": "Upgrade to Elasticsearch 7.x or migrate to OpenSearch",
                                "CheckCategory": "Old Elasticsearch Version",
                            }
                        )

                if storage_type == "gp2":
                    checks["storage_optimization"].append(
                        {
                            "DomainName": domain_name,
                            "StorageType": storage_type,
                            "EBSVolumeSize": ebs_volume_size,
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
                            checks["idle_domains"].append(
                                {
                                    "DomainName": domain_name,
                                    "AvgCPU": round(avg_cpu, 2),
                                    "Recommendation": "Delete idle domain",
                                    "EstimatedSavings": "100% of domain cost",
                                    "CheckCategory": "Idle Domain",
                                }
                            )
                        elif avg_cpu < LOW_CPU_THRESHOLD:
                            checks["underutilized_domains"].append(
                                {
                                    "DomainName": domain_name,
                                    "AvgCPU": round(avg_cpu, 2),
                                    "Recommendation": "Downsize instance type",
                                    "EstimatedSavings": "30-50%",
                                    "CheckCategory": "Underutilized Domain",
                                }
                            )
                except Exception as e:
                    print(f"Warning: Could not get metrics for domain {domain_name}: {e}")
                    continue

            except Exception as e:
                print(f"\u26a0\ufe0f Error analyzing OpenSearch domain {domain_name}: {str(e)}")

    except Exception as e:
        ctx.warn(f"Could not analyze OpenSearch domains: {e}", "opensearch")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
