"""Redshift cost optimization checks.

Extracted from CostOptimizer.get_enhanced_redshift_checks() as a free function.
This module will later become RedshiftModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# Advisory line single-sourced for the commitment levers (RI / Serverless
# Reservation) so the shim never emits a fabricated per-rec dollar that would
# disagree with the $0 the Redshift headline counts (Redshift H2). The realizable
# saving is owned by — and quantified in — the Commitment Analysis tab.
_RI_ADVISORY_SAVINGS: str = (
    "$0.00/month — advisory: commitment purchase; realizable saving quantified in Commitment Analysis"
)

# RS-1 — idle-cluster evidence. AWS bills a provisioned cluster by the hour
# whether or not anyone connects, and PAUSE suspends on-demand billing entirely,
# so an unused cluster wastes 100% of its compute spend. `DatabaseConnections`
# is the free, cluster-scoped signal.
_IDLE_WINDOW_DAYS = 30
# 30 days of hourly datapoints is 720. Requiring at least half of them means a
# cluster created mid-window, or one whose metric only partly published, abstains
# instead of reading as idle.
_MIN_IDLE_DATAPOINTS = 360


def _cluster_has_no_connections(ctx: ScanContext, cluster_id: str) -> bool | None:
    """True when no client connected to the cluster across the whole window.

    ``None`` means UNKNOWN and the caller must abstain: a short or partial series
    is not evidence of idleness, and neither is a denied read. AWS publishes
    ``DatabaseConnections`` continuously for an available cluster, so a
    present-and-zero series is the proof and an empty or short one is not (C13).
    """
    if getattr(ctx, "fast_mode", False):
        return None
    try:
        cw = ctx.client("cloudwatch")
        end = datetime.now(UTC)
        resp = cw.get_metric_statistics(
            Namespace="AWS/Redshift",
            MetricName="DatabaseConnections",
            Dimensions=[{"Name": "ClusterIdentifier", "Value": cluster_id}],
            StartTime=end - timedelta(days=_IDLE_WINDOW_DAYS),
            EndTime=end,
            Period=3600,
            Statistics=["Maximum"],
        )
        points = resp.get("Datapoints", [])
        if len(points) < _MIN_IDLE_DATAPOINTS:
            return None
        return not any((p.get("Maximum") or 0) > 0 for p in points)
    except Exception as exc:
        record_aws_error(
            ctx, exc, service="redshift",
            context=f"cloudwatch:GetMetricStatistics DatabaseConnections for cluster {cluster_id}",
        )
        return None


def _pause_eligible(cluster: dict[str, Any]) -> bool:
    """Whether AWS will actually let this cluster be paused.

    AWS refuses to pause a cluster with automated snapshots disabled, or an HSM
    cluster. A saving whose action AWS rejects is not a saving.
    """
    try:
        retention = int(cluster.get("AutomatedSnapshotRetentionPeriod") or 0)
    except (TypeError, ValueError):
        return False
    if retention <= 0:
        return False
    return not cluster.get("HsmStatus")


REDSHIFT_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "idle_clusters": {
        "title": "Pause Idle Redshift Clusters",
        "description": (
            "A provisioned cluster bills compute by the hour regardless of use; pausing "
            "suspends on-demand billing and is reversible."
        ),
        "action": "Pause clusters with no database connections, or delete them if obsolete",
    },
    "reserved_instances": {
        "title": "Purchase Redshift Reserved Instances",
        "description": (
            "Redshift Reserved Instances cut compute-node cost for predictable workloads "
            "(~30% at 1-year No-Upfront, deeper at 3-year). The realizable commitment saving "
            "is quantified in the Commitment Analysis tab."
        ),
        "action": "Purchase 1-year or 3-year Reserved Instances",
    }
}


def get_enhanced_redshift_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Redshift cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "idle_clusters": [],
        "reserved_instances": [],
        "serverless_optimization": [],
        "cluster_rightsizing": [],
    }

    try:
        redshift = ctx.client("redshift")
        paginator = redshift.get_paginator("describe_clusters")
        clusters: list[dict[str, Any]] = []
        for page in paginator.paginate():
            clusters.extend(page.get("Clusters", []))

        for cluster in clusters:
            cluster_id = cluster.get("ClusterIdentifier")
            node_type = cluster.get("NodeType")
            cluster_status = cluster.get("ClusterStatus")
            number_of_nodes = cluster.get("NumberOfNodes", 1)
            # RS-1 — read the RAW key for the counted lever. Reusing the
            # defaulted variable above would make the abstain guard unreachable,
            # which is the MSK-5 / GL-4 / WS-3 bug this repo has fixed three
            # times already.
            raw_nodes = cluster.get("NumberOfNodes")

            if cluster_status == "available" and node_type and raw_nodes:
                idle = _cluster_has_no_connections(ctx, str(cluster_id))
                if idle is True:
                    eligible = _pause_eligible(cluster)
                    checks["idle_clusters"].append(
                        {
                            "ClusterIdentifier": cluster_id,
                            "NodeType": node_type,
                            "NumberOfNodes": raw_nodes,
                            "MultiAZ": "yes" if cluster.get("MultiAZ") else "no",
                            "PauseEligible": eligible,
                            "MetricWindowDays": _IDLE_WINDOW_DAYS,
                            "IdleEvidence": True,
                            "CheckCategory": "Idle Cluster",
                            "Recommendation": (
                                "No database connections in the last "
                                f"{_IDLE_WINDOW_DAYS} days - pause the cluster (reversible) "
                                "to suspend on-demand compute billing"
                                if eligible
                                else "No database connections in the last "
                                f"{_IDLE_WINDOW_DAYS} days, but AWS cannot pause this "
                                "cluster (automated snapshots disabled, or HSM) - review it"
                            ),
                        }
                    )

            if cluster_status == "available" and cluster.get("ClusterCreateTime") and number_of_nodes >= 2:
                create_time = cluster.get("ClusterCreateTime")
                if isinstance(create_time, str):  # noqa: SIM108
                    cluster_age_days = (
                        datetime.now(UTC) - datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                    ).days
                else:
                    cluster_age_days = (datetime.now(UTC) - create_time).days  # type: ignore[operator]

                if cluster_age_days > 30:
                    checks["reserved_instances"].append(
                        {
                            "ClusterIdentifier": cluster_id,
                            "NodeType": node_type,
                            "NumberOfNodes": number_of_nodes,
                            "ClusterAge": f"{cluster_age_days} days",
                            "Recommendation": (
                                f"Consider Reserved Instances for this stable cluster"
                                f" (running {cluster_age_days} days); the realizable commitment"
                                f" saving is quantified in the Commitment Analysis tab"
                            ),
                            # Advisory commitment lever — the counted dollar is owned by
                            # commitment_analysis. No fabricated per-rec $ here (Redshift H2);
                            # the adapter finalises this to $0 advisory.
                            "EstimatedSavings": _RI_ADVISORY_SAVINGS,
                            "CheckCategory": "Reserved Instance Optimization",
                            "Note": "Suitable for predictable, long-running workloads",
                        }
                    )

            if number_of_nodes > 3:
                checks["cluster_rightsizing"].append(
                    {
                        "ClusterIdentifier": cluster_id,
                        "CurrentNodes": number_of_nodes,
                        "Recommendation": "Analyze query performance and consider reducing cluster size",
                        "EstimatedSavings": f"${(number_of_nodes - 2) * 100:.2f}/month potential",
                        "CheckCategory": "Cluster Rightsizing",
                    }
                )

        try:
            redshift_serverless = ctx.client("redshift-serverless")
            paginator = redshift_serverless.get_paginator("list_workgroups")
            for page in paginator.paginate():
                workgroups = page.get("workgroups", [])

                for workgroup in workgroups:
                    workgroup_name = workgroup.get("workgroupName")
                    status = workgroup.get("status")

                    if status == "AVAILABLE":
                        checks["serverless_optimization"].append(
                            {
                                "WorkgroupName": workgroup_name,
                                "Recommendation": (
                                    "Consider Serverless Reservations for predictable workloads;"
                                    " the realizable commitment saving is quantified in the"
                                    " Commitment Analysis tab"
                                ),
                                # Advisory commitment lever — no fabricated per-rec $ (Redshift H2);
                                # the adapter finalises this to $0 advisory.
                                "EstimatedSavings": _RI_ADVISORY_SAVINGS,
                                "CheckCategory": "Serverless Optimization",
                            }
                        )
        except Exception:
            pass

    except Exception as e:
        ctx.warn(f"Could not analyze Redshift resources: {e}", "redshift")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
