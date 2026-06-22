"""RDS database optimization checks.

Extracted from CostOptimizer RDS-related methods as free functions.
This module will later become RdsModule (T-329) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

# Per-check reduction factors applied to live instance pricing.
# Values reflect typical AWS guidance:
#   - Non-prod schedule: nights + weekends shutdown ≈ 12h/day weekdays only,
#     yielding ≈ 65% reduction relative to 24/7.
#   - The RI scenario matrix below (1yr / 3yr × No Upfront / Partial Upfront /
#     All Upfront) reflects AWS public guidance averages. Discount tiers vary
#     by engine + instance family; treat these as planning estimates and
#     confirm exact savings in the AWS RI marketplace before purchase.
# (Multi-AZ savings use the exact live price delta, not a fixed reduction factor.)
RDS_NON_PROD_SCHEDULE_REDUCTION: float = 0.65
RDS_RI_DISCOUNT_MATRIX: tuple[tuple[str, str, float], ...] = (
    ("1yr", "No Upfront", 0.38),
    ("1yr", "Partial Upfront", 0.41),
    ("1yr", "All Upfront", 0.44),
    ("3yr", "No Upfront", 0.55),
    ("3yr", "Partial Upfront", 0.58),
    ("3yr", "All Upfront", 0.62),
)


def _rds_monthly_price(ctx: ScanContext, engine: str, instance_class: str, *, multi_az: bool) -> float:
    """Return RDS instance monthly $ price via PricingEngine, or 0.0 on failure."""
    if not ctx.pricing_engine or not engine or not instance_class:
        return 0.0
    try:
        return ctx.pricing_engine.get_rds_instance_monthly_price(
            engine, instance_class, multi_az=multi_az
        )
    except Exception as exc:
        logger.debug("RDS pricing lookup failed for %s %s: %s", engine, instance_class, exc)
        return 0.0

RDS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "idle_databases": {
        "title": "Stop or Delete Idle RDS Instances",
        "description": (
            "Idle RDS instances with low CPU utilization can be stopped to save costs or deleted if no longer needed."
        ),
        "action": (
            "1. Stop instance to save compute costs (storage still charged)\n"
            "2. Delete instance and create final snapshot\n"
            "3. Consider Aurora Serverless v2 for variable workloads\n"
            "4. Estimated savings: 100% of compute costs when stopped"
        ),
    },
    "rds_optimization": {
        "title": "Comprehensive RDS Cost Optimization",
        "description": (
            "RDS instances can be optimized through multiple strategies"
            " including rightsizing, engine optimization, and Reserved Instance purchases."
        ),
        "action": (
            "1. **Performance Analysis**: Review CloudWatch metrics for"
            " CPU (target 70-80%), memory, and IOPS utilization over 2-4 weeks\n"
            "2. **Rightsizing**: Downsize overprovisioned instances to match"
            " actual usage patterns\n"
            "3. **Graviton Migration**: Migrate to Graviton2/Graviton3 instances"
            " for 20% cost reduction\n"
            "4. **Reserved Instances**: Purchase 1-year or 3-year RIs for"
            " 30-72% savings on predictable workloads\n"
            "5. **Storage Optimization**: Migrate from gp2 to gp3 storage"
            " for 20% savings\n"
            "6. **Engine Optimization**: Consider Aurora for better"
            " performance per dollar\n"
            "7. **Multi-AZ Review**: Disable Multi-AZ for non-production"
            " environments\n"
            "8. **Backup Optimization**: Reduce backup retention for"
            " non-critical databases"
        ),
    },
    "instance_rightsizing": {
        "title": "Rightsize RDS Instance Classes",
        "description": ("Move to smaller instance classes based on actual CPU, memory, and I/O utilization patterns."),
        "action": (
            "1. Analyze CloudWatch metrics for CPU/memory usage\n"
            "2. Modify instance class during maintenance window\n"
            "3. Monitor performance after change\n"
            "4. Estimated savings: 20-50% based on rightsizing"
        ),
    },
    "reserved_instances": {
        "title": "Purchase RDS Reserved Instances",
        "description": ("Save up to 72% compared to On-Demand pricing with 1-year or 3-year commitments."),
        "action": (
            "1. Analyze usage patterns for steady workloads\n"
            "2. Purchase Reserved Instances (No/Partial/All Upfront)\n"
            "3. Apply to existing instances automatically\n"
            "4. Estimated savings: 30-72% vs On-Demand"
        ),
    },
    "storage_optimization": {
        "title": "Optimize RDS Storage Configuration",
        "description": ("Adjust storage type, size, and IOPS allocation based on actual usage patterns."),
        "action": (
            "1. Monitor storage metrics and IOPS utilization\n"
            "2. Reduce allocated storage if over-provisioned\n"
            "3. Switch from Provisioned IOPS to gp3 if appropriate\n"
            "4. Estimated savings: 10-30% on storage costs"
        ),
    },
}


def get_rds_instance_count(ctx: ScanContext) -> dict[str, int]:
    """Get RDS instance counts by engine and state."""
    logger.debug("RDS module active")
    rds = ctx.client("rds")
    _empty: dict[str, int] = {
        "total": 0,
        "running": 0,
        "stopped": 0,
        "mysql": 0,
        "postgres": 0,
        "aurora": 0,
        "oracle": 0,
        "sqlserver": 0,
    }
    try:
        paginator = rds.get_paginator("describe_db_instances")
        instances: list[dict[str, Any]] = []
        for page in paginator.paginate():
            instances.extend(page.get("DBInstances", []))

        counts: dict[str, int] = {
            "total": len(instances),
            "running": 0,
            "stopped": 0,
            "mysql": 0,
            "postgres": 0,
            "aurora": 0,
            "oracle": 0,
            "sqlserver": 0,
        }

        for instance in instances:
            if instance["DBInstanceStatus"] == "available":
                counts["running"] += 1
            elif instance["DBInstanceStatus"] == "stopped":
                counts["stopped"] += 1

            engine = instance.get("Engine", "").lower()
            if "mysql" in engine:
                counts["mysql"] += 1
            elif "postgres" in engine:
                counts["postgres"] += 1
            elif "aurora" in engine:
                counts["aurora"] += 1
            elif "oracle" in engine:
                counts["oracle"] += 1
            elif "sqlserver" in engine:
                counts["sqlserver"] += 1

        return counts
    except ClientError as ec:
        code = ec.response.get("Error", {}).get("Code", "")
        if code in ("UnauthorizedOperation", "AccessDenied"):
            ctx.permission_issue(
                f"describe_db_instances denied: {code}",
                service="rds",
                action="rds:DescribeDBInstances",
            )
        else:
            ctx.warn(f"Could not get RDS instance count: {ec}", service="rds")
        return _empty
    except Exception as e:
        ctx.warn(f"Unexpected error getting RDS instance count: {e}", service="rds")
        return _empty


def get_rds_compute_optimizer_recommendations(ctx: ScanContext) -> list[dict[str, Any]]:
    """Get RDS recommendations from Compute Optimizer.

    Delegates to services.advisor — the canonical location for all
    advisory-service (Cost Hub / Compute Optimizer) functions.
    """
    from services.advisor import get_rds_compute_optimizer_recommendations as _impl

    return _impl(ctx)


def get_enhanced_rds_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    old_snapshot_days: int = 90,
) -> dict[str, Any]:
    """Get enhanced RDS cost optimization checks."""
    rds = ctx.client("rds")
    region = ctx.region
    account_id = ctx.account_id

    # Only categories that are actually populated below. Idle / rightsizing /
    # storage / Aurora-Serverless candidate buckets were removed (audit L4):
    # idle and rightsizing come from Compute Optimizer, the gp2->gp3 storage
    # check was removed (C1), and the Aurora-Serverless nudges emitted no
    # concrete savings.
    checks: dict[str, Any] = {
        "reserved_instances": [],
        "multi_az_unnecessary": [],
        "backup_retention_excessive": [],
        "old_snapshots": [],
        "non_prod_scheduling": [],
    }

    try:
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            instances = page.get("DBInstances", [])

            for instance in instances:
                db_instance_id = instance.get("DBInstanceIdentifier")
                db_instance_class = instance.get("DBInstanceClass")
                engine = instance.get("Engine")
                db_instance_status = instance.get("DBInstanceStatus")
                multi_az = instance.get("MultiAZ", False)
                backup_retention = instance.get("BackupRetentionPeriod", 0)
                allocated_storage = instance.get("AllocatedStorage", 0)

                if db_instance_status not in ["available", "stopped"]:
                    continue

                if multi_az:
                    try:
                        tags_response = rds.list_tags_for_resource(
                            ResourceName=(f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}")
                        )
                        tags = {tag["Key"]: tag["Value"] for tag in tags_response.get("TagList", [])}

                        env_tag = tags.get(
                            "Environment",
                            tags.get("Stage", tags.get("Env", "")),
                        ).lower()
                        is_non_prod = env_tag in [
                            "dev",
                            "development",
                            "test",
                            "testing",
                            "staging",
                            "qa",
                            "non-prod",
                            "nonprod",
                        ]

                        if not is_non_prod:
                            is_non_prod = any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                            env_name = next(
                                (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                                "non-prod",
                            )
                        else:
                            env_name = env_tag or "non-prod"

                    except Exception as tag_exc:
                        logger.debug(
                            "RDS tag lookup failed for %s; falling back to name-substring heuristic: %s",
                            db_instance_id,
                            tag_exc,
                        )
                        is_non_prod = any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                        env_name = next(
                            (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                            "non-prod",
                        )

                    if is_non_prod:
                        multi_az_price = _rds_monthly_price(
                            ctx, engine or "", db_instance_class or "", multi_az=True
                        )
                        single_az_price = _rds_monthly_price(
                            ctx, engine or "", db_instance_class or "", multi_az=False
                        )
                        # Saving = Multi-AZ cost − Single-AZ cost (≈ 50% of Multi-AZ).
                        multi_az_savings = max(multi_az_price - single_az_price, 0.0)
                        checks["multi_az_unnecessary"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "DBInstanceClass": db_instance_class,
                                "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "MultiAZ": multi_az,
                                "Environment": env_name,
                                "Recommendation": (f"Disable Multi-AZ for {env_name} environment to reduce costs"),
                                "EstimatedSavings": (
                                    f"${multi_az_savings:.2f}/month with single-AZ deployment"
                                ),
                                "CheckCategory": "Multi-AZ Optimization",
                                "instanceFinding": (f"Multi-AZ enabled in {env_name} environment"),
                                "AuditBasis": {
                                    "rate_basis": "RDS on-demand instance price (live Pricing API)",
                                    "region": region,
                                    "engine": engine,
                                    "instance_class": db_instance_class,
                                    "metric_window": "non-prod tag/name heuristic (no CloudWatch usage)",
                                    "formula": (
                                        f"Multi-AZ ${multi_az_price:.2f} - Single-AZ ${single_az_price:.2f}"
                                    ),
                                },
                            }
                        )

                if backup_retention > 7:
                    extra_backup_days = backup_retention - 7
                    backup_price = (
                        ctx.pricing_engine.get_rds_backup_storage_price_per_gb()
                        if ctx.pricing_engine
                        else 0.095 * pricing_multiplier
                    )
                    # AWS provides free backup storage = 100% of total provisioned
                    # DB storage in the region. This estimate assumes that pool is
                    # already exhausted and additional retention is fully billable;
                    # accounts under the free tier may see lower realized savings.
                    backup_savings = allocated_storage * backup_price * extra_backup_days / 30
                    checks["backup_retention_excessive"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "engineVersion": instance.get("EngineVersion", ""),
                            "BackupRetentionPeriod": backup_retention,
                            "Recommendation": (f"Reduce backup retention from {backup_retention} to 7 days"),
                            "EstimatedSavings": (
                                f"${backup_savings:.2f}/month (estimate assumes regional"
                                " free tier — 100% of provisioned storage — is exhausted)"
                            ),
                            "CheckCategory": "Backup Retention Optimization",
                            "instanceFinding": (
                                f"{backup_retention} days retention (recommend 7 days for non-critical DBs)"
                            ),
                            "AuditBasis": {
                                "rate_basis": "RDS backup storage $/GB-month (live Pricing API)",
                                "region": region,
                                "engine": engine,
                                "rate": round(backup_price, 4),
                                "metric_window": "describe-API retention; assumes free tier exhausted",
                                "formula": (
                                    f"{allocated_storage}GB x ${backup_price:.4f} x "
                                    f"({backup_retention}-7)/30 days"
                                ),
                            },
                        }
                    )

                # gp2 -> gp3 storage-migration finding removed (audit C1): unlike EBS,
                # RDS gp2 and gp3 *base* storage cost the same per GB ($0.115/GB-Mo,
                # every engine, verified via the Pricing API). The old flat 20% was
                # phantom savings. gp3's real benefit is its included 3000 IOPS /
                # 125 MBps baseline, so any saving requires reading provisioned IOPS
                # above that baseline — there is no defensible flat per-GB delta.
                # io1/io2/gp3 "review IOPS/throughput" finding likewise removed:
                # requires workload analysis to quantify — emitted no concrete savings.

                if any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"]):
                    env_name = next(
                        (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                        "non-prod",
                    )
                    if engine in ["mysql", "postgres", "mariadb"]:
                        sched_base_price = _rds_monthly_price(
                            ctx, engine, db_instance_class or "", multi_az=multi_az
                        )
                        sched_savings = sched_base_price * RDS_NON_PROD_SCHEDULE_REDUCTION
                        checks["non_prod_scheduling"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "DBInstanceClass": db_instance_class,
                                "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "Environment": env_name,
                                "Recommendation": (
                                    f"Implement start/stop schedule for {env_name} database (stop nights/weekends)"
                                ),
                                "EstimatedSavings": (
                                    f"${sched_savings:.2f}/month with nights/weekends shutdown"
                                ),
                                "CheckCategory": "Non-Production Scheduling",
                                "instanceFinding": (f"{env_name} database - eligible for automated scheduling"),
                                "AuditBasis": {
                                    "rate_basis": "RDS on-demand instance price (live Pricing API)",
                                    "region": region,
                                    "engine": engine,
                                    "instance_class": db_instance_class,
                                    "metric_window": "name-substring heuristic (no CloudWatch usage)",
                                    "formula": (
                                        f"${sched_base_price:.2f} x {RDS_NON_PROD_SCHEDULE_REDUCTION} "
                                        "(nights/weekends shutdown)"
                                    ),
                                },
                            }
                        )

                # Stopped-database housekeeping finding removed: "storage costs continue
                # while stopped" is informational, not a quantified saving.
                # Burstable instance review removed: "Requires CloudWatch analysis" is
                # a monitoring nudge, not a cost recommendation.

                if db_instance_status == "available":
                    is_likely_prod = not any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                    ri_text = "production databases" if is_likely_prod else "long-running databases"
                    ri_base_price = _rds_monthly_price(
                        ctx, engine or "", db_instance_class or "", multi_az=multi_az
                    )
                    ri_scenarios = [
                        {
                            "term": term,
                            "payment_option": payment,
                            "monthly_savings": round(ri_base_price * pct, 2),
                            "discount_pct": round(pct * 100, 1),
                            "ondemand_monthly_estimate": round(ri_base_price, 2),
                        }
                        for term, payment, pct in RDS_RI_DISCOUNT_MATRIX
                    ]
                    best_scenario = max(ri_scenarios, key=lambda s: s["monthly_savings"])
                    checks["reserved_instances"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "DBInstanceClass": db_instance_class,
                            "Recommendation": (f"Consider Reserved Instances for {ri_text}"),
                            "EstimatedSavings": (
                                f"up to ${best_scenario['monthly_savings']:.2f}/month "
                                f"({best_scenario['term']} {best_scenario['payment_option']})"
                            ),
                            "CheckCategory": ("Reserved Instance Opportunities"),
                            "instanceFinding": (f"Instance ({db_instance_class}) - RI candidate"),
                            "RIScenarios": ri_scenarios,
                            "OnDemandMonthlyEstimate": round(ri_base_price, 2),
                            "AuditBasis": {
                                "rate_basis": "RDS on-demand instance price (live Pricing API)",
                                "region": region,
                                "engine": engine,
                                "instance_class": db_instance_class,
                                "metric_window": "advisory — excluded from headline (see commitment_analysis)",
                                "formula": (
                                    f"${ri_base_price:.2f} x {best_scenario['discount_pct']}% "
                                    f"({best_scenario['term']} {best_scenario['payment_option']})"
                                ),
                            },
                        }
                    )

                # Aurora Serverless v2 migration nudge removed: $0/month with "quantify
                # after measuring idle hours" — no concrete savings.

        try:
            paginator = rds.get_paginator("describe_db_snapshots")
            for page in paginator.paginate(SnapshotType="manual"):
                for snapshot in page.get("DBSnapshots", []):
                    snapshot_id = snapshot.get("DBSnapshotIdentifier")
                    create_time = snapshot.get("SnapshotCreateTime")
                    snap_allocated_storage = snapshot.get("AllocatedStorage", 0)

                    if create_time:
                        age_days = (datetime.now(create_time.tzinfo) - create_time).days
                        if age_days > old_snapshot_days:
                            checks["old_snapshots"].append(
                                {
                                    "SnapshotId": snapshot_id,
                                    "resourceArn": (f"arn:aws:rds:{region}:{account_id}:snapshot:{snapshot_id}"),
                                    "AgeDays": age_days,
                                    "AllocatedStorage": snap_allocated_storage,
                                    "Recommendation": (
                                        f"Delete {age_days}-day old manual"
                                        " snapshot (savings based on"
                                        " allocated storage estimate)"
                                    ),
                                    "EstimatedSavings": (
                                        f"${snap_allocated_storage * (ctx.pricing_engine.get_rds_backup_storage_price_per_gb() if ctx.pricing_engine else 0.095 * pricing_multiplier):.2f}"
                                        "/month (coarse estimate)"
                                    ),
                                    "CheckCategory": "Old RDS Snapshots",
                                    "instanceFinding": (f"{age_days} days old ({snap_allocated_storage}GB)"),
                                    "AuditBasis": {
                                        "rate_basis": "RDS backup storage $/GB-month (live Pricing API)",
                                        "region": region,
                                        "metric_window": (
                                            f"describe-API snapshot age {age_days}d > {old_snapshot_days}d threshold"
                                        ),
                                        "formula": (
                                            f"{snap_allocated_storage}GB x backup $/GB-mo "
                                            "(allocated-size estimate, coarse)"
                                        ),
                                    },
                                }
                            )
        except Exception as e:
            ctx.warn(f"Could not check RDS snapshots: {e}", service="rds")

        try:
            paginator = rds.get_paginator("describe_db_clusters")
            for page in paginator.paginate():
                for cluster in page.get("DBClusters", []):
                    cluster_id = cluster.get("DBClusterIdentifier")
                    engine = cluster.get("Engine", "")

                    if "aurora" in engine.lower():
                        # Aurora Serverless v2 migration nudge and Aurora I/O-Optimized
                        # "review" finding both removed: each emitted $0/month with
                        # "quantify after measuring" — no concrete savings.
                        pass

            try:
                paginator = rds.get_paginator("describe_db_cluster_snapshots")
                for page in paginator.paginate(SnapshotType="manual"):
                    for snapshot in page.get("DBClusterSnapshots", []):
                        snapshot_id = snapshot.get("DBClusterSnapshotIdentifier")
                        create_time = snapshot.get("SnapshotCreateTime")
                        cluster_allocated_storage = snapshot.get("AllocatedStorage", 0)

                        if create_time:
                            age_days = (datetime.now(create_time.tzinfo) - create_time).days
                            if age_days > old_snapshot_days:
                                checks["old_snapshots"].append(
                                    {
                                        "SnapshotId": snapshot_id,
                                        "resourceArn": (
                                            f"arn:aws:rds:{region}:{account_id}:cluster-snapshot:{snapshot_id}"
                                        ),
                                        "AgeDays": age_days,
                                        "AllocatedStorage": (cluster_allocated_storage),
                                        "Recommendation": (
                                            f"Delete {age_days}-day old"
                                            " Aurora cluster snapshot"
                                            " (savings based on allocated"
                                            " storage estimate)"
                                        ),
                                        "EstimatedSavings": (
                                            f"${cluster_allocated_storage * (ctx.pricing_engine.get_rds_backup_storage_price_per_gb() if ctx.pricing_engine else 0.095 * pricing_multiplier):.2f}"
                                            "/month (coarse estimate)"
                                        ),
                                        "CheckCategory": ("Old Aurora Cluster Snapshots"),
                                        "instanceFinding": (
                                            f"{age_days} days old Aurora"
                                            " cluster snapshot"
                                            f" ({cluster_allocated_storage}GB)"
                                        ),
                                        "AuditBasis": {
                                            "rate_basis": "RDS backup storage $/GB-month (live Pricing API)",
                                            "region": region,
                                            "metric_window": (
                                                f"describe-API snapshot age {age_days}d > "
                                                f"{old_snapshot_days}d threshold"
                                            ),
                                            "formula": (
                                                f"{cluster_allocated_storage}GB x backup $/GB-mo "
                                                "(allocated-size estimate, coarse)"
                                            ),
                                        },
                                    }
                                )
            except Exception as e:
                ctx.warn(f"Could not check Aurora cluster snapshots: {e}", service="rds")
        except Exception as e:
            ctx.warn(f"Could not check Aurora clusters: {e}", service="rds")

    except Exception as e:
        ctx.warn(f"Could not perform enhanced RDS checks: {e}", service="rds")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
