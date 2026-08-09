"""CloudFront cost optimization checks.

Extracted from CostOptimizer.get_enhanced_cloudfront_checks() as a free function.
This module will later become CloudfrontModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.pricing_engine import FALLBACK_CF_DEDICATED_IP_SSL_MONTH
from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# Sentinel for dedicated-IP distributions whose certificate cannot be
# identified from the summary. They collapse into ONE group so an unreadable
# payload under-counts (a single fee) instead of multiplying the $600 by the
# number of distributions that might share the very same certificate.
_UNKNOWN_CERT = "__unidentified__"


def _certificate_key(cert: dict[str, Any]) -> str:
    """Billing identity of a viewer certificate.

    AWS charges the dedicated-IP fee per certificate, so two distributions
    sharing one certificate cost $600 total, not $1,200. ACM ARN / IAM id /
    the generic ``Certificate`` field are the identity, in that order.
    """
    for field in ("ACMCertificateArn", "IAMCertificateId", "Certificate"):
        value = cert.get(field)
        if value:
            return str(value)
    return _UNKNOWN_CERT


def _dedicated_ip_ssl_recs(
    ctx: ScanContext, certs: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """One rec per certificate served over dedicated IPs (CF-4).

    The fee is flat, exact, and account-specific, so it counts. Realizability
    is a client-compatibility question, not a pricing one: SNI works on every
    browser released after ~2010, and the recommendation carries that caveat
    rather than discounting the dollar with an unmeasurable guess.
    """
    if not certs:
        return []
    pe = ctx.pricing_engine
    monthly = (
        FALLBACK_CF_DEDICATED_IP_SSL_MONTH
        if pe is None
        else pe.get_cloudfront_dedicated_ip_ssl_monthly_price()
    )
    if monthly <= 0:
        return []

    recs: list[dict[str, Any]] = []
    for cert_key, dist_ids in sorted(certs.items()):
        identified = cert_key != _UNKNOWN_CERT
        recs.append(
            {
                "CertificateId": cert_key if identified else "unidentified",
                "DistributionIds": sorted(dist_ids),
                "DistributionCount": len(dist_ids),
                "Recommendation": (
                    "Certificate is served over dedicated IPs - switch the "
                    f"{len(dist_ids)} distribution(s) using it to SNI (sni-only) "
                    "to drop the flat monthly fee"
                ),
                "EstimatedSavings": f"${monthly:.2f}/month",
                "EstimatedMonthlySavings": round(monthly, 2),
                "AuditBasis": {
                    "metric": (
                        f"ViewerCertificate.SSLSupportMethod is dedicated-IP on "
                        f"{len(dist_ids)} enabled distribution(s)"
                    ),
                    "evidence": "cloudfront:ListDistributions",
                    "rate_monthly": round(monthly, 2),
                    "formula": (
                        "flat fee per CERTIFICATE (not per distribution) - AWS bills "
                        "$600/mo for each custom SSL certificate associated with one "
                        "or more distributions using dedicated IP, pro-rated hourly"
                    ),
                },
                "PricingWarning": (
                    "Dedicated IP exists for clients that cannot do SNI (pre-2010 "
                    "browsers). Confirm no such viewers remain before switching."
                    + ("" if identified else " Certificate identity was unreadable, so"
                       " every dedicated-IP distribution is grouped as one fee -"
                       " the real charge may be higher if they use distinct certificates.")
                ),
                "Action": (
                    "1. Confirm viewers support SNI (essentially all clients since 2010)\n"
                    "2. Update the distribution's viewer certificate to SSLSupportMethod=sni-only\n"
                    "3. Wait for the distribution to deploy, then verify HTTPS delivery"
                ),
                "CheckCategory": "CloudFront Dedicated IP SSL",
            }
        )
    return recs


def get_enhanced_cloudfront_checks(ctx: ScanContext) -> dict[str, Any]:
    """Enhanced CloudFront cost optimization checks with traffic-based gating.

    Analyzes CloudFront distributions for cost optimization opportunities:
    - Price class optimization (only for active distributions with >1000 requests/week)
    - Low traffic distribution identification
    - Origin Shield necessity analysis
    - Geographic distribution analysis for price class recommendations

    Uses CloudWatch request metrics for intelligent gating:
    - Only suggests price class changes for distributions with significant traffic
    - Analyzes 7-day request patterns to validate activity
    - Prevents recommendations for inactive or low-traffic distributions
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "price_class_optimization": [],
        "low_traffic_distributions": [],
        "origin_shield_unnecessary": [],
        "dedicated_ip_ssl": [],
    }

    # CF-4 — {certificate identity: [distribution ids]}. AWS bills this fee per
    # CERTIFICATE, not per distribution, so the recs are emitted after the walk
    # from this map rather than inside the loop.
    dedicated_ip_certs: dict[str, list[str]] = {}

    # Fast mode (cloudfront H2): a --fast scan must make no per-distribution
    # CloudWatch reads or get_distribution_config calls. Both surviving levers
    # depend on those reads — price_class_optimization is gated on >1000 weekly
    # `Requests` (CloudWatch), and the Origin-Shield analysis needs
    # get_distribution_config + CacheHitRate/Requests — so without metrics there
    # is no honest, traffic-gated recommendation to emit. We skip both per
    # distribution and surface a single advisory notice (mirrors the Lambda /
    # ElastiCache fast-mode guards). list_distributions itself is one cheap call.
    fast_mode = bool(getattr(ctx, "fast_mode", False))

    try:
        cloudfront = ctx.client("cloudfront")
        if fast_mode:
            ctx.warn(
                "Fast mode: skipped CloudFront CloudWatch reads and "
                "get_distribution_config — price-class and Origin-Shield "
                "analysis require traffic metrics and were not evaluated.",
                "cloudfront",
            )
        paginator = cloudfront.get_paginator("list_distributions")
        for page in paginator.paginate():
            for dist in page.get("DistributionList", {}).get("Items", []):
                dist_id = dist.get("Id")
                domain_name = dist.get("DomainName", "Unknown")
                price_class = dist.get("PriceClass", "PriceClass_All")
                status = dist.get("Status", "Unknown")
                enabled = dist.get("Enabled", True)

                # CF-4 — dedicated-IP custom SSL is a flat $600/mo fee, and the
                # evidence is already in the summary: no CloudWatch, no
                # get_distribution_config, so it runs in fast mode too. AWS
                # documents the charge as beginning "when you associate your
                # SSL/TLS certificate with a distribution AND you enable the
                # distribution", so a disabled distribution is not billing.
                cert = dist.get("ViewerCertificate") or {}
                if enabled and cert.get("SSLSupportMethod") in ("vip", "static-ip"):
                    dedicated_ip_certs.setdefault(_certificate_key(cert), []).append(str(dist_id))

                if fast_mode:
                    # No CW / config reads in fast mode → nothing to evaluate
                    # per distribution. Skip the metric-gated price-class block
                    # and the get_distribution_config Origin-Shield block.
                    continue

                if price_class == "PriceClass_All" and enabled:
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        # CloudFront publishes its CloudWatch metrics ONLY to
                        # us-east-1 regardless of the scanned region — querying
                        # the scan region returns zero datapoints and silently
                        # disables this lever (F-COV-01).
                        cloudwatch = ctx.client("cloudwatch", region="us-east-1")
                        request_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/CloudFront",
                            MetricName="Requests",
                            Dimensions=[{"Name": "DistributionId", "Value": dist_id}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=["Sum"],
                        )

                        total_requests = sum(dp["Sum"] for dp in request_metrics.get("Datapoints", []))

                        if total_requests > 1000:
                            checks["price_class_optimization"].append(
                                {
                                    "DistributionId": dist_id,
                                    "DomainName": domain_name,
                                    "Status": status,
                                    "CurrentPriceClass": price_class,
                                    "WeeklyRequests": f"{total_requests:.0f}",
                                    "Recommendation": (
                                        f"Active distribution ({total_requests:.0f} requests/week)"
                                        " - consider PriceClass_100/200 if users are regional"
                                    ),
                                    "EstimatedSavings": "20-50% on data transfer costs for regional traffic",
                                    "CheckCategory": "CloudFront Price Class Optimization",
                                }
                            )
                    except Exception as e:
                        # H1 — classify the Requests read failure; a swallowed
                        # error silently drops the only populated category for
                        # this distribution.
                        record_aws_error(
                            ctx,
                            e,
                            service="cloudfront",
                            context=f"cloudwatch:GetMetricStatistics Requests failed for distribution {dist_id}",
                        )

                # Disabled CloudFront distribution housekeeping finding removed: explicitly
                # $0/month — disabled distributions incur no data-transfer cost.
                #
                # Origin-Shield analysis removed: the finding it gated was deleted
                # ("net effect can go either way"), but its CloudWatch fetch was
                # left behind — a per-distribution get_distribution_config + two
                # GetMetricStatistics calls whose result was discarded. The Requests
                # call used Period=60 over a 7-day window (10080 datapoints > the
                # 1440 limit), so it raised InvalidParameterCombination for EVERY
                # distribution: 175 dead warnings + ~350 wasted API calls per scan
                # (LW-01). The whole dead block is deleted.

        checks["dedicated_ip_ssl"] = _dedicated_ip_ssl_recs(ctx, dedicated_ip_certs)
    except Exception as e:
        ctx.warn(f"Could not perform CloudFront checks: {e}", "cloudfront")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
