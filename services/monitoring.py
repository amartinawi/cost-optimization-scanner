"""CloudWatch and CloudTrail cost optimization checks.

Extracted from CostOptimizer.get_cloudwatch_checks() and
CostOptimizer.get_cloudtrail_checks() as free functions.
This module will later become MonitoringModule (T-322) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

# Tiered custom-metric rates re-verified against the AWS Pricing API on
# 2026-06-27 (AmazonCloudWatch SKU KG586CTNGQ4VRZKZ, usagetype
# CW:MetricMonitorUsage): $0.30 first 10k / $0.10 to 250k / $0.05 to 1M /
# $0.02 above 1M. The 4th tier was previously observed but never coded, so
# tier_3 covered everything above 250k at $0.05 — overstating the marginal
# rate (and the saving) for any account with >1M custom metrics (monitoring L2).

# A custom metric that published NO datapoints over this trailing window is
# treated as stale (removable). Drives the H3 removable quantity from a
# measured staleness signal instead of a fabricated count//2.
CUSTOM_METRIC_STALE_LOOKBACK_DAYS: int = 30

# GetMetricData accepts at most 500 MetricDataQueries per call.
_GET_METRIC_DATA_BATCH: int = 500


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


def _stale_custom_metric_counts(
    cloudwatch: Any,
    metrics: list[dict[str, Any]],
    lookback_days: int,
) -> dict[str, int]:
    """Return per-namespace count of STALE custom metrics.

    A metric is stale when GetMetricData returns no datapoints (empty
    ``Values``) for it over the trailing ``lookback_days`` window — i.e. it
    published nothing and is genuinely removable. Each unique
    ``(Namespace, MetricName, Dimensions)`` is one billable metric stream.
    Queries are batched 500-per-call (the GetMetricData limit) and
    ``NextToken``-paginated.

    Raises on API error so the caller can classify the failure and fall back
    to a $0 advisory rather than fabricate a removable quantity (H3).
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    period = max(60, lookback_days * 86400)  # one bucket spanning the window
    stale_by_ns: dict[str, int] = {}

    for offset in range(0, len(metrics), _GET_METRIC_DATA_BATCH):
        batch = metrics[offset : offset + _GET_METRIC_DATA_BATCH]
        id_to_metric: dict[str, dict[str, Any]] = {}
        queries: list[dict[str, Any]] = []
        for i, metric in enumerate(batch):
            qid = f"m{i}"
            id_to_metric[qid] = metric
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": metric.get("Namespace", ""),
                            "MetricName": metric.get("MetricName", ""),
                            "Dimensions": metric.get("Dimensions", []),
                        },
                        "Period": period,
                        "Stat": "SampleCount",
                    },
                    "ReturnData": True,
                }
            )

        active_ids: set[str] = set()
        next_token = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": start,
                "EndTime": end,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            response = cloudwatch.get_metric_data(**kwargs)
            for result in response.get("MetricDataResults", []):
                if result.get("Values"):
                    active_ids.add(result.get("Id"))
            next_token = response.get("NextToken")
            if not next_token:
                break

        for qid, metric in id_to_metric.items():
            if qid not in active_ids:
                namespace = metric.get("Namespace", "")
                stale_by_ns[namespace] = stale_by_ns.get(namespace, 0) + 1

    return stale_by_ns


