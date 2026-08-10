"""Athena adapter — measures scan spend, counts nothing.

ATH-1 (CRITICAL) — this adapter used to compute

    savings = monthly_tb * $5.00 * pricing_multiplier * 0.75

and sum it into the headline. Three separate defects in one line:

* the ``0.75`` was a fabricated fraction with no account-specific input (see the
  module docstring in ``services/athena.py`` for why it cannot have one);
* ``$5.00`` was hardcoded, while Athena's scan rate is $5.00 in us-east-1 and
  $9.00 in sa-east-1 — verified live — so the constant was wrong by 80% there;
* ``pricing_multiplier`` was then applied on top, but Athena's scan surface does
  not track that multiplier at all (sa-east-1 is 1.80x on scanned TB while its
  DPU-hour rate is identical to us-east-1), so the scaling was a second error in
  an unrelated direction (C1).

The lever now counts nothing. What it reports is the measured monthly scan spend
per workgroup, priced from the live regional SKU, as context on a $0 advisory.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._aws_errors import record_aws_error
from services._base import BaseServiceModule
from services.athena import ATHENA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_athena_checks

logger = logging.getLogger(__name__)

_SCAN_WINDOW_DAYS = 30
# AWS bills Athena per TB = 10^12 bytes. The 1024**4 (TiB) divisor understates
# scans by ~9.05%.
_BYTES_PER_TB = 1e12
# AWS does not bill FAILED queries; it does bill CANCELED ones for the bytes
# scanned before cancellation.
_BILLED_QUERY_STATES = ("SUCCEEDED", "CANCELED")


def _discover_dimension_sets(cw: Any, workgroup: str) -> list[list[dict[str, str]]] | None:
    """Dimension sets AWS actually publishes for this workgroup's ProcessedBytes.

    Returns ``None`` when the listing failed. An empty list means the metric
    genuinely has no published series. ``ListMetrics`` only surfaces series
    active in roughly the last 14 days, which can truncate a 30-day window —
    disclosed in the AuditBasis rather than silently absorbed.
    """
    try:
        sets: list[list[dict[str, str]]] = []
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(Namespace="AWS/Athena", MetricName="ProcessedBytes"):
            for metric in page.get("Metrics", []):
                dims = metric.get("Dimensions", [])
                if any(d.get("Name") == "WorkGroup" and d.get("Value") == workgroup for d in dims):
                    sets.append(dims)
        return sets
    except Exception:
        return None


def _sum_metric(cw: Any, dims: list[dict[str, str]], start: datetime, end: datetime) -> float | None:
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Athena",
            MetricName="ProcessedBytes",
            Dimensions=dims,
            StartTime=start,
            EndTime=end,
            Period=_SCAN_WINDOW_DAYS * 86400,
            Statistics=["Sum"],
        )
        points = resp.get("Datapoints", [])
        return float(sum(p["Sum"] for p in points)) if points else 0.0
    except Exception:
        return None


def _measure_scanned_bytes(cw: Any, workgroup: str) -> tuple[float | None, dict[str, Any]]:
    """Bytes scanned by a workgroup over the window, with the basis used.

    Dimension sets are grouped by their NAME set. Within one name set the series
    partition a single axis and are disjoint, so they SUM; across name sets they
    are alternative views of the same bytes, so we take the MAX.

    When a ``(WorkGroup, QueryState)`` name set exists it is preferred, summing
    only the states AWS actually bills. Falling back to the bare ``WorkGroup``
    rollup would include FAILED queries, which AWS does not bill — that rollup
    is therefore an UPPER BOUND, and is labelled as one.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_SCAN_WINDOW_DAYS)

    published = _discover_dimension_sets(cw, workgroup)
    if published is None:
        return None, {"basis": "metric_listing_failed"}

    by_name_set: dict[tuple[str, ...], list[list[dict[str, str]]]] = {}
    for dims in published:
        key = tuple(sorted(d.get("Name", "") for d in dims))
        by_name_set.setdefault(key, []).append(dims)

    if not by_name_set:
        # Nothing published. Fall back to the bare WorkGroup dimension, which is
        # what a workgroup with metrics enabled but no recent activity looks
        # like, and let the caller distinguish 0.0 from None.
        total = _sum_metric(cw, [{"Name": "WorkGroup", "Value": workgroup}], start, end)
        if total is None:
            return None, {"basis": "metric_read_failed"}
        return total, {"basis": "workgroup_rollup", "dimension_sets_used": 1}

    state_key = ("QueryState", "WorkGroup")
    if state_key in by_name_set:
        total = 0.0
        used = 0
        for dims in by_name_set[state_key]:
            state = next((d.get("Value") for d in dims if d.get("Name") == "QueryState"), "")
            if state not in _BILLED_QUERY_STATES:
                continue
            part = _sum_metric(cw, dims, start, end)
            if part is None:
                return None, {"basis": "metric_read_failed"}
            total += part
            used += 1
        return total, {
            "basis": "exact",
            "billed_states": list(_BILLED_QUERY_STATES),
            "dimension_sets_used": used,
        }

    best: float | None = None
    best_basis: dict[str, Any] = {}
    for name_set, dim_list in by_name_set.items():
        subtotal = 0.0
        for dims in dim_list:
            part = _sum_metric(cw, dims, start, end)
            if part is None:
                return None, {"basis": "metric_read_failed"}
            subtotal += part
        if best is None or subtotal > best:
            best = subtotal
            best_basis = {
                "basis": (
                    "upper_bound_includes_failed_queries"
                    if name_set == ("WorkGroup",)
                    else "dimension_rollup"
                ),
                "dimension_names": list(name_set),
                "dimension_sets_used": len(dim_list),
            }
    return best, best_basis


