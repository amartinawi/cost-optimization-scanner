"""RDS database optimization checks.

Extracted from CostOptimizer RDS-related methods as free functions.
This module will later become RdsModule (T-329) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.scan_context import ScanContext

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
    print("🔍 [services/rds.py] RDS module active")
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
    except Exception as e:
        print(f"Warning: Could not get RDS instance count: {e}")
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

    checks: dict[str, Any] = {
        "idle_databases": [],
        "instance_rightsizing": [],
        "reserved_instances": [],
        "storage_optimization": [],
        "multi_az_unnecessary": [],
        "backup_retention_excessive": [],
        "old_snapshots": [],
        "non_prod_scheduling": [],
        "aurora_serverless_candidates": [],
        "aurora_serverless_v2": [],
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
                storage_type = instance.get("StorageType", "gp2")

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

                    except Exception:
                        is_non_prod = any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                        env_name = next(
                            (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                            "non-prod",
                        )

                    if is_non_prod:
                        checks["multi_az_unnecessary"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "MultiAZ": multi_az,
                                "Environment": env_name,
                                "Recommendation": (f"Disable Multi-AZ for {env_name} environment to reduce costs"),
                                "EstimatedSavings": "~50% of instance cost",
                                "CheckCategory": "Multi-AZ Optimization",
                                "instanceFinding": (f"Multi-AZ enabled in {env_name} environment"),
                            }
                        )

                if backup_retention > 7:
                    extra_backup_days = backup_retention - 7
                    backup_price = (
                        ctx.pricing_engine.get_rds_backup_storage_price_per_gb()
                        if ctx.pricing_engine
                        else 0.095 * pricing_multiplier
                    )
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
                                f"${backup_savings:.2f}/month in backup storage (estimate - beyond free tier)"
                            ),
                            "CheckCategory": "Backup Retention Optimization",
                            "instanceFinding": (
                                f"{backup_retention} days retention (recommend 7 days for non-critical DBs)"
                            ),
                        }
                    )

                if storage_type == "gp2":
                    gp2_price = (
                        ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp2", multi_az=multi_az)
                        if ctx.pricing_engine
                        else 0.115 * pricing_multiplier
                    )
                    monthly_cost = allocated_storage * gp2_price
                    savings = monthly_cost * 0.20
                    checks["storage_optimization"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "engineVersion": instance.get("EngineVersion", ""),
                            "CurrentStorageType": storage_type,
                            "AllocatedStorage": allocated_storage,
                            "Recommendation": ("Migrate from gp2 to gp3 for 20% cost savings"),
                            "EstimatedSavings": f"${savings:.2f}/month",
                            "CheckCategory": "RDS Storage Optimization",
                            "storageFinding": (f"{storage_type} ({allocated_storage}GB) → gp3 recommended"),
                        }
                    )
                elif storage_type in ["io1", "io2", "gp3"]:
                    checks["storage_optimization"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "engineVersion": instance.get("EngineVersion", ""),
                            "CurrentStorageType": storage_type,
                            "AllocatedStorage": allocated_storage,
                            "Recommendation": (
                                f"Review {storage_type} IOPS/throughput configuration for potential optimization"
                            ),
                            "EstimatedSavings": ("Requires workload analysis for IOPS/throughput tuning"),
                            "CheckCategory": "RDS Storage Optimization",
                            "storageFinding": (
                                f"{storage_type} ({allocated_storage}GB) - review IOPS/throughput settings"
                            ),
                        }
                    )

                if any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"]):
                    env_name = next(
                        (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                        "non-prod",
                    )
                    if engine in ["mysql", "postgres", "mariadb"]:
                        checks["non_prod_scheduling"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "Environment": env_name,
                                "Recommendation": (
                                    f"Implement start/stop schedule for {env_name} database (stop nights/weekends)"
                                ),
                                "EstimatedSavings": "65-75% of compute costs",
                                "CheckCategory": "Non-Production Scheduling",
                                "instanceFinding": (f"{env_name} database - eligible for automated scheduling"),
                            }
                        )

                if db_instance_status == "stopped":
                    checks["idle_databases"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "DBInstanceStatus": db_instance_status,
                            "Recommendation": (
                                "Consider deleting stopped database if no longer needed (storage costs still apply)"
                            ),
                            "EstimatedSavings": ("Storage costs continue while stopped"),
                            "CheckCategory": "Idle Database Detection",
                            "instanceFinding": (f"Database in {db_instance_status} state"),
                        }
                    )

                if db_instance_class.startswith("db.t"):
                    checks["instance_rightsizing"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "DBInstanceClass": db_instance_class,
                            "Recommendation": (
                                "Review burstable instance usage - consider fixed instance if consistently high CPU"
                            ),
                            "EstimatedSavings": ("Requires CloudWatch analysis for accurate sizing"),
                            "CheckCategory": "Instance Rightsizing",
                            "instanceFinding": (f"Burstable instance ({db_instance_class}) - review usage patterns"),
                        }
                    )

                if db_instance_status == "available":
                    is_likely_prod = not any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                    ri_text = "production databases" if is_likely_prod else "long-running databases"
                    checks["reserved_instances"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "DBInstanceClass": db_instance_class,
                            "Recommendation": (f"Consider Reserved Instances for {ri_text}"),
                            "EstimatedSavings": ("Up to 60% savings for 1-3 year commitments"),
                            "CheckCategory": ("Reserved Instance Opportunities"),
                            "instanceFinding": (f"Instance ({db_instance_class}) - RI candidate"),
                        }
                    )

                if "aurora" in engine and db_instance_class in [
                    "db.t3.small",
                    "db.t3.medium",
                    "db.t4g.small",
                    "db.t4g.medium",
                ]:
                    checks["aurora_serverless_candidates"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "engineVersion": instance.get("EngineVersion", ""),
                            "DBInstanceClass": db_instance_class,
                            "Recommendation": ("Consider migrating to Aurora Serverless v2 for variable workloads"),
                            "EstimatedSavings": ("Pay only for capacity used (up to 90% for idle periods)"),
                            "CheckCategory": ("Aurora Serverless v2 Migration"),
                            "instanceFinding": (
                                f"Small Aurora instance ({db_instance_class}) - good candidate for Serverless v2"
                            ),
                        }
                    )

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
                                }
                            )
        except Exception as e:
            print(f"Warning: Could not check RDS snapshots: {e}")

        try:
            paginator = rds.get_paginator("describe_db_clusters")
            for page in paginator.paginate():
                for cluster in page.get("DBClusters", []):
                    cluster_id = cluster.get("DBClusterIdentifier")
                    engine = cluster.get("Engine", "")

                    if "aurora" in engine.lower():
                        if cluster.get("ServerlessV2ScalingConfiguration") is None:
                            checks["aurora_serverless_v2"].append(
                                {
                                    "DBClusterIdentifier": cluster_id,
                                    "resourceArn": (f"arn:aws:rds:{region}:{account_id}:cluster:{cluster_id}"),
                                    "Engine": engine,
                                    "Recommendation": ("Consider Aurora Serverless v2 for variable workloads"),
                                    "EstimatedSavings": ("20-90% cost reduction for variable workloads"),
                                    "CheckCategory": ("Aurora Serverless v2 Migration"),
                                }
                            )

                        storage_type = cluster.get("StorageType", "aurora")
                        if storage_type == "aurora":
                            checks["storage_optimization"].append(
                                {
                                    "DBClusterIdentifier": cluster_id,
                                    "resourceArn": (f"arn:aws:rds:{region}:{account_id}:cluster:{cluster_id}"),
                                    "Engine": engine,
                                    "StorageType": storage_type,
                                    "Recommendation": (
                                        "Review Aurora storage usage and"
                                        " consider Aurora I/O-Optimized"
                                        " if high I/O costs"
                                    ),
                                    "EstimatedSavings": ("Potential I/O cost reduction for high-throughput workloads"),
                                    "CheckCategory": ("Aurora Storage Optimization"),
                                    "instanceFinding": ("Aurora cluster - review I/O patterns"),
                                }
                            )

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
                                    }
                                )
            except Exception as e:
                print(f"Warning: Could not check Aurora cluster snapshots: {e}")
        except Exception as e:
            print(f"Warning: Could not check Aurora clusters: {e}")

    except Exception as e:
        print(f"Warning: Could not perform enhanced RDS checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