MONITORING_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "never_expiring_logs": {
        "title": "Set Log Retention Policies",
        "description": "Log groups without retention policies grow indefinitely, increasing storage costs.",
        "action": "Set retention policy (e.g., 30 days) on all log groups",
    },
    "excessive_logging": {
        "title": "Reduce Excessive Log Storage",
        "description": "Large log groups may indicate excessive logging or missing log level controls.",
        "action": "Review log levels and reduce retention for non-essential logs",
    },
    "unused_custom_metrics": {
        "title": "Optimize Custom Metrics",
        "description": "High volumes of custom metrics incur significant monthly charges.",
        "action": "Review and reduce unnecessary custom metrics",
    },
    "unused_alarms": {
        "title": "Clean Up Stale Alarms",
        "description": "Alarms with persistent insufficient data may be monitoring unavailable metrics.",
        "action": "Delete or reconfigure alarms with prolonged insufficient data",
    },
    "multi_region_trails": {
        "title": "Optimize Multi-Region Trails",
        "description": "Multi-region CloudTrail trails replicate events to all regions, increasing costs.",
        "action": "Use single-region trails where multi-region is not required",
    },
    "data_events_all_s3": {
        "title": "Scope S3 Data Events",
        "description": "Data events for all S3 objects generate very high volume and cost.",
        "action": "Limit data events to specific buckets",
    },
    "data_events_all_lambda": {
        "title": "Scope Lambda Data Events",
        "description": "Data events for all Lambda functions increase CloudTrail costs.",
        "action": "Limit data events to specific functions",
    },
}


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

        for log_group in log_groups:
            log_group_name = log_group.get("logGroupName")
            retention_days = log_group.get("retentionInDays")
            stored_bytes = log_group.get("storedBytes", 0)

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

            high_volume = {ns for ns, c in namespace_counts.items() if c > 100}

            # H3 — the removable quantity must come from a STALENESS signal, not
            # a fabricated count//2. A custom metric that published no datapoints
            # over the lookback window is genuinely removable; one still emitting
            # data is not. Probe only high-volume namespaces (bounded) and gate
            # on fast mode. No evidence (fast mode / GetMetricData failure) → $0
            # advisory, never a fabricated count.
            stale_by_ns = None
            if high_volume and not fast_mode:
                probe = [m for m in custom_metrics if m.get("Namespace", "") in high_volume]
                try:
                    stale_by_ns = _stale_custom_metric_counts(
                        cloudwatch, probe, CUSTOM_METRIC_STALE_LOOKBACK_DAYS
                    )
                except Exception as e:
                    # A GetMetricData permission/throttle gap must surface AND
                    # leave the saving unfabricated (fall through to advisory).
                    record_aws_error(
                        ctx, e, service="monitoring", context="cloudwatch:GetMetricData failed"
                    )
                    stale_by_ns = None

            # AWS custom-metric tiering is account-wide per region ($0.30 first 10k,
            # then $0.10, then $0.05), so the removable (stale) metrics come off the
            # TOP of the account's metric stack. Anchor the marginal-rate computation
            # on the total custom-metric count and walk it down as each namespace's
            # stale metrics are attributed — pricing per-namespace from 0 overstated
            # the saving once the account exceeds the 10k tier-1 limit.
            running_account_count = len(custom_metrics)

            for namespace in sorted(high_volume):
                count = namespace_counts[namespace]

                if stale_by_ns is None:
                    # No staleness evidence → advisory $0 (never count//2).
                    checks["unused_custom_metrics"].append(
                        {
                            "Namespace": namespace,
                            "MetricCount": count,
                            "StaleMetricCount": None,
                            "Recommendation": (
                                f"High number of custom metrics ({count}) - review necessity"
                            ),
                            "EstimatedSavings": (
                                "$0.00/month — advisory: removable metrics require per-metric "
                                f"staleness (no datapoints over {CUSTOM_METRIC_STALE_LOOKBACK_DAYS}d); "
                                "run without --fast / grant cloudwatch:GetMetricData"
                            ),
                            "EstimatedMonthlySavings": 0.0,
                            "Counted": False,
                            "CheckCategory": "Excessive Custom Metrics",
                            "AuditBasis": {
                                "metric_count": count,
                                "reason": "no GetMetricData staleness evidence (fast mode or read failure)",
                                "formula": "advisory $0",
                            },
                        }
                    )
                    continue

                stale = stale_by_ns.get(namespace, 0)
                if stale <= 0:
                    # Measured: every custom metric still publishing → nothing removable.
                    checks["unused_custom_metrics"].append(
                        {
                            "Namespace": namespace,
                            "MetricCount": count,
                            "StaleMetricCount": 0,
                            "Recommendation": (
                                f"All {count} custom metrics published data in the last "
                                f"{CUSTOM_METRIC_STALE_LOOKBACK_DAYS}d - none stale"
                            ),
                            "EstimatedSavings": "$0.00/month — advisory: no stale metrics measured",
                            "EstimatedMonthlySavings": 0.0,
                            "Counted": False,
                            "CheckCategory": "Excessive Custom Metrics",
                            "AuditBasis": {
                                "metric_count": count,
                                "stale_metric_count": 0,
                                "metric_window": (
                                    f"{CUSTOM_METRIC_STALE_LOOKBACK_DAYS}d GetMetricData SampleCount"
                                ),
                                "formula": "advisory $0 (no stale metrics)",
                            },
                        }
                    )
                    continue

                # Counted: removable = measured stale metrics, priced at the
                # account-wide MARGINAL tier (top of the stack), region-scaled.
                current_cost = _cw_custom_metrics_monthly_cost(running_account_count)
                reduced_cost = _cw_custom_metrics_monthly_cost(running_account_count - stale)
                monthly_savings = (current_cost - reduced_cost) * pricing_multiplier
                running_account_count -= stale
                checks["unused_custom_metrics"].append(
                    {
                        "Namespace": namespace,
                        "MetricCount": count,
                        "StaleMetricCount": stale,
                        "Recommendation": (
                            f"{stale} of {count} custom metrics published no data in "
                            f"{CUSTOM_METRIC_STALE_LOOKBACK_DAYS}d - delete to stop per-metric charges"
                        ),
                        "EstimatedSavings": f"${monthly_savings:.2f}/month if {stale} stale metrics removed",
                        "EstimatedMonthlySavings": round(monthly_savings, 2),
                        "CheckCategory": "Excessive Custom Metrics",
                        "AuditBasis": {
                            "metric_count": count,
                            "stale_metric_count": stale,
                            "tier_rates_per_metric_month": [
                                CW_CUSTOM_METRIC_TIER_1,
                                CW_CUSTOM_METRIC_TIER_2,
                                CW_CUSTOM_METRIC_TIER_3,
                            ],
                            "metric_window": (
                                f"{CUSTOM_METRIC_STALE_LOOKBACK_DAYS}d GetMetricData SampleCount "
                                "(no datapoints = stale)"
                            ),
                            "region_multiplier": round(pricing_multiplier, 4),
                            "account_marginal_anchor": running_account_count + stale,
                            "formula": (
                                "(tiered_cost(account_anchor) - tiered_cost(account_anchor - stale)) "
                                "x region_multiplier  [account-wide marginal tier]"
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
