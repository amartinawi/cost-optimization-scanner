"""Historical spend analysis and scan-to-scan optimization opportunity tracking.

Queries AWS Cost Explorer for daily spend trends, identifies fastest-growing
services, forecasts next-month spend, and compares consecutive scan results
to track recommendation and savings deltas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# fastest_growing noise floor (F-TREND-02): a service spending fractions of a
# dollar in the prior month produces meaningless growth percentages (e.g. a
# $0.001 → $0.05 jump reads as 5000% growth). Require a defensible prior-month
# base before reporting a growth rate.
_FASTEST_GROWING_MIN_PRIOR_SPEND = 1.0


@dataclass(frozen=True)
class TrendAnalysisResult:
    """Cost Explorer historical spend analysis result.

    Attributes:
        period: Date range string for the analysis window (e.g. "2025-01-01 to 2025-03-31").
        total_spend: Total unblended cost across all services in the period.
        daily_spend_series: List of ``{"date": ..., "amount": ...}`` dicts for each day.
        top_services: Top 10 services by total spend as ``{"service": ..., "amount": ...}`` dicts.
        spend_change_pct: Percentage change, second half of the window vs the
            first half (~45d vs ~45d for a 90-day window).
        forecast: Projected next-30-day spend from Cost Explorer, or ``None`` if unavailable.
        fastest_growing: Services sorted by month-over-month % increase (min 2 months data).
    """

    period: str
    total_spend: float
    daily_spend_series: tuple[dict[str, Any], ...]
    top_services: tuple[dict[str, Any], ...]
    spend_change_pct: float
    forecast: float | None
    fastest_growing: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class OpportunityTrend:
    """Delta between two consecutive scan results.

    Attributes:
        total_recommendations_delta: Change in recommendation count (current - previous).
        total_savings_delta: Change in estimated monthly savings (current - previous).
        per_service_deltas: Per-service breakdown of recommendation and savings changes.
        savings_realization_rate: Fraction of previously identified savings that were addressed.
        is_baseline: ``True`` when there is no previous scan to compare against.
    """

    total_recommendations_delta: int
    total_savings_delta: float
    per_service_deltas: tuple[dict[str, Any], ...]
    savings_realization_rate: float
    is_baseline: bool


def _empty_trend(days_back: int) -> TrendAnalysisResult:
    """Return an empty TrendAnalysisResult for error/bypass paths.

    Args:
        days_back: Number of days originally requested (used in period string).

    Returns:
        TrendAnalysisResult with zeroed fields.
    """
    return TrendAnalysisResult(
        period=f"last {days_back} days (unavailable)",
        total_spend=0.0,
        daily_spend_series=(),
        top_services=(),
        spend_change_pct=0.0,
        forecast=None,
        fastest_growing=(),
    )


def analyze_spend_trends(ctx: Any, days_back: int = 90) -> TrendAnalysisResult:
    """Analyze historical AWS spend via Cost Explorer.

    Fetches daily unblended cost grouped by service for the requested period,
    computes top services by spend, month-over-month growth rates, and a
    30-day forward forecast.

    Args:
        ctx: ScanContext providing ``client("ce")`` for Cost Explorer access.
        days_back: Number of historical days to analyze (default 90).

    Returns:
        TrendAnalysisResult with daily series, top services, forecast, and growth data.
        Returns empty result if Cost Explorer is unavailable or any error occurs.
    """
    try:
        ce = ctx.client("ce")
        if ce is None:
            logger.warning(
                "Cost Explorer client is None (ce:* may be denied or 'ce' absent from the registry)"
            )
            return _empty_trend(days_back)

        end_date = date.today()
        start_date = end_date - timedelta(days=days_back)
        period_label = f"{start_date.isoformat()} to {end_date.isoformat()}"
        logger.debug("Fetching %d-day spend trend (%s)", days_back, period_label)

        daily_series: list[dict[str, Any]] = []
        service_totals: dict[str, float] = {}
        monthly_service_totals: dict[str, dict[str, float]] = {}

        next_token: str | None = None
        page_count = 0
        while True:
            kwargs: dict[str, Any] = {
                "TimePeriod": {"Start": start_date.isoformat(), "End": end_date.isoformat()},
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            if next_token:
                kwargs["NextPageToken"] = next_token

            resp = ce.get_cost_and_usage(**kwargs)
            page_count += 1

            results_by_time = resp.get("ResultsByTime", [])

            if page_count == 1 and len(results_by_time) == 0:
                logger.warning(
                    "Cost Explorer returned 0 ResultsByTime — CE may not be activated or the account is too new"
                )

            for page in results_by_time:
                day_start = page.get("TimePeriod", {}).get("Start", "")
                day_total = 0.0

                for group in page.get("Groups", []):
                    keys = group.get("Keys", [])
                    amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0))
                    service_name = keys[0] if keys else "Unknown"
                    if amount > 0:
                        service_totals[service_name] = service_totals.get(service_name, 0.0) + amount
                        day_total += amount

                        if day_start:
                            month_key = day_start[:7]
                            svc_months = monthly_service_totals.setdefault(service_name, {})
                            svc_months[month_key] = svc_months.get(month_key, 0.0) + amount

                if day_start:
                    daily_series.append({"date": day_start, "amount": round(day_total, 2)})

            next_token = resp.get("NextPageToken")
            if not next_token:
                break

        total_spend = sum(service_totals.values())
        logger.debug(
            "Computed total_spend=$%.2f from %d services across %d days",
            total_spend,
            len(service_totals),
            len(daily_series),
        )

        if total_spend == 0.0:
            logger.warning(
                "Cost Explorer spend-trend total is $0.00 (CE not activated, ce:GetCostAndUsage denied, "
                "or no spend in range %s)",
                period_label,
            )

        top_services = sorted(
            [{"service": k, "amount": round(v, 2)} for k, v in service_totals.items()],
            key=lambda x: x["amount"],
            reverse=True,
        )[:10]

        spend_change_pct = _compute_spend_change(daily_series)

        forecast: float | None = None
        try:
            forecast_start = end_date + timedelta(days=1)
            forecast_end = forecast_start + timedelta(days=30)
            # F-TREND-01: use DAILY granularity, NOT MONTHLY. A 30-day window
            # (e.g. 2026-06-30 → 2026-07-30) straddles two calendar months; with
            # MONTHLY granularity Cost Explorer returns one full-month bucket per
            # month touched and Total.Amount sums them — roughly DOUBLING the
            # figure (a reported "$57k 30-day forecast" against a ~$27k/mo run
            # rate). DAILY returns one bucket per day, so Total.Amount is the true
            # 30-day projection.
            fc_resp = ce.get_cost_forecast(
                TimePeriod={"Start": forecast_start.isoformat(), "End": forecast_end.isoformat()},
                Granularity="DAILY",
                Metric="UNBLENDED_COST",
            )
            forecast = round(float(fc_resp["Total"]["Amount"]), 2)
            logger.debug("30-day forecast: $%.2f", forecast)
        except Exception as fc_exc:
            logger.debug("Cost forecast failed: %s: %s", type(fc_exc).__name__, fc_exc, exc_info=True)

        fastest_growing = _compute_fastest_growing(monthly_service_totals)

        logger.debug(
            "Trend analysis complete: $%.2f over %d days, %d top services",
            total_spend,
            len(daily_series),
            len(top_services),
        )

        return TrendAnalysisResult(
            period=period_label,
            total_spend=round(total_spend, 2),
            daily_spend_series=tuple(daily_series),
            top_services=tuple(top_services),
            spend_change_pct=round(spend_change_pct, 2),
            forecast=forecast,
            fastest_growing=tuple(fastest_growing),
        )

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.debug("Trend analysis failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        # Record the failure so the missing spend trend is visible in the report
        # and scan_doctor rather than only printed to the console.
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            ctx.permission_issue(
                f"Cost Explorer spend-trend unavailable ({code}); executive-summary trend omitted",
                service="trend_analysis",
                action="ce:GetCostAndUsage",
            )
        else:
            ctx.warn(
                f"Cost Explorer spend-trend unavailable ({code or type(exc).__name__})",
                service="trend_analysis",
            )
        return _empty_trend(days_back)

    except Exception as exc:
        logger.debug("Trend analysis failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        ctx.warn(f"Cost Explorer spend-trend unavailable ({type(exc).__name__})", service="trend_analysis")
        return _empty_trend(days_back)


def _compute_spend_change(daily_series: list[dict[str, Any]]) -> float:
    """Compute percentage spend change: second half of the window vs the first half.

    Splits the daily series at its midpoint, so for the default 90-day window this
    compares the most recent ~45 days against the prior ~45 days (not 30 vs 30).

    Args:
        daily_series: List of ``{"date": ..., "amount": ...}`` dicts sorted chronologically.

    Returns:
        Percentage change (positive = increase). Returns 0.0 if insufficient data.
    """
    if len(daily_series) < 2:
        return 0.0

    mid = len(daily_series) // 2
    first_half_total = sum(d["amount"] for d in daily_series[:mid])
    second_half_total = sum(d["amount"] for d in daily_series[mid:])

    if first_half_total == 0:
        return 0.0

    return ((second_half_total - first_half_total) / first_half_total) * 100.0


def _compute_fastest_growing(monthly_service_totals: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Identify services with the highest month-over-month spend increase.

    Only includes services with at least 2 months of data. Sorted by
    growth rate descending, capped at 10 entries.

    Args:
        monthly_service_totals: Mapping of service name to {month_key: total_spend}.

    Returns:
        List of ``{"service": ..., "growth_pct": ..., "latest_month_spend": ...}`` dicts.
    """
    growing: list[dict[str, Any]] = []

    for service, months in monthly_service_totals.items():
        if len(months) < 2:
            continue
        sorted_months = sorted(months.items(), key=lambda x: x[0])
        latest_month = sorted_months[-1]
        previous_month = sorted_months[-2]
        latest_spend = latest_month[1]
        previous_spend = previous_month[1]

        # F-TREND-02: skip services whose prior-month spend is below the floor.
        # Dividing a tiny base produces absurd, misleading growth percentages
        # (e.g. CodeArtifact at 52,114% on $0.00 latest spend).
        if previous_spend < _FASTEST_GROWING_MIN_PRIOR_SPEND:
            continue

        growth_pct = ((latest_spend - previous_spend) / previous_spend) * 100.0

        growing.append(
            {
                "service": service,
                "growth_pct": round(growth_pct, 2),
                "latest_month_spend": round(latest_spend, 2),
            }
        )

    growing.sort(key=lambda x: x["growth_pct"], reverse=True)
    return growing[:10]


