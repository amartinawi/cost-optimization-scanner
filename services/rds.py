"""RDS database optimization checks.

Extracted from CostOptimizer RDS-related methods as free functions.
This module will later become RdsModule (T-329) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

# Evidence-gating thresholds for the metric-backed heuristic checks (N-M3).
# A heuristic that assumes a target with no usage evidence is a guess; both the
# Multi-AZ-disable and non-prod-scheduling checks now require a CloudWatch
# DatabaseConnections read over this window before emitting a saving.
RDS_METRIC_WINDOW_DAYS: int = 14
# Avg connections at/below which a non-prod DB is treated as idle enough to stop
# nights/weekends (scheduling). ~0 sustained connections => genuinely schedulable.
RDS_SCHEDULE_IDLE_MAX_AVG_CONN: float = 1.0
# Avg connections above which we DON'T suggest dropping Multi-AZ even on a
# non-prod-tagged DB — sustained load implies a workload worth keeping HA on
# (corroborating suppressor, mirrors the EC2 "signals only suppress" rule).
RDS_MULTI_AZ_BUSY_AVG_CONN: float = 20.0


def _rds_connection_signal(cloudwatch: Any, db_instance_id: str) -> dict[str, float] | None:
    """Return {avg_conn, max_conn} from AWS/RDS DatabaseConnections, or None on no data.

    Reads hourly DatabaseConnections (Average/Maximum) over the last
    ``RDS_METRIC_WINDOW_DAYS`` for the given DB instance. Returns None when the
    metric has no datapoints (so callers warn and skip rather than fabricate a
    saving). Exceptions propagate to the caller for permission classification.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=RDS_METRIC_WINDOW_DAYS)
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="DatabaseConnections",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
        StartTime=start,
        EndTime=end,
        Period=3600,
        Statistics=["Average", "Maximum"],
    )
    datapoints = resp.get("Datapoints", [])
    if not datapoints:
        return None
    avg_conn = sum(d["Average"] for d in datapoints) / len(datapoints)
    max_conn = max(d["Maximum"] for d in datapoints)
    return {"avg_conn": avg_conn, "max_conn": max_conn}

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


def _rds_monthly_price(
    ctx: ScanContext,
    engine: str,
    instance_class: str,
    *,
    multi_az: bool,
    license_model: str | None = None,
    aurora_io_optimized: bool = False,
) -> float:
    """Return RDS instance monthly $ price via PricingEngine, or 0.0 on failure.

    ``license_model`` is the instance's ``LicenseModel`` from describe-API (needed
    for Oracle/SQL Server LI-vs-BYOL/per-edition pricing). ``aurora_io_optimized``
    selects the Aurora I/O-Optimized SKU (≈30% dearer) vs Standard.
    """
    if not ctx.pricing_engine or not engine or not instance_class:
        return 0.0
    try:
        return ctx.pricing_engine.get_rds_instance_monthly_price(
            engine, instance_class, multi_az=multi_az, license_model=license_model,
            aurora_io_optimized=aurora_io_optimized,
        )
    except Exception as exc:
        logger.debug("RDS pricing lookup failed for %s %s: %s", engine, instance_class, exc)
        return 0.0


def _backup_rate(ctx: ScanContext, engine: str | None, pricing_multiplier: float) -> float:
    """Engine-aware RDS/Aurora backup-storage $/GB-month, with offline fallback.

    Aurora snapshots bill at ~$0.021/GB-mo vs standard RDS ~$0.095/GB-mo; pricing
    an Aurora snapshot at the standard rate overstates ~4.5x (audit C-A1).
    """
    if ctx.pricing_engine:
        return ctx.pricing_engine.get_rds_backup_storage_price_per_gb(engine)
    is_aurora = (engine or "").lower().startswith("aurora")
    return (0.021 if is_aurora else 0.095) * pricing_multiplier