class AthenaModule(BaseServiceModule):
    """ServiceModule adapter for Athena. Measures scan spend; counts nothing."""

    key: str = "athena"
    cli_aliases: tuple[str, ...] = ("athena",)
    display_name: str = "Athena"
    reads_fast_mode: bool = True
    requires_cloudwatch: bool = True  # adapter consults CW ProcessedBytes metric.

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Athena scanning."""
        return ("athena", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        result = get_enhanced_athena_checks(ctx)
        source_recs = result.get("recommendations", [])
        fast_mode = bool(getattr(ctx, "fast_mode", False))
        engine = getattr(ctx, "pricing_engine", None)

        recs: list[dict[str, Any]] = []
        for rec in source_recs:
            workgroup = rec.get("WorkGroup", "primary")
            billing = rec.get("BillingModel", "per-tb-scanned")
            publishes = rec.get("PublishesQueryMetrics")

            # ATH-2 — a provisioned-capacity (or Spark) workgroup does not bill
            # scanned bytes at all, so a per-TB figure would be meaningless.
            if billing != "per-tb-scanned":
                rec["Recommendation"] = (
                    f"Workgroup '{workgroup}' bills provisioned capacity (DPU-hours), not "
                    "scanned bytes - review the reservation's utilisation instead"
                )
                rec["EstimatedSavings"] = (
                    "$0.00/month - advisory: this workgroup does not bill per TB scanned"
                )
                rec["AuditBasis"] = {"billing_model": billing, "counted": False}
                recs.append(rec)
                continue

            # ATH-6 — metrics are OFF by default for API-created workgroups.
            # Without them there is nothing to measure and nothing to say, so no
            # card is manufactured; the gap is surfaced as a warning instead.
            if publishes is False:
                ctx.warn(
                    f"Athena workgroup {workgroup!r} does not publish CloudWatch query "
                    "metrics (PublishCloudWatchMetricsEnabled is off), so its scan spend "
                    "could not be measured.",
                    "athena",
                )
                continue

            if fast_mode:
                ctx.warn(
                    f"Fast mode: skipped the CloudWatch ProcessedBytes read for Athena "
                    f"workgroup {workgroup!r}.",
                    "athena",
                )
                continue

            try:
                cw = ctx.client("cloudwatch")
                scanned_bytes, basis = _measure_scanned_bytes(cw, workgroup)
            except Exception as exc:
                record_aws_error(
                    ctx, exc, service="athena",
                    context=f"cloudwatch:ProcessedBytes for workgroup {workgroup}",
                )
                scanned_bytes, basis = None, {"basis": "metric_read_failed"}

            if scanned_bytes is None:
                ctx.warn(
                    f"Could not measure Athena scan spend for workgroup {workgroup!r} "
                    f"({basis.get('basis')}).",
                    "athena",
                )
                continue

            rate = engine.get_athena_data_scanned_price_per_tb() if engine is not None else None
            scanned_tb = scanned_bytes / _BYTES_PER_TB
            rec["ScannedTB"] = round(scanned_tb, 4)
            rec["Recommendation"] = (
                f"Workgroup '{workgroup}' scanned {scanned_tb:,.2f} TB in the last "
                f"{_SCAN_WINDOW_DAYS} days - partitioning and columnar formats reduce this, "
                "by an amount that depends on the table formats and query shape"
            )
            audit: dict[str, Any] = {
                "metric": "AWS/Athena ProcessedBytes (Sum)",
                "metric_window_days": _SCAN_WINDOW_DAYS,
                "scanned_bytes": scanned_bytes,
                "bytes_per_tb": _BYTES_PER_TB,
                "counted": False,
                "reason": (
                    "the realizable fraction needs per-table format and partition keys from "
                    "Glue plus per-query attribution; Athena's API exposes neither, and the "
                    "ratio is query-shaped (AWS's own example is 3x compression x 4x column "
                    "pruning, and the 4x exists only because that query read 1 of 4 columns)"
                ),
                "note": (
                    "AWS bills a 10 MB minimum per query and rounds up to the MB, while "
                    "ProcessedBytes reports actual bytes, so this UNDER-states workgroups "
                    "running many tiny queries. ListMetrics only surfaces series active in "
                    "roughly the last 14 days, so a 30-day window can be truncated."
                ),
                **basis,
            }
            if rate is not None:
                cost = scanned_tb * rate
                audit["rate_per_tb"] = rate
                audit["rate_source"] = (
                    f"AmazonAthena DataScannedInTB live SKU for {getattr(ctx, 'region', '?')}"
                )
                audit["formula"] = "scanned_bytes / 1e12 x rate_per_tb"
                # Preformatted so the generic renderer's property loop cannot
                # print a bare float next to a $0.00 savings line.
                rec["MeasuredMonthlyScanCost"] = f"${cost:,.2f}/month scanned"
                rec["EstimatedSavings"] = (
                    f"$0.00/month - advisory: {scanned_tb:,.2f} TB scanned "
                    f"(${cost:,.2f}/month); the recoverable fraction is not measurable here"
                )
            else:
                audit["rate_source"] = (
                    "abstained - no live DataScannedInTB SKU for this region, and the "
                    "us-east-1 rate must not be substituted (sa-east-1 is 1.80x)"
                )
                rec["EstimatedSavings"] = (
                    f"$0.00/month - advisory: {scanned_tb:,.2f} TB scanned; no regional "
                    "scan rate available to price it"
                )
            rec["AuditBasis"] = audit
            recs.append(rec)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Athena",
            # Nothing on this tab is counted; see the module docstring.
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources=sources,
            optimization_descriptions=ATHENA_OPTIMIZATION_DESCRIPTIONS,
        )