def compare_scan_results(current: dict[str, Any], previous: dict[str, Any] | None) -> OpportunityTrend:
    """Compare two scan result dicts to compute optimization opportunity deltas.

    Tracks how recommendations and estimated savings change between consecutive
    scans, computing per-service breakdowns and a savings realization rate.

    Args:
        current: Latest scan result dict with ``total_recommendations``,
            ``total_monthly_savings``, and ``services`` keys.
        previous: Prior scan result dict of the same shape, or ``None`` for
            the first-ever scan (baseline).

    Returns:
        OpportunityTrend with deltas and realization metrics.
        Returns a baseline result with ``is_baseline=True`` when previous is None.
    """
    if previous is None:
        logger.debug("No previous scan — establishing baseline")
        return OpportunityTrend(
            total_recommendations_delta=0,
            total_savings_delta=0.0,
            per_service_deltas=(),
            savings_realization_rate=0.0,
            is_baseline=True,
        )

    curr_recs = current.get("total_recommendations", 0)
    prev_recs = previous.get("total_recommendations", 0)
    curr_savings = current.get("total_monthly_savings", 0.0)
    prev_savings = previous.get("total_monthly_savings", 0.0)

    recs_delta = curr_recs - prev_recs
    savings_delta = round(curr_savings - prev_savings, 2)

    per_service_deltas = _compute_per_service_deltas(
        current.get("services", {}),
        previous.get("services", {}),
    )

    savings_realized = 0.0
    if prev_savings > 0 and savings_delta < 0:
        savings_realized = round(abs(savings_delta) / prev_savings, 4)

    logger.debug(
        "Scan delta: recs %+d, savings $%+.2f, realization %.1f%%",
        recs_delta,
        savings_delta,
        savings_realized * 100.0,
    )

    return OpportunityTrend(
        total_recommendations_delta=recs_delta,
        total_savings_delta=savings_delta,
        per_service_deltas=tuple(per_service_deltas),
        savings_realization_rate=savings_realized,
        is_baseline=False,
    )


