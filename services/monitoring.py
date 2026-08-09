"""CloudWatch and CloudTrail cost optimization checks.

Extracted from CostOptimizer.get_cloudwatch_checks() and
CostOptimizer.get_cloudtrail_checks() as free functions.
This module will later become MonitoringModule (T-322) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# CloudWatch Logs storage list price (us-east-1): $0.03/GB-month
# Source: AWS Pricing API SKU JRHJQ2UMPUB5K73A (verified 2026-05).
# Region-scaled via `pricing_multiplier` at the per-rec emit site.
CW_LOGS_GB_MONTH: float = 0.03

# CloudWatch custom metrics tiered pricing (us-east-1):
#   First 10,000 metrics:  $0.30/metric/month
#   Next 240,000:          $0.10/metric/month
#   Above 250,000:         $0.05/metric/month
# Source: https://aws.amazon.com/cloudwatch/pricing/ (verified 2026-05).
CW_CUSTOM_METRIC_TIER_1: float = 0.30
CW_CUSTOM_METRIC_TIER_2: float = 0.10
CW_CUSTOM_METRIC_TIER_3: float = 0.05
CW_CUSTOM_METRIC_TIER_4: float = 0.02
CW_CUSTOM_METRIC_TIER_1_LIMIT: int = 10_000
CW_CUSTOM_METRIC_TIER_2_LIMIT: int = 250_000
CW_CUSTOM_METRIC_TIER_3_LIMIT: int = 1_000_000
# Spend floor for the custom-metric advisory: surface a namespace only when
# its proportional share of the account's custom-metric bill is worth a
# review (the old >100-metric CARDINALITY gate hid 99-metric namespaces
# worth ~$30/mo while surfacing $2/mo ones on huge accounts).
CW_CUSTOM_METRIC_SPEND_ADVISORY_FLOOR: float = 10.0

# MON-3 — CloudWatch Logs ingestion, $/GB by log class. Validated against the
# live Pricing API 2026-08-09 (AmazonCloudWatch, us-east-1, operation
# PutLogEvents): Standard custom-log ingestion USE1-DataProcessing-Bytes =
# $0.50/GB flat; Infrequent Access USE1-DataProcessingIA-Bytes = $0.25/GB flat.
# Region-scaled by pricing_multiplier at the call site.
CW_LOGS_INGEST_STANDARD_GB: float = 0.50
CW_LOGS_INGEST_IA_GB: float = 0.25

# One GetMetricData call carries at most 500 queries. Log-group counts can run
# to thousands, so the probe covers the largest groups first and says so when it
# truncates rather than silently reporting on a subset.
_INGESTION_PROBE_LIMIT: int = 500
_INGESTION_WINDOW_DAYS: int = 30


def _log_group_ingestion_gb(
    ctx: ScanContext, log_group_names: list[str]
) -> dict[str, float]:
    """{log group name: GB ingested over the window}, batched into one call.

    Only groups CloudWatch actually reported are present; a missing name means
    no data was returned, which the caller treats as "unknown", never as zero.
    """
    if not log_group_names:
        return {}
    from datetime import datetime, timedelta

    queries = [
        {
            "Id": f"q{i}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Logs",
                    "MetricName": "IncomingBytes",
                    "Dimensions": [{"Name": "LogGroupName", "Value": name}],
                },
                "Period": _INGESTION_WINDOW_DAYS * 86400,
                "Stat": "Sum",
            },
            "ReturnData": True,
        }
        for i, name in enumerate(log_group_names)
    ]
    end = datetime.now(UTC)
    start = end - timedelta(days=_INGESTION_WINDOW_DAYS)
    try:
        resp = ctx.client("cloudwatch").get_metric_data(
            MetricDataQueries=queries, StartTime=start, EndTime=end
        )
    except Exception as exc:
        record_aws_error(
            ctx,
            exc,
            service="monitoring",
            context="cloudwatch:GetMetricData IncomingBytes for log groups",
        )
        return {}

    out: dict[str, float] = {}
    for result in resp.get("MetricDataResults", []):
        values = result.get("Values") or []
        if not values:
            continue
        try:
            index = int(str(result.get("Id", "q"))[1:])
        except ValueError:
            continue
        if 0 <= index < len(log_group_names):
            out[log_group_names[index]] = sum(values) / (1024**3)
    return out

# Tiered custom-metric rates re-verified against the AWS Pricing API on
# 2026-06-27 (AmazonCloudWatch SKU KG586CTNGQ4VRZKZ, usagetype
# CW:MetricMonitorUsage): $0.30 first 10k / $0.10 to 250k / $0.05 to 1M /
# $0.02 above 1M. The 4th tier was previously observed but never coded, so
# tier_3 covered everything above 250k at $0.05 — overstating the marginal
# rate (and the saving) for any account with >1M custom metrics (monitoring L2).

def _cw_custom_metrics_monthly_cost(count: int) -> float:
    """Return CloudWatch custom metrics monthly cost for `count` metrics
    applying AWS-published tiered pricing breakpoints. Region-scaled by
    the caller via `pricing_multiplier`.
    """
    if count <= 0:
        return 0.0
    tier_1 = min(count, CW_CUSTOM_METRIC_TIER_1_LIMIT) * CW_CUSTOM_METRIC_TIER_1
    tier_2 = max(
        0,
        min(count, CW_CUSTOM_METRIC_TIER_2_LIMIT) - CW_CUSTOM_METRIC_TIER_1_LIMIT,
    ) * CW_CUSTOM_METRIC_TIER_2
    tier_3 = max(
        0,
        min(count, CW_CUSTOM_METRIC_TIER_3_LIMIT) - CW_CUSTOM_METRIC_TIER_2_LIMIT,
    ) * CW_CUSTOM_METRIC_TIER_3
    tier_4 = max(0, count - CW_CUSTOM_METRIC_TIER_3_LIMIT) * CW_CUSTOM_METRIC_TIER_4
    return tier_1 + tier_2 + tier_3 + tier_4



def get_cloudwatch_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Category 9: CloudWatch optimization checks.

    Args:
        ctx: Scan context with cloudwatch + logs clients.
        pricing_multiplier: Regional pricing multiplier applied to per-rec
            $ values. us-east-1 ≈ 1.0; eu-west-1 ≈ 1.08; etc.
    """
    # The expensive part of this scan is the CloudWatch describes (list_metrics +
    # describe_alarms) and the GetMetricData staleness probe; under --fast skip
    # them so the adapter's reads_fast_mode declaration holds.
    fast_mode = bool(getattr(ctx, "fast_mode", False))

    checks: dict[str, list[dict[str, Any]]] = {
        "never_expiring_logs": [],
        "log_class_migration": [],
        "excessive_logging": [],
        "unused_custom_metrics": [],
        "high_resolution_metrics": [],
        "unused_alarms": [],
        "duplicate_metrics": [],
    }

    try:
        logs = ctx.client("logs")
        log_groups: list[dict[str, Any]] = []
        log_groups_params: dict[str, Any] = {}
        while True:
            log_groups_response = logs.describe_log_groups(**log_groups_params)
            log_groups.extend(log_groups_response.get("logGroups", []))
            next_token = log_groups_response.get("nextToken")
            if not next_token:
                break
            log_groups_params["nextToken"] = next_token

        # MON-3 — ingestion is the dominant CloudWatch Logs cost and had no
        # lever at all. Probe the biggest groups first; one batched call.
        standard_groups = [
            str(lg.get("logGroupName"))
            for lg in sorted(log_groups, key=lambda g: g.get("storedBytes", 0) or 0, reverse=True)
            if lg.get("logGroupName")
            and str(lg.get("logGroupClass") or "STANDARD").upper() == "STANDARD"
        ]
        probed = standard_groups[:_INGESTION_PROBE_LIMIT]
        if len(standard_groups) > len(probed):
            ctx.warn(
                f"Log ingestion probed for the {len(probed)} largest Standard-class log "
                f"groups of {len(standard_groups)}; the remainder were not measured.",
                "monitoring",
            )
        ingestion_gb = {} if fast_mode else _log_group_ingestion_gb(ctx, probed)

        for log_group in log_groups:
            log_group_name = log_group.get("logGroupName")
            retention_days = log_group.get("retentionInDays")
            stored_bytes = log_group.get("storedBytes", 0)

            monthly_gb = ingestion_gb.get(str(log_group_name))
            if monthly_gb is not None and monthly_gb > 0:
                delta = (CW_LOGS_INGEST_STANDARD_GB - CW_LOGS_INGEST_IA_GB) * pricing_multiplier
                potential = monthly_gb * delta
                checks["log_class_migration"].append(
                    {
                        "LogGroupName": log_group_name,
                        "LogGroupClass": log_group.get("logGroupClass") or "STANDARD",
                        "MonthlyIngestedGB": round(monthly_gb, 2),
                        "Recommendation": (
                            f"{monthly_gb:,.1f} GB/month ingested into the Standard log "
                            "class - the Infrequent Access class ingests the same data at "
                            "half the rate"
                        ),
                        # ADVISORY, deliberately. Unlike this tranche's other new
                        # levers, the change is not a config toggle: AWS documents
                        # that logGroupClass "can't be changed after a log group is
                        # created", so realizing this means creating a new group and
                        # repointing every producer. IA also drops EMF, Live Tail,
                        # anomaly detection, pattern analysis and console viewing,
                        # and nothing here can prove none of those are in use. Same
                        # call as the FSx SSD->HDD demotion: an exact figure, but not
                        # an in-place change, so the figure renders without being
                        # counted.
                        "EstimatedSavings": (
                            f"$0.00/month - advisory: ${potential:,.2f}/month at the IA "
                            "rate, but the log class cannot be changed in place and IA "
                            "drops EMF / Live Tail / anomaly detection"
                        ),
                        "EstimatedMonthlySavings": 0.0,
                        "PotentialMonthlySavings": round(potential, 2),
                        "Counted": False,
                        "CheckCategory": "CloudWatch Logs Class Migration",
                        "AuditBasis": {
                            "metric": "AWS/Logs IncomingBytes (Sum), dimension LogGroupName",
                            "metric_window_days": _INGESTION_WINDOW_DAYS,
                            "monthly_ingested_gb": round(monthly_gb, 2),
                            "standard_rate_per_gb": CW_LOGS_INGEST_STANDARD_GB,
                            "ia_rate_per_gb": CW_LOGS_INGEST_IA_GB,
                            "region_multiplier": round(pricing_multiplier, 4),
                            "rate_source": (
                                "AmazonCloudWatch USE1-DataProcessing-Bytes / "
                                "USE1-DataProcessingIA-Bytes, operation PutLogEvents "
                                "(AWS Pricing API, validated 2026-08-09)"
                            ),
                            "formula": "ingested GB x (standard rate - IA rate)",
                            "counted": False,
                            "reason": (
                                "logGroupClass cannot be changed after creation (AWS docs), "
                                "so this needs a new log group and every producer "
                                "repointed; IA also drops features whose use this scan "
                                "cannot rule out"
                            ),
                        },
                    }
                )

            if retention_days is None:
                stored_gb = stored_bytes / (1024**3)
                # H2 — setting a retention policy only deletes log data OLDER
                # than the chosen window. describe_log_groups exposes only
                # storedBytes (no age distribution), so the deletable fraction
                # cannot be measured here; charging 100% of storedBytes
                # fabricates a saving that scales with the largest groups.
                # Emit a $0 advisory (S3-style: no evidence → not counted)
                # rather than an unbacked counted dollar. Quantify via
                # CloudWatch Logs Insights ("bytes older than N days") before
                # counting.
                checks["never_expiring_logs"].append(
                    {
                        "LogGroupName": log_group_name,
                        "StoredBytes": stored_bytes,
                        "StoredGB": round(stored_gb, 2),
                        "Recommendation": "Set retention policy to prevent unlimited log growth",
                        "EstimatedSavings": (
                            "$0.00/month — advisory: deletable bytes (older than "
                            "retention) not measurable from describe_log_groups; "
                            "quantify via CloudWatch Logs Insights before counting"
                        ),
                        "EstimatedMonthlySavings": 0.0,
                        "Counted": False,
                        "CheckCategory": "Never-Expiring Log Groups",
                        "AuditBasis": {
                            "stored_gb": round(stored_gb, 2),
                            "rate_per_gb_month": CW_LOGS_GB_MONTH,
                            "region_multiplier": round(pricing_multiplier, 4),
                            "reason": (
                                "no measured bytes-older-than-retention signal; "
                                "100%-of-storedBytes saving is unbacked"
                            ),
                            "formula": "advisory $0 (requires age-of-bytes evidence)",
                        },
                    }
                )

            # Excessive log storage finding removed: emitted no concrete $ — "review log
            # level and retention" without quantifying storage savings.

        try:
            if not fast_mode:
                cloudwatch = ctx.client("cloudwatch")
                paginator = cloudwatch.get_paginator("describe_alarms")
                for page in paginator.paginate():
                    alarms = page.get("MetricAlarms", [])

                    for alarm in alarms:
                        alarm_name = alarm.get("AlarmName")
                        state_reason = alarm.get("StateReason", "")
                        alarm_config_updated = alarm.get("AlarmConfigurationUpdatedTimestamp")

                        # Unused CloudWatch Alarms finding removed: health/operational
                        # signal with no EstimatedSavings field — not a cost rec.
                        _ = (state_reason, alarm_config_updated, alarm_name)

        except Exception as e:
            # H1 — classify (don't logger-only): a permission gap on DescribeAlarms
            # must surface, not read as a clean empty result.
            record_aws_error(ctx, e, service="monitoring", context="cloudwatch:DescribeAlarms failed")

        try:
            cloudwatch = ctx.client("cloudwatch")
            metrics: list[dict[str, Any]] = []
            if not fast_mode:
                paginator = cloudwatch.get_paginator("list_metrics")
                for page in paginator.paginate():
                    metrics.extend(page.get("Metrics", []))

            # Only custom (non-AWS/) metrics incur the per-metric monthly charge.
            custom_metrics = [
                m for m in metrics if not (m.get("Namespace", "") or "").startswith("AWS/")
            ]
            namespace_counts: dict[str, int] = {}
            for metric in custom_metrics:
                namespace = metric.get("Namespace", "")
                namespace_counts[namespace] = namespace_counts.get(namespace, 0) + 1

            # MON-1/MON-2 — the previous lever counted "stale" custom metrics
            # as a removable dollar. That was inverted twice over: CloudWatch
            # bills per PutMetricData-ACTIVE metric-month, so a metric that
            # stopped publishing is ALREADY FREE (and there is no DeleteMetric
            # API — metrics age out on their own); and ListMetrics only returns
            # metrics with datapoints in ~2 weeks, so every metric enumerated
            # here is inside any 30d window — the stale set was empty on
            # healthy accounts and non-empty exactly when the GetMetricData
            # probe broke (its StatusCode was never checked): dead code when
            # healthy, phantom when it fired. Replaced with a MEASURED-SPEND
            # advisory ($0 counted — the only way to save is to stop
            # publishing, which this scan cannot verify anyone wants). Each
            # namespace's figure is its PROPORTIONAL share of the account's
            # tiered custom-metric bill (order-independent — a marginal
            # off-the-top walk priced identical namespaces differently by
            # sort position), gated on a spend floor rather than the old
            # cardinality threshold.
            total_custom = len(custom_metrics)
            account_monthly_cost = _cw_custom_metrics_monthly_cost(total_custom) * pricing_multiplier
            for namespace, count in sorted(namespace_counts.items()):
                spend = account_monthly_cost * (count / total_custom) if total_custom else 0.0
                if spend < CW_CUSTOM_METRIC_SPEND_ADVISORY_FLOOR:
                    continue
                checks["unused_custom_metrics"].append(
                    {
                        "Namespace": namespace,
                        "MetricCount": count,
                        "Recommendation": (
                            f"{count} active custom metrics bill ~${spend:.2f}/mo — review "
                            "whether each publisher is still needed (stopping PutMetricData "
                            "stops the charge; idle metrics are already free)"
                        ),
                        "EstimatedSavings": (
                            f"$0.00/month — advisory: ~${spend:.2f}/mo billed for {count} "
                            "active custom metrics in this namespace; a saving requires "
                            "stopping unneeded publishers, which this scan cannot verify"
                        ),
                        "EstimatedMonthlySavings": 0.0,
                        "Counted": False,
                        "CheckCategory": "Excessive Custom Metrics",
                        "AuditBasis": {
                            "metric_count": count,
                            "billed_monthly_estimate": round(spend, 2),
                            "tier_rates_per_metric_month": [
                                CW_CUSTOM_METRIC_TIER_1,
                                CW_CUSTOM_METRIC_TIER_2,
                                CW_CUSTOM_METRIC_TIER_3,
                                CW_CUSTOM_METRIC_TIER_4,
                            ],
                            "region_multiplier": round(pricing_multiplier, 4),
                            "account_total_custom_metrics": total_custom,
                            "account_monthly_cost": round(account_monthly_cost, 2),
                            "formula": (
                                "account_monthly_cost x (namespace_count / total_count) — "
                                "proportional share of MEASURED SPEND, not a saving"
                            ),
                            "reason": (
                                "idle custom metrics are not billed and cannot be deleted; "
                                "only active publishers cost money (MON-1)"
                            ),
                        },
                    }
                )

        except Exception as e:
            # H1 — a ListMetrics permission gap silently drops the counted
            # custom-metrics check → empty tab reads as "$0 savings".
            record_aws_error(ctx, e, service="monitoring", context="cloudwatch:ListMetrics failed")

    except Exception as e:
        # H1 — outer guard (e.g. logs:DescribeLogGroups AccessDenied): classify so
        # the CloudWatch tab does not empty silently.
        record_aws_error(ctx, e, service="monitoring", context="CloudWatch checks failed")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}


def get_cloudtrail_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 10: CloudTrail optimization checks.

    Every concrete CloudTrail finding (multi-region trails, S3/Lambda data
    events, duplicate/expensive trails, unused Insights) was removed earlier
    because each emitted ``$0`` with only a generic percentage-range estimate
    rather than an account-specific dollar. The dead-cost walk that fed them
    (un-paginated ``describe_trails`` + per-trail ``get_event_selectors``) is
    removed with them: it spent API quota to produce zero recommendations while
    swallowing every error through ``logger.warning``. The empty checks
    structure is retained so the report keeps a stable source-block shape.

    Args:
        ctx: Scan context (unused; retained for signature parity with the other
            monitoring sub-shims).
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "multi_region_trails": [],
        "data_events_all_s3": [],
        "data_events_all_lambda": [],
        "duplicate_trails": [],
        "expensive_storage_trails": [],
        "unused_insights": [],
    }

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}
