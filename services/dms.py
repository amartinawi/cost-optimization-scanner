"""DMS cost optimization checks.

Extracted from CostOptimizer.get_enhanced_dms_checks() as a free function.
This module will later become DmsModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# Replication-instance states in which the instance is NOT yet (or no longer)
# billing: pre-live provisioning and terminal failure. Every other reported
# state — ``available``, ``modifying``, ``upgrading``, ``maintenance``,
# ``storage-full`` — continues to accrue charges and is in scope (dms L4).
_NON_BILLABLE_STATES: frozenset[str] = frozenset({"creating", "deleting", "failed"})


def _attached_replication_task_count(ctx: ScanContext, dms: Any, instance_id: str, instance_arn: str | None) -> int:
    """Count replication tasks attached to a DMS instance (any state).

    A low-CPU replication instance is only safe to recommend for TERMINATION when
    no replication task is attached — a running CDC task can sit well under 5% CPU
    yet still be actively replicating, and deleting the instance would destroy it
    (dms F3). This fails SAFE: a missing ARN or an API error returns 1 ("assume a
    task is attached") so the caller never recommends terminating an instance
    whose task state could not be confirmed.

    Returns:
        Number of attached replication tasks, or 1 when the state is unknown.
    """
    if not instance_arn:
        return 1
    try:
        count = 0
        marker: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Filters": [{"Name": "replication-instance-arn", "Values": [instance_arn]}],
                "MaxRecords": 100,
            }
            if marker:
                kwargs["Marker"] = marker
            resp = dms.describe_replication_tasks(**kwargs)
            count += len(resp.get("ReplicationTasks", []))
            marker = resp.get("Marker")
            if not marker:
                break
        return count
    except Exception as e:
        record_aws_error(ctx, e, service="dms", context=f"describe_replication_tasks for {instance_id}")
        return 1

DMS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "serverless_migration": {
        "title": "DMS Serverless Migration Review",
        "description": "Monitor DMS Serverless usage patterns for cost optimization opportunities.",
        "action": "Review serverless replication configs and assess cost vs provisioned instances",
    },
    "instance_rightsizing": {
        "title": "Optimize DMS Instance Sizing",
        "description": "Right-size DMS instances or migrate to serverless for variable workloads.",
        "action": "Consider DMS Serverless or smaller instance types",
    },
    "unused_instances": {
        "title": "Unused DMS Instances",
        "description": "DMS instances with very low CPU utilization that may be candidates for termination.",
        "action": "Verify instance is truly unused, then stop or delete to save full instance cost",
    },
}


def get_enhanced_dms_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced DMS cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "serverless_migration": [],
        "instance_rightsizing": [],
        "unused_instances": [],
    }

    try:
        dms = ctx.client("dms")

        paginator = dms.get_paginator("describe_replication_instances")

        for page in paginator.paginate():
            instances = page.get("ReplicationInstances", [])

            for instance in instances:
                instance_id = instance.get("ReplicationInstanceIdentifier")
                instance_class = instance.get("ReplicationInstanceClass")
                status = instance.get("ReplicationInstanceStatus")
                # Carry the deployment mode so the adapter can pin the correct
                # AZ-specific Pricing SKU (InstanceUsg vs Multi-AZUsg) and drive
                # the Multi-AZ->Single-AZ per-AZ-delta lever (dms H1/H2).
                multi_az = bool(instance.get("MultiAZ", False))

                if status not in _NON_BILLABLE_STATES and instance_class:
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        cloudwatch = ctx.client("cloudwatch")
                        cpu_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DMS",
                            MetricName="CPUUtilization",
                            Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": instance_id}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )

                        datapoints = cpu_metrics.get("Datapoints", [])
                        # Zero datapoints → no metric coverage (brand-new instance
                        # or CW denied). ``sum()/max(len,1)`` yields 0.0, which
                        # passes both ``<30`` and ``<5`` and would flag a
                        # metric-less instance as rightsized AND unused (dms C2).
                        # Abstain rather than fabricate either finding.
                        if not datapoints:
                            continue
                        avg_cpu = sum(point["Average"] for point in datapoints) / len(datapoints)

                        # Mutually exclusive: a very-low-CPU instance is EITHER
                        # unused (terminate, full cost) OR rightsized (downsize,
                        # ~35%), never both — the previous nested ``<5`` inside
                        # ``<30`` double-counted one InstanceId at ~0.70× monthly
                        # (dms C1).
                        if avg_cpu < 5:
                            # dms F3 — a counted "terminate at full cost" rec is
                            # only safe when NO replication task is attached. Low
                            # CPU on an instance with a running/configured task is
                            # in-use, not unused; recommending termination there is
                            # dangerous (it would delete active replication). When
                            # tasks are attached (or the state can't be read), demote
                            # to a $0 advisory instead of a counted reclaim.
                            task_count = _attached_replication_task_count(
                                ctx, dms, instance_id, instance.get("ReplicationInstanceArn")
                            )
                            rec = {
                                "InstanceId": instance_id,
                                "InstanceClass": instance_class,
                                "MultiAZ": multi_az,
                                "AvgCPU": f"{avg_cpu:.1f}%",
                                "CheckCategory": "Unused DMS Instances",
                            }
                            if task_count == 0:
                                rec["Recommendation"] = (
                                    "Very low CPU and no replication tasks attached "
                                    "- stop or delete to save the full instance cost"
                                )
                                rec["EstimatedSavings"] = "Full instance cost if terminated"
                            else:
                                rec["Counted"] = False
                                rec["Recommendation"] = (
                                    f"Very low CPU ({avg_cpu:.1f}%) but {task_count} replication "
                                    "task(s) still attached - review before terminating; "
                                    "rightsizing may be safer than deletion"
                                )
                                rec["EstimatedSavings"] = "advisory - instance has attached replication tasks"
                                rec["PricingWarning"] = (
                                    "low CPU but attached replication task(s) present; terminating would "
                                    "delete configured replication - not a safe unconditional reclaim"
                                )
                            checks["unused_instances"].append(rec)
                        elif avg_cpu < 30:
                            checks["instance_rightsizing"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceClass": instance_class,
                                    "MultiAZ": multi_az,
                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                    "Recommendation": (
                                        f"Low CPU utilization ({avg_cpu:.1f}%) "
                                        "- consider DMS Serverless or smaller instance"
                                    ),
                                    "EstimatedSavings": "Rightsize for ~35% savings on instance cost",
                                    "CheckCategory": "Instance Optimization",
                                }
                            )
                    except Exception as e:
                        # Account-wide CW AccessDenied/Throttling must NOT
                        # silently append a counted 35% rec for every instance
                        # (dms C3). Classify the error and emit a $0 advisory
                        # only — never counted.
                        record_aws_error(ctx, e, service="dms", context=f"DMS CPUUtilization metric for {instance_id}")

        # DMS Serverless monitor finding removed: "Variable based on usage" with
        # no per-config quantification — monitoring nudge, not a saving. The
        # describe_replication_configs enumeration that fed it was left behind and
        # called via get_paginator(), but botocore has no paginator for that
        # operation, so it raised "Operation cannot be paginated" on every scan
        # while discarding its result (LW-04). The dead block is deleted; if the
        # serverless lever is reinstated, enumerate with a direct
        # describe_replication_configs() Marker loop (no paginator exists).

    except Exception as e:
        # Promote AccessDenied/UnauthorizedOperation to ctx.permission_issue;
        # everything else routes to ctx.warn (dms L3).
        record_aws_error(ctx, e, service="dms", context="Could not analyze DMS resources")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
