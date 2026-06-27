"""App Runner cost optimization checks.

Extracted from CostOptimizer.get_enhanced_apprunner_checks() as a free function.
This module will later become AppRunnerModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

APP_RUNNER_MEM_GB_HOURLY: float = 0.007
HOURS_PER_MONTH: int = 730
IDLE_REQUEST_LOOKBACK_DAYS: int = 30
SECONDS_PER_DAY: int = 86400

APPRUNNER_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "auto_scaling_optimization": {
        "title": "Pause Idle App Runner Services",
        "description": (
            "RUNNING services with zero requests accrue the 24/7 provisioned "
            "memory charge; pause or delete to recover it."
        ),
        "action": "Pause or delete the idle service",
    }
}


def _monthly_requests(cw: Any, service_name: str, service_id: str, ctx: Any) -> float | None:
    """Total App Runner ``Requests`` over the lookback window, or None on failure.

    Queries the ``AWS/AppRunner`` ``Requests`` metric with its real published
    dimensions (``ServiceName`` + ``ServiceID``). The previous code queried an
    invalid ``Service`` dimension name, so CloudWatch returned no datapoints for
    every service — the read was a guaranteed no-op and the tab was permanently
    empty (apprunner C1/H1). Both dimensions are required because App Runner
    publishes the metric keyed on the (ServiceName, ServiceID) pair; omitting
    ``ServiceID`` would match a non-existent time series.

    Returns ``None`` (not ``0``) when the read fails OR returns no datapoints, so
    the caller abstains rather than false-positives an idle/delete flag. A read
    failure is classified on ``ctx`` (permission gap vs transient) before
    returning — never silently swallowed (apprunner H2).
    """
    now = datetime.now(UTC)
    start = now - timedelta(days=IDLE_REQUEST_LOOKBACK_DAYS)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/AppRunner",
            MetricName="Requests",
            Dimensions=[
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "ServiceID", "Value": service_id},
            ],
            StartTime=start,
            EndTime=now,
            Period=IDLE_REQUEST_LOOKBACK_DAYS * SECONDS_PER_DAY,
            Statistics=["Sum"],
        )
    except Exception as e:
        record_aws_error(
            ctx,
            e,
            service="apprunner",
            context=f"cloudwatch GetMetricStatistics(Requests) failed for '{service_name}'",
        )
        return None
    dps = resp.get("Datapoints", [])
    if dps:
        return sum(d["Sum"] for d in dps)
    return None


def get_enhanced_apprunner_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced App Runner cost optimization checks.

    Emits an idle-service rec for each RUNNING service with zero ``Requests``
    over the lookback window (CloudWatch-gated on the real ``ServiceName`` +
    ``ServiceID`` metric dimensions — see ``_monthly_requests``). The 24/7
    provisioned memory charge (``Memory_GB × $0.007/hr × 730``) is the
    recoverable waste when the service is paused/deleted. ``fast_mode`` skips the
    CW read and emits nothing — without the request signal an idle flag would be
    a guess.

    Fail-safe ordering: the idle (0-request) signal is confirmed first; only then
    is ``describe_service`` called to read the memory that prices the saving. If
    that read fails the service is skipped (no counted delete on a guessed
    memory) and the failure is classified, never swallowed (apprunner H2).
    """
    checks: dict[str, list[dict[str, Any]]] = {"unused_services": []}

    if getattr(ctx, "fast_mode", False):
        return {"recommendations": [], "checks": checks}

    try:
        response = ctx.client("apprunner").list_services()
        services = response.get("ServiceSummaryList", [])
        cw = ctx.client("cloudwatch")

        for service in services:
            service_name = service.get("ServiceName")
            service_id = service.get("ServiceId")
            status = service.get("Status")
            if not service_name or not service_id or status != "RUNNING":
                continue

            monthly_requests = _monthly_requests(cw, service_name, service_id, ctx)
            if monthly_requests is None:
                # CW read failed/no data — abstain rather than false-positive.
                continue
            if monthly_requests > 0:
                continue

            # Idle confirmed. Read the instance config that prices the saving;
            # without it (failed/denied read) abstain — this is a delete rec.
            try:
                details = ctx.client("apprunner").describe_service(ServiceArn=service.get("ServiceArn"))
                instance_config = details.get("Service", {}).get("InstanceConfiguration", {})
            except Exception as e:
                record_aws_error(
                    ctx,
                    e,
                    service="apprunner",
                    context=f"describe_service failed for '{service_name}' (idle service not priced)",
                )
                continue

            checks["unused_services"].append(
                {
                    "ServiceName": service_name,
                    "ServiceArn": service.get("ServiceArn"),
                    "InstanceConfiguration": instance_config,
                    "MonthlyRequests": 0,
                    "Recommendation": (
                        f"Pause or delete idle service '{service_name}' (0 requests in "
                        f"{IDLE_REQUEST_LOOKBACK_DAYS} days) to recover the 24/7 "
                        f"provisioned memory charge"
                    ),
                    "EstimatedSavings": "Full provisioned-memory monthly cost",
                    "CheckCategory": "Idle Service",
                }
            )
    except Exception as e:
        record_aws_error(
            ctx,
            e,
            service="apprunner",
            context="Could not analyze App Runner resources",
        )

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