def _compute_per_service_deltas(
    current_services: dict[str, Any],
    previous_services: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute per-service recommendation and savings deltas between two scans.

    Iterates services present in either scan dict, computing the change in
    recommendation count and estimated monthly savings for each.

    Args:
        current_services: Current scan's services dict keyed by service name.
        previous_services: Previous scan's services dict keyed by service name.

    Returns:
        List of ``{"service": ..., "recs_delta": ..., "savings_delta": ...}`` dicts.
    """
    all_service_keys = set(current_services.keys()) | set(previous_services.keys())
    deltas: list[dict[str, Any]] = []

    for svc in sorted(all_service_keys):
        curr = current_services.get(svc, {})
        prev = previous_services.get(svc, {})

        curr_recs = curr.get("total_recommendations", 0) if isinstance(curr, dict) else 0
        prev_recs = prev.get("total_recommendations", 0) if isinstance(prev, dict) else 0
        curr_sav = curr.get("total_monthly_savings", 0.0) if isinstance(curr, dict) else 0.0
        prev_sav = prev.get("total_monthly_savings", 0.0) if isinstance(prev, dict) else 0.0

        recs_delta = curr_recs - prev_recs
        sav_delta = round(curr_sav - prev_sav, 2)

        if recs_delta != 0 or sav_delta != 0.0:
            deltas.append(
                {
                    "service": svc,
                    "recs_delta": recs_delta,
                    "savings_delta": sav_delta,
                }
            )

    return deltas