def _snapshot_savings_text(size_gb: float, rate: float) -> tuple[str, str, float]:
    """Build (EstimatedSavings, AuditBasis formula, savings) for a snapshot finding.

    - Size unreported (0/None): AWS does not return the snapshot's size for some
      snapshots (notably Aurora cluster snapshots), so the saving cannot be
      quantified — emit an ADVISORY string (no ``$``) instead of a misleading
      ``$0.00`` finding (audit B1/B2). The snapshot still bills for backup storage.
    - Size present: the figure is an UPPER BOUND — it uses the provisioned/allocated
      size, but AWS bills snapshots on actual (compressed/incremental) bytes, which
      are typically lower (audit B3).
    """
    if not size_gb or size_gb <= 0:
        return (
            "advisory — snapshot size not reported by the API; delete to stop "
            "backup charges (quantify in console)",
            "size not reported by API; not quantifiable at scan time",
            0.0,
        )
    savings = size_gb * rate
    return (
        f"${savings:.2f}/month (upper bound — provisioned size; "
        "actual backup bytes are typically lower)",
        f"{size_gb}GB x ${rate:.4f}/GB-mo (provisioned-size upper bound)",
        savings,
    )


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
            # Check aurora FIRST: "aurora-mysql"/"aurora-postgresql" contain the
            # "mysql"/"postgres" substrings, so an aurora-first test avoids
            # miscounting Aurora instances as MySQL/PostgreSQL (audit L-A1).
            if "aurora" in engine:
                counts["aurora"] += 1
            elif "mysql" in engine:
                counts["mysql"] += 1
            elif "postgres" in engine:
                counts["postgres"] += 1
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
    fast_mode: bool = False,
) -> dict[str, Any]:
    """Get enhanced RDS cost optimization checks.

    The Multi-AZ-disable and non-prod-scheduling checks are gated on a CloudWatch
    DatabaseConnections read (N-M3): in ``fast_mode`` the metric reads are skipped
    (one warning) and those two checks are suppressed; with no metric data the
    instance is skipped with a warning rather than emitting a guessed saving.
    """
    rds = ctx.client("rds")
    region = ctx.region
    account_id = ctx.account_id

    cloudwatch = ctx.client("cloudwatch") if not fast_mode else None
    if fast_mode:
        ctx.warn(
            "[rds] fast mode: Multi-AZ and non-prod-scheduling checks need CloudWatch "
            "DatabaseConnections metrics and were skipped.",
            service="rds",
        )
    cw_denied = False  # latch so a permission gap is reported once, not per instance

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

    # Map cluster id -> StorageType so Aurora member-instance pricing can select
    # the right storage-mode SKU (aurora-iopt1 = I/O-Optimized, else Standard).
    clusters_by_id: dict[str, dict[str, Any]] = {}
    try:
        cl_paginator = rds.get_paginator("describe_db_clusters")
        for page in cl_paginator.paginate():
            for cluster in page.get("DBClusters", []):
                cid = cluster.get("DBClusterIdentifier")
                if cid:
                    clusters_by_id[cid] = cluster
    except Exception as e:
        ctx.warn(f"[rds] could not list DB clusters for storage-mode pricing: {e}", service="rds")

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
                license_model = instance.get("LicenseModel")

                if db_instance_status not in ["available", "stopped"]:
                    continue

                # CloudWatch DatabaseConnections evidence shared by the Multi-AZ
                # and scheduling checks. Computed once per instance; None means
                # fast-mode, a permission gap, an error, or no datapoints — in all
                # cases the metric-gated checks below are skipped (never guessed).
                conn_signal: dict[str, float] | None = None
                if cloudwatch is not None and db_instance_status == "available":
                    try:
                        conn_signal = _rds_connection_signal(cloudwatch, db_instance_id or "")
                        if conn_signal is None:
                            ctx.warn(
                                f"[rds] no DatabaseConnections data for {db_instance_id}; "
                                "skipping evidence-gated Multi-AZ/scheduling checks",
                                service="rds",
                            )
                    except ClientError as ce:
                        code = ce.response.get("Error", {}).get("Code", "")
                        if code in ("AccessDenied", "UnauthorizedOperation"):
                            if not cw_denied:
                                ctx.permission_issue(
                                    f"get_metric_statistics denied: {code}",
                                    service="rds",
                                    action="cloudwatch:GetMetricStatistics",
                                )
                                cw_denied = True
                        else:
                            ctx.warn(f"[rds] CloudWatch read failed for {db_instance_id}: {ce}", service="rds")
                    except Exception as e:
                        ctx.warn(f"[rds] CloudWatch read failed for {db_instance_id}: {e}", service="rds")

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

                    # Evidence gate (N-M3): require a connections signal and
                    # suppress on sustained load (a busy DB likely wants HA even if
                    # mis-tagged non-prod). No signal -> skip (already warned).
                    multi_az_evidenced = (
                        is_non_prod
                        and conn_signal is not None
                        and conn_signal["avg_conn"] <= RDS_MULTI_AZ_BUSY_AVG_CONN
                    )
                    if multi_az_evidenced and conn_signal is not None:
                        maz_avg_conn = conn_signal["avg_conn"]
                        multi_az_price = _rds_monthly_price(
                            ctx, engine or "", db_instance_class or "", multi_az=True,
                            license_model=license_model,
                        )
                        single_az_price = _rds_monthly_price(
                            ctx, engine or "", db_instance_class or "", multi_az=False,
                            license_model=license_model,
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
                                    "license_model": license_model,
                                    "metric_window": (
                                        f"non-prod ({env_name}); {RDS_METRIC_WINDOW_DAYS}d avg "
                                        f"DatabaseConnections {maz_avg_conn:.1f} "
                                        f"(<= {RDS_MULTI_AZ_BUSY_AVG_CONN:.0f})"
                                    ),
                                    "formula": (
                                        f"Multi-AZ ${multi_az_price:.2f} - Single-AZ ${single_az_price:.2f}"
                                    ),
                                },
                            }
                        )

                if backup_retention > 7:
                    backup_price = (
                        ctx.pricing_engine.get_rds_backup_storage_price_per_gb()
                        if ctx.pricing_engine
                        else 0.095 * pricing_multiplier
                    )
                    # N-M2: the billable backup amount cannot be derived at scan
                    # time. AWS gives free backup storage = 100% of total provisioned
                    # DB storage; only the excess (actual incremental snapshot bytes
                    # beyond that pool) is billed at the rate below. We have neither
                    # the actual backup bytes nor the account-wide provisioned pool
                    # here, so we surface the lever as ADVISORY (no fabricated $;
                    # excluded from the headline) rather than guess. The old
                    # allocated×days/30 model double-counted and ignored the free tier.
                    checks["backup_retention_excessive"].append(
                        {
                            "DBInstanceIdentifier": db_instance_id,
                            "resourceArn": (f"arn:aws:rds:{region}:{account_id}:db:{db_instance_id}"),
                            "engine": engine,
                            "engineVersion": instance.get("EngineVersion", ""),
                            "BackupRetentionPeriod": backup_retention,
                            "Recommendation": (
                                f"Reduce backup retention from {backup_retention} to 7 days for "
                                "non-critical DBs to shrink billable backup storage"
                            ),
                            "EstimatedSavings": (
                                "advisory — billable backup = snapshot bytes beyond the free "
                                f"allotment (100% of provisioned storage), billed at "
                                f"${backup_price:.4f}/GB-month; see Cost Explorer for the exact amount"
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
                                "metric_window": (
                                    "advisory — free allotment = 100% of provisioned storage; "
                                    "billable excess not derivable at scan time"
                                ),
                                "formula": "billable_backup_GB x rate (billable_backup_GB unknown at scan time)",
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
                    # N-M4: any non-Aurora RDS instance can be stopped/started on a
                    # schedule (Aurora stops at the cluster level, handled elsewhere).
                    # N-M3: require CloudWatch evidence the DB is genuinely idle on
                    # average before claiming a nights/weekends saving.
                    schedulable_engine = bool(engine) and not engine.startswith("aurora")
                    sched_evidenced = (
                        schedulable_engine
                        and conn_signal is not None
                        and conn_signal["avg_conn"] <= RDS_SCHEDULE_IDLE_MAX_AVG_CONN
                    )
                    if sched_evidenced and conn_signal is not None:
                        sched_avg_conn = conn_signal["avg_conn"]
                        sched_base_price = _rds_monthly_price(
                            ctx, engine or "", db_instance_class or "", multi_az=multi_az,
                            license_model=license_model,
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
                                    "license_model": license_model,
                                    "metric_window": (
                                        f"non-prod ({env_name}); {RDS_METRIC_WINDOW_DAYS}d avg "
                                        f"DatabaseConnections {sched_avg_conn:.1f} "
                                        f"(<= {RDS_SCHEDULE_IDLE_MAX_AVG_CONN:.0f}, idle)"
                                    ),
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
                    # Aurora members inherit their cluster's storage mode, which
                    # changes the instance rate (Standard vs I/O-Optimized).
                    cluster_of = instance.get("DBClusterIdentifier")
                    aurora_io = bool(cluster_of) and clusters_by_id.get(cluster_of, {}).get(
                        "StorageType"
                    ) == "aurora-iopt1"
                    ri_base_price = _rds_monthly_price(
                        ctx, engine or "", db_instance_class or "", multi_az=multi_az,
                        license_model=license_model, aurora_io_optimized=aurora_io,
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
                    snap_engine = snapshot.get("Engine")

                    if create_time:
                        age_days = (datetime.now(create_time.tzinfo) - create_time).days
                        if age_days > old_snapshot_days:
                            snap_rate = _backup_rate(ctx, snap_engine, pricing_multiplier)
                            snap_est, snap_formula, _snap_sv = _snapshot_savings_text(
                                snap_allocated_storage, snap_rate
                            )
                            checks["old_snapshots"].append(
                                {
                                    "SnapshotId": snapshot_id,
                                    "resourceArn": (f"arn:aws:rds:{region}:{account_id}:snapshot:{snapshot_id}"),
                                    "AgeDays": age_days,
                                    "AllocatedStorage": snap_allocated_storage,
                                    "engine": snap_engine,
                                    "Recommendation": (
                                        f"Delete {age_days}-day old manual"
                                        " snapshot (savings based on"
                                        " allocated storage estimate)"
                                    ),
                                    "EstimatedSavings": snap_est,
                                    "CheckCategory": "Old RDS Snapshots",
                                    "instanceFinding": (f"{age_days} days old ({snap_allocated_storage}GB)"),
                                    "AuditBasis": {
                                        "rate_basis": "RDS backup storage $/GB-month (live Pricing API)",
                                        "region": region,
                                        "engine": snap_engine,
                                        "rate": round(snap_rate, 4),
                                        "metric_window": (
                                            f"describe-API snapshot age {age_days}d > {old_snapshot_days}d threshold"
                                        ),
                                        "formula": snap_formula,
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
                        # Cluster snapshots are Aurora; price at the Aurora backup
                        # rate ($0.021/GB-mo), not the standard RDS rate (C-A1).
                        snap_engine = snapshot.get("Engine") or "aurora"

                        if create_time:
                            age_days = (datetime.now(create_time.tzinfo) - create_time).days
                            if age_days > old_snapshot_days:
                                snap_rate = _backup_rate(ctx, snap_engine, pricing_multiplier)
                                snap_est, snap_formula, _snap_sv = _snapshot_savings_text(
                                    cluster_allocated_storage, snap_rate
                                )
                                checks["old_snapshots"].append(
                                    {
                                        "SnapshotId": snapshot_id,
                                        "resourceArn": (
                                            f"arn:aws:rds:{region}:{account_id}:cluster-snapshot:{snapshot_id}"
                                        ),
                                        "AgeDays": age_days,
                                        "AllocatedStorage": (cluster_allocated_storage),
                                        "engine": snap_engine,
                                        "Recommendation": (
                                            f"Delete {age_days}-day old"
                                            " Aurora cluster snapshot"
                                            " (savings based on allocated"
                                            " storage estimate)"
                                        ),
                                        "EstimatedSavings": snap_est,
                                        "CheckCategory": ("Old Aurora Cluster Snapshots"),
                                        "instanceFinding": (
                                            f"{age_days} days old Aurora"
                                            " cluster snapshot"
                                            f" ({cluster_allocated_storage}GB)"
                                        ),
                                        "AuditBasis": {
                                            "rate_basis": "Aurora backup storage $/GB-month (live Pricing API)",
                                            "region": region,
                                            "engine": snap_engine,
                                            "rate": round(snap_rate, 4),
                                            "metric_window": (
                                                f"describe-API snapshot age {age_days}d > "
                                                f"{old_snapshot_days}d threshold"
                                            ),
                                            "formula": snap_formula,
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
