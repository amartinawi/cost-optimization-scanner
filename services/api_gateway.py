"""API Gateway cost optimization checks (REST APIs only).

Extracted from CostOptimizer.get_enhanced_api_gateway_checks() as a free function.
This module will later become ApiGatewayModule (T-321) implementing ServiceModule.

Scope (api_gateway H4 — honest coverage statement).
This module scans **only v1 REST APIs** (the ``apigateway`` ``get_rest_apis``
surface). HTTP and WebSocket APIs (the v2 ``apigatewayv2`` ``get_apis`` surface)
are intentionally NOT scanned. They are a real, separately-billed cost — HTTP
$1.00/M requests (``USE1-ApiGatewayHttpRequest``); WebSocket $1.00/M messages
(``USE1-ApiGatewayMessage``) plus $0.25/M connection minutes
(``USE1-ApiGatewayMinute``), all validated against the live AWS Pricing API on
2025-11-20 (us-east-1) — but no defensible *counted* saving lever exists for them
from configuration alone: HTTP API is already the cheapest API type (no cheaper
migration target the way REST→HTTP is), and any WebSocket saving (idle-API delete)
would require per-API usage metrics to quantify and is a destructive rec that must
fail safe. Rather than fabricate uncounted coverage, the REST-only scope is
documented here; apigatewayv2 coverage is deferred until a usage-gated, live-priced
saving can be emitted. The single REST lever is the REST→HTTP migration candidate
(≤10 resources), counted strictly from measured CloudWatch request volume.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# Per-million request rates, validated live against the AWS Pricing API
# (AmazonApiGateway, us-east-1, publication 2025-11-20):
#   REST request first-tier  USE1-ApiGatewayRequest      = $3.50/M (first 333M)
#   HTTP request first-tier   USE1-ApiGatewayHttpRequest  = $1.00/M (first 300M)
# Region scaling for non-us-east-1 is applied once via ctx.pricing_multiplier in
# the adapter. The REST→HTTP migration saving is the first-tier delta ($2.50/M);
# higher request-volume tiers are cheaper, so this is a conservative (floor) rate.
REST_PER_M = 3.50
HTTP_PER_M = 1.00
SAVINGS_PER_M = REST_PER_M - HTTP_PER_M

# AG-1 — the real request-price LADDERS, validated against the live Pricing API
# 2026-08-10 (AmazonApiGateway us-east-1, usagetype USE1-ApiGatewayRequest /
# USE1-ApiGatewayHttpRequest). Each entry is (upper_bound_requests, $/million);
# the final bound is None for "and above".
#
# A flat first-tier delta is NOT a conservative floor above the first tier, it
# is a CEILING: REST falls to $2.80/M after 333M while HTTP only falls to
# $0.90/M after 300M, so the gap narrows as volume grows. At 500M requests the
# flat $2.50/M claims $1,250/month where the ladders give $1,153.10 — $96.90
# overstated, and worse at higher volumes.
REST_REQUEST_TIERS: tuple[tuple[int | None, float], ...] = (
    (333_000_000, 3.50),
    (1_000_000_000, 2.80),
    (20_000_000_000, 2.38),
    (None, 1.51),
)
HTTP_REQUEST_TIERS: tuple[tuple[int | None, float], ...] = (
    (300_000_000, 1.00),
    (None, 0.90),
)


def tiered_request_cost(requests: float, tiers: tuple[tuple[int | None, float], ...]) -> float:
    """Monthly $ for ``requests`` priced through a tier ladder.

    Tiers fill from the bottom, so this is the real bill rather than
    ``volume x first_tier_rate``.
    """
    if requests <= 0:
        return 0.0
    total = 0.0
    consumed = 0.0
    for upper, rate_per_million in tiers:
        ceiling = float(upper) if upper is not None else float("inf")
        billable = min(requests, ceiling) - consumed
        if billable <= 0:
            continue
        total += (billable / 1_000_000) * rate_per_million
        consumed += billable
        if consumed >= requests:
            break
    return total


def rest_to_http_savings(account_requests: float, api_requests: float) -> float:
    """This API's share of the ACCOUNT-WIDE REST -> HTTP saving.

    AG-1's second half: request tiers are account-wide (per region), not
    per-API, so pricing each API independently walks every one of them up from
    the first tier and overstates the total. The account-wide delta is computed
    once on the summed volume and allocated pro-rata by each API's share, so the
    per-API figures reconcile to the account total by construction.
    """
    if account_requests <= 0 or api_requests <= 0:
        return 0.0
    delta = tiered_request_cost(account_requests, REST_REQUEST_TIERS) - tiered_request_cost(
        account_requests, HTTP_REQUEST_TIERS
    )
    if delta <= 0:
        return 0.0
    return delta * (api_requests / account_requests)

API_GATEWAY_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "rest_vs_http": {
        "title": "Migrate Simple REST APIs to HTTP API",
        "description": "REST APIs with ≤10 resources can migrate to cheaper HTTP APIs for 10-30% cost reduction.",
        "action": "Review simple REST APIs and migrate to HTTP API where feature compatibility allows",
    },
    "stage_caches": {
        "title": "Remove Unused REST Stage Caches",
        "description": (
            "A provisioned stage cache bills 24/7 by size ($14.60-$2,774/month) whether "
            "or not any request hits it."
        ),
        "action": "Disable the cache on stages that serve no traffic",
    },
}


# apigateway CacheClusterStatus values in which the cache is provisioned and
# therefore billing. CREATE_IN_PROGRESS is excluded deliberately: it is a
# transient state, and recommending deletion of something mid-creation would be
# noise. DELETE_IN_PROGRESS / NOT_AVAILABLE are already on their way out.
_BILLING_CACHE_STATUSES = frozenset({"AVAILABLE", "FLUSH_IN_PROGRESS"})

_CACHE_METRIC_WINDOW_DAYS = 30


def _stage_request_count(ctx: ScanContext, api_name: str, stage_name: str) -> float | None:
    """Requests to one stage over the window, or ``None`` if unreadable.

    ``(ApiName, Stage)`` is a standard AWS/ApiGateway dimension pair — unlike
    the four-dimension form it does NOT require detailed metrics to be enabled.
    ``None`` means the read failed or was skipped, which is not evidence of
    zero traffic (H2).
    """
    if ctx.fast_mode:
        return None
    try:
        from datetime import datetime

        cw = ctx.client("cloudwatch")
        end = datetime.now(UTC)
        start = end - timedelta(days=_CACHE_METRIC_WINDOW_DAYS)
        resp = cw.get_metric_statistics(
            Namespace="AWS/ApiGateway",
            MetricName="Count",
            Dimensions=[
                {"Name": "ApiName", "Value": api_name},
                {"Name": "Stage", "Value": stage_name},
            ],
            StartTime=start,
            EndTime=end,
            Period=_CACHE_METRIC_WINDOW_DAYS * 86400,
            Statistics=["Sum"],
        )
        return float(sum(dp["Sum"] for dp in resp.get("Datapoints", [])))
    except Exception as exc:
        record_aws_error(
            ctx,
            exc,
            service="api_gateway",
            context=f"CloudWatch Count read failed for stage '{api_name}/{stage_name}'",
        )
        return None


def _stage_cache_recs(
    ctx: ScanContext, apigateway: Any, api_id: str, api_name: str
) -> list[dict[str, Any]]:
    """One rec per provisioned REST stage cache (AG-3).

    Counted only when the stage demonstrably served zero requests over the
    window: a cache fronting live traffic may be load-bearing, and this scanner
    cannot measure the backend cost it offsets. Everything else — traffic
    present, metric unreadable, fast mode — renders the exact figure as a $0
    advisory instead.
    """
    try:
        stages = apigateway.get_stages(restApiId=api_id).get("item", [])
    except Exception as exc:
        record_aws_error(
            ctx,
            exc,
            service="api_gateway",
            context=f"apigateway:GetStages failed for API '{api_name}'",
        )
        return []

    recs: list[dict[str, Any]] = []
    for stage in stages:
        if not stage.get("cacheClusterEnabled"):
            continue
        if stage.get("cacheClusterStatus") not in _BILLING_CACHE_STATUSES:
            continue
        size = str(stage.get("cacheClusterSize") or "")
        pe = ctx.pricing_engine
        monthly = pe.get_apigateway_cache_monthly_price(size) if pe is not None else 0.0
        if monthly <= 0:
            # Unknown size or no rate — abstain rather than invent a dollar.
            ctx.warn(
                f"API Gateway stage {api_name}/{stage.get('stageName')} has a cache of "
                f"unpriceable size {size!r}; skipped",
                "api_gateway",
            )
            continue

        stage_name = str(stage.get("stageName") or "")
        requests = _stage_request_count(ctx, api_name, stage_name)
        idle = requests == 0.0
        rec: dict[str, Any] = {
            "ApiId": api_id,
            "ApiName": api_name,
            "StageName": stage_name,
            "CacheClusterSize": size,
            "MonthlyRequests": requests,
            "Recommendation": (
                f"Stage cache ({size}GB) is provisioned 24/7 and the stage served no "
                "requests in the last 30 days - disable the cache"
                if idle
                else f"Stage cache ({size}GB) is provisioned 24/7 - confirm it earns its cost"
            ),
            "CheckCategory": "API Gateway Stage Cache",
            "AuditBasis": {
                "metric": "AWS/ApiGateway Count (Sum), dimensions ApiName+Stage",
                "metric_window_days": _CACHE_METRIC_WINDOW_DAYS,
                "monthly_requests": requests,
                "cache_size_gb": size,
                "rate_monthly": round(monthly, 2),
                "rate_source": (
                    "AmazonApiGateway productFamily 'Amazon API Gateway Cache' "
                    "(AWS Pricing API, validated 2026-08-09)"
                ),
                "formula": "cache hourly rate x 730 (billed whether or not the cache is hit)",
            },
        }
        if idle:
            rec["EstimatedSavings"] = f"${monthly:.2f}/month"
            rec["EstimatedMonthlySavings"] = round(monthly, 2)
        else:
            reason = (
                f"stage served {requests:,.0f} requests in the last "
                f"{_CACHE_METRIC_WINDOW_DAYS} days; the cache may be load-bearing and "
                "the backend cost it offsets is not measured here"
                if requests is not None
                else "request metric unavailable, so idleness is unproven"
            )
            rec["EstimatedSavings"] = f"$0.00/month - advisory: {reason}"
            rec["EstimatedMonthlySavings"] = 0.0
            rec["PotentialMonthlySavings"] = round(monthly, 2)
            rec["Counted"] = False
        recs.append(rec)
    return recs


def get_enhanced_api_gateway_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced API Gateway cost optimization checks for REST APIs.

    Scans **only** v1 REST APIs (``apigateway:GetRestApis``). HTTP and WebSocket
    (v2 ``apigatewayv2``) APIs are intentionally out of scope — see the module
    docstring (api_gateway H4) for the rationale. The single emitted lever is a
    REST→HTTP migration candidate for APIs with ≤10 resources; its counted saving
    is ``(REST $3.50/M − HTTP $1.00/M) × measured monthly requests``. A failed or
    fast-mode-skipped CloudWatch read yields a ``Counted=False`` $0 advisory, never
    a fabricated counted dollar.

    Args:
        ctx: ScanContext with region, clients, fast_mode, and pricing data.

    Returns:
        A dict with a flat ``recommendations`` list plus one list per check
        category (currently only ``rest_vs_http``).
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "rest_vs_http": [],
        "stage_caches": [],
    }

    try:
        apigateway = ctx.client("apigateway")
        paginator = apigateway.get_paginator("get_rest_apis")
        for page in paginator.paginate():
            for api in page.get("items", []):
                api_id = api.get("id")
                api_name = api.get("name", "Unknown")

                try:
                    resources = apigateway.get_resources(restApiId=api_id)
                    resource_count = len(resources.get("items", []))

                    if resource_count <= 10:
                        monthly_requests = 0.0
                        # H2 — a failed CloudWatch read is NOT evidence of zero
                        # traffic; only a successful empty Datapoints is a genuine
                        # 0. Track the failure so the rec is not counted on it.
                        metric_read_failed = False
                        if not ctx.fast_mode:
                            try:
                                from datetime import datetime

                                cw = ctx.client("cloudwatch")
                                end = datetime.now(UTC)
                                start = end - timedelta(days=30)
                                resp = cw.get_metric_statistics(
                                    Namespace="AWS/ApiGateway",
                                    MetricName="Count",
                                    Dimensions=[{"Name": "ApiName", "Value": api_name}],
                                    StartTime=start,
                                    EndTime=end,
                                    Period=2592000,
                                    Statistics=["Sum"],
                                )
                                monthly_requests = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
                            except Exception as cw_exc:
                                record_aws_error(
                                    ctx,
                                    cw_exc,
                                    service="api_gateway",
                                    context=f"CloudWatch Count read failed for API '{api_name}'",
                                )
                                metric_read_failed = True

                        # AG-1 — priced after the walk, once the account-wide
                        # request total is known (tiers are account-wide).
                        estimated_savings = 0.0

                        rec = {
                            "ApiId": api_id,
                            "ApiName": api_name,
                            "ApiType": "REST",
                            "ResourceCount": resource_count,
                            "Recommendation": "Simple API - consider migrating to HTTP API for lower cost",
                            "EstimatedSavings": "10-30% cost reduction for simple APIs",
                            "EstimatedMonthlySavings": estimated_savings,
                            "MonthlyRequests": monthly_requests,
                            "CheckCategory": "API Gateway Type Optimization",
                            # Defensible from the report alone (rule 8). Rates
                            # validated live: AmazonApiGateway us-east-1, AWS
                            # Pricing API publication 2025-11-20.
                            "AuditBasis": {
                                "rest_first_tier_per_million": REST_PER_M,
                                "http_first_tier_per_million": HTTP_PER_M,
                                "rest_tiers": [list(t) for t in REST_REQUEST_TIERS],
                                "http_tiers": [list(t) for t in HTTP_REQUEST_TIERS],
                                "rate_source": "AmazonApiGateway USE1-ApiGatewayRequest / "
                                "USE1-ApiGatewayHttpRequest tier ladders "
                                "(AWS Pricing API, validated 2026-08-10)",
                                "metric": "AWS/ApiGateway Count (Sum)",
                                "metric_window_days": 30,
                                "monthly_requests": monthly_requests,
                                "formula": (
                                    "account-wide (tiered REST cost - tiered HTTP cost), "
                                    "allocated by this API's share of account requests"
                                ),
                            },
                        }
                        if metric_read_failed:
                            # No usage evidence → advisory, never a counted dollar.
                            rec["Counted"] = False
                            rec["MetricReadFailed"] = True
                            rec["EstimatedSavings"] = "$0.00/month — advisory: request metric unavailable"
                        checks["rest_vs_http"].append(rec)
                except Exception as res_exc:
                    # H1 — classify, don't swallow: an IAM-gapped GetResources must
                    # surface as a permission_issue, not a vanished API.
                    record_aws_error(
                        ctx,
                        res_exc,
                        service="api_gateway",
                        context=f"apigateway:GetResources failed for API '{api_name}'",
                    )

                # AG-3 — the previous note here ("caching actually adds API
                # Gateway cost $0.020-$3.80/hr by cache size") found the rate and
                # then deleted the lever instead of inverting it. A PROVISIONED
                # stage cache bills 24/7 whether or not anything hits it, so a
                # cache on a stage with zero requests is pure waste at a known,
                # exact rate. That is the lever; the old framing (does caching
                # pay for itself in reduced backend cost?) needed backend
                # pricing this scanner does not measure, but this one does not.
                checks["stage_caches"].extend(_stage_cache_recs(ctx, apigateway, api_id, api_name))

    except Exception as e:
        # H1 — classify the outer failure (account-wide AccessDenied on
        # GetRestApis must read as a permission gap, not an empty tab).
        record_aws_error(ctx, e, service="api_gateway", context="API Gateway checks failed")

    # AG-1 — second pass. Request tiers are account-wide, so the delta is
    # computed once on the summed volume and allocated by share; pricing each
    # API independently walked every one of them up from the first tier.
    candidates = [r for r in checks["rest_vs_http"] if r.get("Counted") is not False]
    account_requests = sum(float(r.get("MonthlyRequests") or 0.0) for r in candidates)
    for rec in candidates:
        api_requests = float(rec.get("MonthlyRequests") or 0.0)
        saving = rest_to_http_savings(account_requests, api_requests)
        rec["EstimatedMonthlySavings"] = round(saving, 2)
        rec["AuditBasis"]["account_monthly_requests"] = account_requests
        rec["AuditBasis"]["account_request_share"] = (
            round(api_requests / account_requests, 6) if account_requests else 0.0
        )
        if saving > 0:
            # B2 — the string and the numeric must agree; the shim used to leave
            # a "10-30% cost reduction" percentage on a counted rec (AG-2).
            rec["EstimatedSavings"] = f"${saving:,.2f}/month"

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
