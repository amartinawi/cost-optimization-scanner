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

from datetime import timedelta, timezone
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

API_GATEWAY_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "rest_vs_http": {
        "title": "Migrate Simple REST APIs to HTTP API",
        "description": "REST APIs with ≤10 resources can migrate to cheaper HTTP APIs for 10-30% cost reduction.",
        "action": "Review simple REST APIs and migrate to HTTP API where feature compatibility allows",
    },
}


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
                                end = datetime.now(timezone.utc)
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

                        estimated_savings = (
                            (monthly_requests / 1_000_000) * SAVINGS_PER_M if monthly_requests > 0 else 0.0
                        )

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
                                "rest_rate_per_million": REST_PER_M,
                                "http_rate_per_million": HTTP_PER_M,
                                "savings_rate_per_million": SAVINGS_PER_M,
                                "rate_source": "AmazonApiGateway USE1-ApiGatewayRequest / "
                                "USE1-ApiGatewayHttpRequest (AWS Pricing API 2025-11-20)",
                                "metric": "AWS/ApiGateway Count (Sum)",
                                "metric_window_days": 30,
                                "monthly_requests": monthly_requests,
                                "formula": "(monthly_requests / 1e6) * (REST_PER_M - HTTP_PER_M)",
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

                # API Gateway Caching finding removed: "Reduced backend costs" with no
                # quantification — caching actually adds API Gateway cost ($0.020-$3.80/hr
                # by cache size); whether net savings exist depends on backend pricing
                # not measured here.
                _ = api_id

    except Exception as e:
        # H1 — classify the outer failure (account-wide AccessDenied on
        # GetRestApis must read as a permission gap, not an empty tab).
        record_aws_error(ctx, e, service="api_gateway", context="API Gateway checks failed")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
