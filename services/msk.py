"""MSK cost optimization checks.

Extracted from CostOptimizer.get_enhanced_msk_checks() as a free function.
This module will later become MskModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

MSK_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "cluster_rightsizing": {
        "title": "Optimize MSK Cluster Sizing",
        "description": "Right-size MSK clusters or consider serverless for variable workloads.",
        "action": "Consider MSK Serverless or smaller broker instances",
    },
    "idle_clusters": {
        "title": "Delete Idle MSK Clusters",
        "description": (
            "A provisioned cluster bills broker hours plus provisioned storage around the"
            " clock, whether or not any client is connected."
        ),
        "action": "Delete clusters with no client connections on any broker",
    },
}

_IDLE_WINDOW_DAYS = 30


def _cluster_has_no_connections(
    ctx: ScanContext, cluster_name: str, num_brokers: int
) -> bool | None:
    """True when EVERY broker reported zero connections for the whole window.

    ``ConnectionCount`` is a DEFAULT-level (free) MSK metric published from the
    moment a cluster reaches ACTIVE, with dimensions ``Cluster Name`` **and**
    ``Broker ID`` — so the read must be made per broker. A cluster-name-only
    read matches no dimension set and returns nothing, which would then look
    exactly like idleness (the SM-1 trap).

    Note the polarity is the OPPOSITE of Transfer Family's BytesIn/BytesOut:
    those publish only while a connection exists, so THERE an empty series
    proves idleness. ConnectionCount publishes continuously, so here an empty
    series means the read failed to find the metric, and the caller must
    abstain. Returns:

    * ``True``  — datapoints present on every broker, all zero.
    * ``False`` — at least one broker saw a connection.
    * ``None``  — unknown: read failed, or a broker published no datapoints.
    """
    if ctx.fast_mode or num_brokers <= 0:
        return None
    try:
        cw = ctx.client("cloudwatch")
        end = datetime.now(UTC)
        start = end - timedelta(days=_IDLE_WINDOW_DAYS)
        for broker_id in range(1, num_brokers + 1):
            resp = cw.get_metric_statistics(
                Namespace="AWS/Kafka",
                MetricName="ConnectionCount",
                Dimensions=[
                    {"Name": "Cluster Name", "Value": cluster_name},
                    {"Name": "Broker ID", "Value": str(broker_id)},
                ],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Maximum"],
            )
            points = resp.get("Datapoints", [])
            if not points:
                return None
            if any((dp.get("Maximum") or 0) > 0 for dp in points):
                return False
        return True
    except Exception as exc:
        record_aws_error(
            ctx,
            exc,
            service="msk",
            context=f"cloudwatch:GetMetricStatistics ConnectionCount for cluster {cluster_name}",
        )
        return None


def get_enhanced_msk_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced MSK cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "cluster_rightsizing": [],
        "serverless_migration": [],
        "storage_optimization": [],
        "idle_clusters": [],
    }

    try:
        kafka = ctx.client("kafka")
        paginator = kafka.get_paginator("list_clusters")
        for page in paginator.paginate():
            clusters = page.get("ClusterInfoList", [])

            for cluster in clusters:
                cluster_name = cluster.get("ClusterName")
                state = cluster.get("State")
                broker_node_group = cluster.get("BrokerNodeGroupInfo", {})
                instance_type = broker_node_group.get("InstanceType")
                # MSK-5 — no default. The old ``, 3)`` fabricated a 3x multiplier
                # on every priced leg whenever MSK omitted the field, which was
                # merely a wrong *displayed* figure while everything was advisory
                # and becomes a wrong *counted* dollar now that the idle lever
                # below counts. Absent -> 0 -> every priced leg is omitted.
                num_brokers = cluster.get("NumberOfBrokerNodes") or 0

                # Real per-broker provisioned EBS size (MSK reports it under
                # BrokerNodeGroupInfo.StorageInfo.EBSStorageInfo.VolumeSize).
                # ``0`` means MSK did not report a size — keep it falsy so the
                # adapter omits the storage leg instead of inventing 100 GB (H3).
                storage_info = broker_node_group.get("StorageInfo", {})
                ebs_storage = storage_info.get("EBSStorageInfo", {})
                volume_size = ebs_storage.get("VolumeSize", 0)

                if state == "ACTIVE" and instance_type and "large" in instance_type:
                    rightsizing_rec: dict[str, Any] = {
                        "ClusterName": cluster_name,
                        "InstanceType": instance_type,
                        "Recommendation": (
                            "Review cluster utilization - consider MSK Serverless for variable workloads"
                        ),
                        "EstimatedSavings": "$200/month potential",
                        "CheckCategory": "Cluster Rightsizing",
                        "Note": "Verify actual throughput and utilization before downsizing",
                        "NumberOfBrokerNodes": num_brokers,
                    }
                    # H3: carry the real per-broker EBS volume size so the adapter
                    # prices the storage leg from evidence, not a phantom 100 GB.
                    # Omit the key entirely when the size is unknown.
                    if volume_size:
                        rightsizing_rec["BrokerStorageGB"] = volume_size
                    checks["cluster_rightsizing"].append(rightsizing_rec)

                # MSK-1 — a provisioned cluster bills brokers + storage around the
                # clock. No size gate here (unlike the rightsizing lever's
                # "large" filter, MSK-2): a small idle cluster is just as wasted.
                if state == "ACTIVE" and instance_type and num_brokers > 0:
                    idle = _cluster_has_no_connections(ctx, str(cluster_name), num_brokers)
                    if idle is True:
                        idle_rec: dict[str, Any] = {
                            "ClusterName": cluster_name,
                            "InstanceType": instance_type,
                            "NumberOfBrokerNodes": num_brokers,
                            "MetricWindowDays": _IDLE_WINDOW_DAYS,
                            "IdleEvidence": True,
                            "Recommendation": (
                                "No client connected to any broker in the last "
                                f"{_IDLE_WINDOW_DAYS} days - delete the cluster if it is "
                                "no longer needed"
                            ),
                            "CheckCategory": "Idle MSK Cluster",
                        }
                        if volume_size:
                            idle_rec["BrokerStorageGB"] = volume_size
                        checks["idle_clusters"].append(idle_rec)

                if volume_size > 1000:
                    checks["storage_optimization"].append(
                        {
                            "ClusterName": cluster_name,
                            "VolumeSize": f"{volume_size} GB",
                            "BrokerStorageGB": volume_size,
                            "NumberOfBrokerNodes": num_brokers,
                            "Recommendation": "Large EBS volumes - review retention policies and consider gp3 volumes",
                            "EstimatedSavings": "20% with gp3 migration + retention optimization",
                            "CheckCategory": "MSK Storage Optimization",
                        }
                    )

        try:
            paginator_v2 = kafka.get_paginator("list_clusters_v2")
            for page in paginator_v2.paginate():
                serverless_clusters = page.get("ClusterInfoList", [])

                # MSK Serverless monitor finding removed: "Variable based on usage" with
                # no concrete per-cluster quantification.
                _ = serverless_clusters
        except Exception:
            pass

    except Exception as e:
        ctx.warn(f"Could not analyze MSK resources: {e}", "msk")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
