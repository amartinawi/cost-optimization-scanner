"""Historical spend analysis and scan-to-scan optimization opportunity tracking.

Queries AWS Cost Explorer for daily spend trends, identifies fastest-growing
services, forecasts next-month spend, and compares consecutive scan results
to track recommendation and savings deltas.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class TrendAnalysisResult:
    """Cost Explorer historical spend analysis result.

    Attributes:
        period: Date range string for the analysis window (e.g. "2025-01-01 to 2025-03-31").
        total_spend: Total unblended cost across all services in the period.
        daily_spend_series: List of ``{"date": ..., "amount": ...}`` dicts for each day.
        top_services: Top 10 services by total spend as ``{"service": ..., "amount": ...}`` dicts.
        spend_change_pct: Percentage change comparing last 30 days vs previous 30 days.
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
            print("🔍 [core/trend_analysis.py] Cost Explorer client is None — ctx.client('ce') returned None")
            print(
                "🔍 [core/trend_analysis.py] This usually means the 'ce' service is not in the client registry or IAM denies ce:*"
            )
            return _empty_trend(days_back)

        print(f"🔍 [core/trend_analysis.py] CE client obtained successfully: type={type(ce).__name__}")

        end_date = date.today()
        start_date = end_date - timedelta(days=days_back)
        period_label = f"{start_date.isoformat()} to {end_date.isoformat()}"
        print(f"🔍 [core/trend_analysis.py] Fetching {days_back}-day spend trend ({period_label})")
        print(
            f"🔍 [core/trend_analysis.py] Date range query: start={start_date.isoformat()}, end={end_date.isoformat()}"
        )

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
                print(f"🔍 [core/trend_analysis.py] Paginating — fetching page {page_count + 2} with NextPageToken")

            resp = ce.get_cost_and_usage(**kwargs)
            page_count += 1

            results_by_time = resp.get("ResultsByTime", [])
            groups_in_page = sum(len(page.get("Groups", [])) for page in results_by_time)
            print(
                f"🔍 [core/trend_analysis.py] API page {page_count}: {len(results_by_time)} ResultsByTime entries, {groups_in_page} total groups"
            )

            if page_count == 1 and len(results_by_time) == 0:
                print(
                    "🔍 [core/trend_analysis.py] WARNING: First page returned 0 ResultsByTime — Cost Explorer may not be activated or account may be too new"
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

        if next_token is None and page_count > 0:
            print(f"🔍 [core/trend_analysis.py] Pagination complete: {page_count} page(s) fetched")

        total_spend = sum(service_totals.values())
        print(
            f"🔍 [core/trend_analysis.py] Computed total_spend=${total_spend:,.2f} from {len(service_totals)} unique services across {len(daily_series)} days"
        )

        if total_spend == 0.0:
            print("🔍 [core/trend_analysis.py] WARNING: total_spend is $0.00 — possible causes:")
            print("🔍 [core/trend_analysis.py]   1. Cost Explorer not activated (enable in Billing console)")
            print("🔍 [core/trend_analysis.py]   2. IAM missing ce:GetCostAndUsage permission")
            print("🔍 [core/trend_analysis.py]   3. Account has no spend in the queried date range")
            print(f"🔍 [core/trend_analysis.py]   4. Date range may be in the future or before account creation")
            print(
                f"🔍 [core/trend_analysis.py]   daily_series length={len(daily_series)}, service_totals keys={list(service_totals.keys())[:10]}"
            )

        top_services = sorted(
            [{"service": k, "amount": round(v, 2)} for k, v in service_totals.items()],
            key=lambda x: x["amount"],
            reverse=True,
        )[:10]

        spend_change_pct = _compute_spend_change(daily_series)

        forecast: float | None = None
        try:
            tomorrow = end_date + timedelta(days=1)
            forecast_end = tomorrow + timedelta(days=30)
            print(
                f"🔍 [core/trend_analysis.py] Requesting forecast: {tomorrow.isoformat()} to {forecast_end.isoformat()}"
            )
            fc_resp = ce.get_cost_forecast(
                TimePeriod={"Start": tomorrow.isoformat(), "End": forecast_end.isoformat()},
                Granularity="MONTHLY",
                Metric="UNBLENDED_COST",
            )
            forecast = round(float(fc_resp["Total"]["Amount"]), 2)
            print(f"🔍 [core/trend_analysis.py] 30-day forecast: ${forecast:,.2f}")
        except Exception as fc_exc:
            print(f"🔍 [core/trend_analysis.py] Cost forecast failed: {type(fc_exc).__name__}: {fc_exc}")
            print(f"🔍 [core/trend_analysis.py] Forecast traceback: {traceback.format_exc()}")

        fastest_growing = _compute_fastest_growing(monthly_service_totals)

        print(
            f"🔍 [core/trend_analysis.py] Trend analysis complete: ${total_spend:,.2f} over {len(daily_series)} days, {len(top_services)} top services"
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

    except Exception as exc:
        print(f"🔍 [core/trend_analysis.py] Trend analysis failed: {type(exc).__name__}: {exc}")
        print(f"🔍 [core/trend_analysis.py] Full traceback:\n{traceback.format_exc()}")
        return _empty_trend(days_back)


def _compute_spend_change(daily_series: list[dict[str, Any]]) -> float:
    """Compute percentage spend change: last 30 days vs previous 30 days.

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

        if previous_spend > 0:
            growth_pct = ((latest_spend - previous_spend) / previous_spend) * 100.0
        else:
            growth_pct = 0.0

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
        print("🔍 [core/trend_analysis.py] No previous scan — establishing baseline")
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

    print(
        f"🔍 [core/trend_analysis.py] Scan delta: recs {recs_delta:+d}, savings ${savings_delta:+,.2f}, realization {savings_realized:.1%}"
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
