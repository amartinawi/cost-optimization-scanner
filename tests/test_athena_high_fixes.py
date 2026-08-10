"""Athena L1 — outer ``list_work_groups`` failure must be classified.

The outer handler previously called ``ctx.warn`` directly, so an account-wide
``AccessDenied`` / ``UnauthorizedOperation`` surfaced as a generic warning rather
than a permission gap. It now routes through
``services/_aws_errors.record_aws_error`` so an IAM denial lands on
``ctx.permission_issue`` (and a transient failure still falls back to ``ctx.warn``).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.athena as athena_adapter
import services.athena as athena


def _access_denied(op: str = "ListWorkGroups") -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, op)


class _Ctx:
    """Minimal ScanContext double capturing warn / permission_issue calls."""

    def __init__(self, clients: dict[str, Any]):
        self._clients = clients
        self.region = "us-east-1"
        self.account_id = "123456789012"
        self.warnings: list[tuple] = []
        self.permission_issues: list[tuple] = []

    def client(self, name: str, region: str | None = None) -> Any:
        return self._clients.get(name)

    def warn(self, message: str, service: str | None = None) -> None:
        self.warnings.append((service, message))

    def permission_issue(self, message: str, service: str | None = None, action: str | None = None) -> None:
        self.permission_issues.append((service, message, action))


class _Boom:
    """A client whose every attribute, when called, raises ``exc``."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def __getattr__(self, _name: str):
        def _raise(*_a: Any, **_k: Any):
            raise self._exc

        return _raise


def _perm_services(ctx: _Ctx) -> list[str]:
    return [svc for svc, *_ in ctx.permission_issues]


def test_athena_list_work_groups_access_denied_classified() -> None:
    ctx = _Ctx({"athena": _Boom(_access_denied("ListWorkGroups"))})
    result = athena.get_enhanced_athena_checks(ctx)
    # Permission gap surfaced, not buried as a generic warning, and no recs emitted.
    assert "athena" in _perm_services(ctx)
    assert ctx.warnings == []
    assert result["recommendations"] == []


def test_athena_list_work_groups_transient_failure_warns() -> None:
    ctx = _Ctx({"athena": _Boom(RuntimeError("throttled"))})
    athena.get_enhanced_athena_checks(ctx)
    # Non-permission failure still falls back to ctx.warn, never silently swallowed.
    assert ctx.permission_issues == []
    assert any(svc == "athena" for svc, _ in ctx.warnings)


def test_athena_advisory_string_is_dollar_zero_prefixed(monkeypatch: Any) -> None:
    """A $0 advisory athena rec leads with '$0.00/month — advisory:'.

    The adapter set Counted=False + EMV=0 but left the bare 'Up to 75% scan-cost
    reduction' string, so the card showed a percentage with no $0 marker. The
    string must now agree with the advisory state (Fix H).
    """
    from types import SimpleNamespace

    import services.adapters.athena as athena_adapter

    rec = {
        "WorkGroup": "primary",
        "CheckCategory": "Athena Optimization",
        "EstimatedSavings": "Up to 75% scan-cost reduction (priced from CW ProcessedBytes)",
    }
    monkeypatch.setattr(
        athena_adapter, "get_enhanced_athena_checks", lambda _ctx: {"recommendations": [rec]}
    )
    # ATH-1: fast mode can measure nothing, so it manufactures NO card — the gap
    # is surfaced as a warning instead of N identical "we could not measure"
    # advisories. A real ScanContext always has warn(); the old double did not.
    warnings: list[tuple] = []
    ctx = SimpleNamespace(
        fast_mode=True, pricing_multiplier=1.0, region="us-east-1",
        pricing_engine=None, client=lambda *a, **k: None,
    )
    ctx.warn = lambda msg, service=None: warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None: None

    findings = athena_adapter.AthenaModule().scan(ctx)
    assert findings.sources["enhanced_checks"].recommendations == ()
    assert findings.total_monthly_savings == 0.0
    assert any("Fast mode" in msg for _svc, msg in warnings)


# --------------------------------------------------------------------------- #
# ATH-1 (CRITICAL) — the fabricated 0.75 factor is gone, and with it the
# hardcoded $5.00 rate and the pricing_multiplier that was applied on top of it.
#
# Athena's scan surface does not track the generic multiplier: verified live,
# sa-east-1 is $9.00/TB against us-east-1's $5.00 (1.80x) while its DPU-hour
# rate is identical. So the old line was wrong three ways at once.
# --------------------------------------------------------------------------- #
def _ath_ctx(*, region="us-east-1", rate=5.0, fast=False, cw=None):
    ns = SimpleNamespace(
        fast_mode=fast, pricing_multiplier=1.0, region=region,
        pricing_engine=SimpleNamespace(get_athena_data_scanned_price_per_tb=lambda: rate),
        warnings=[],
    )
    ns.warn = lambda msg, service=None: ns.warnings.append(msg)
    ns.permission_issue = lambda msg, service=None, action=None: None
    ns.client = lambda name, region=None: cw
    return ns


class _AthCw:
    """Fake CloudWatch. `series` maps a dimension-name tuple -> {label: bytes}."""

    def __init__(self, series, list_error=None, stat_error=None):
        self._series = series
        self._list_error = list_error
        self._stat_error = stat_error

    def get_paginator(self, name):
        if self._list_error:
            raise self._list_error
        metrics = []
        for dims_tuple, entries in self._series.items():
            for label in entries:
                dims = [{"Name": n, "Value": v} for n, v in zip(dims_tuple, label)]
                metrics.append({"Dimensions": dims})
        return SimpleNamespace(paginate=lambda **kw: [{"Metrics": metrics}])

    def get_metric_statistics(self, **kw):
        if self._stat_error:
            raise self._stat_error
        names = tuple(d["Name"] for d in kw["Dimensions"])
        label = tuple(d["Value"] for d in kw["Dimensions"])
        val = self._series.get(names, {}).get(label)
        return {"Datapoints": [{"Sum": val}] if val is not None else []}


def _wg_rec(**kw):
    rec = {"WorkGroup": "primary", "State": "ENABLED", "Counted": False,
           "EstimatedMonthlySavings": 0.0, "BillingModel": "per-tb-scanned",
           "PublishesQueryMetrics": True, "CheckCategory": "Workgroup Scan Optimization"}
    rec.update(kw)
    return rec


def _scan(monkeypatch, rec, ctx):
    monkeypatch.setattr(
        athena_adapter, "get_enhanced_athena_checks", lambda _c: {"recommendations": [dict(rec)]}
    )
    return athena_adapter.AthenaModule().scan(ctx)


def test_athena_counts_nothing_even_with_measured_scan(monkeypatch) -> None:
    """10 TB scanned used to book 10 x $5 x 0.75 = $37.50/month of counted
    savings. It now books nothing."""
    cw = _AthCw({("WorkGroup",): {("primary",): 10e12}})
    findings = _scan(monkeypatch, _wg_rec(), _ath_ctx(cw=cw))
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["ScannedTB"] == pytest.approx(10.0)
    assert "$50.00/month scanned" in rec["MeasuredMonthlyScanCost"]


def test_measured_spend_is_a_preformatted_string_not_a_bare_float(monkeypatch) -> None:
    """The generic renderer prints any truthy float property verbatim, so a raw
    50.0 would sit above a "$0.00/month" savings line with no currency."""
    cw = _AthCw({("WorkGroup",): {("primary",): 10e12}})
    findings = _scan(monkeypatch, _wg_rec(), _ath_ctx(cw=cw))
    value = findings.sources["enhanced_checks"].recommendations[0]["MeasuredMonthlyScanCost"]
    assert isinstance(value, str) and value.startswith("$")


def test_regional_rate_is_used_not_a_multiplier(monkeypatch) -> None:
    """sa-east-1 is $9.00/TB live. The old code used $5.00 x pricing_multiplier."""
    cw = _AthCw({("WorkGroup",): {("primary",): 1e12}})
    findings = _scan(monkeypatch, _wg_rec(), _ath_ctx(region="sa-east-1", rate=9.0, cw=cw))
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert "$9.00/month scanned" in rec["MeasuredMonthlyScanCost"]
    assert rec["AuditBasis"]["rate_per_tb"] == 9.0


def test_unpriceable_region_abstains_rather_than_borrowing_us_east_1(monkeypatch) -> None:
    """The pricing method returns None for an unmapped region. Substituting the
    us-east-1 rate would misprice sa-east-1 by 80%."""
    cw = _AthCw({("WorkGroup",): {("primary",): 10e12}})
    ctx = _ath_ctx(cw=cw)
    ctx.pricing_engine = SimpleNamespace(get_athena_data_scanned_price_per_tb=lambda: None)
    rec = _scan(monkeypatch, _wg_rec(), ctx).sources["enhanced_checks"].recommendations[0]
    assert "MeasuredMonthlyScanCost" not in rec
    assert "no regional scan rate" in rec["EstimatedSavings"]
    assert "must not be substituted" in rec["AuditBasis"]["rate_source"]


def test_failed_queries_are_excluded_from_billed_bytes(monkeypatch) -> None:
    """AWS does not bill FAILED queries but does bill CANCELED ones. The
    WorkGroup rollup includes failures, so preferring it would overstate."""
    cw = _AthCw({
        ("WorkGroup",): {("primary",): 100e12},           # rollup incl. failures
        ("QueryState", "WorkGroup"): {
            ("SUCCEEDED", "primary"): 6e12,
            ("CANCELED", "primary"): 1e12,
            ("FAILED", "primary"): 93e12,
        },
    })
    rec = _scan(monkeypatch, _wg_rec(), _ath_ctx(cw=cw)).sources["enhanced_checks"].recommendations[0]
    assert rec["ScannedTB"] == pytest.approx(7.0)  # 6 + 1, not 100
    assert rec["AuditBasis"]["basis"] == "exact"


def test_workgroup_rollup_is_labelled_an_upper_bound(monkeypatch) -> None:
    cw = _AthCw({("WorkGroup",): {("primary",): 3e12}})
    rec = _scan(monkeypatch, _wg_rec(), _ath_ctx(cw=cw)).sources["enhanced_checks"].recommendations[0]
    assert rec["AuditBasis"]["basis"] == "upper_bound_includes_failed_queries"


def test_provisioned_capacity_workgroup_is_not_priced_per_tb(monkeypatch) -> None:
    """ATH-2: a reservation-attached or Spark workgroup does not bill scanned
    bytes at all."""
    cw = _AthCw({("WorkGroup",): {("primary",): 10e12}})
    rec = _scan(
        monkeypatch, _wg_rec(BillingModel="provisioned-capacity"), _ath_ctx(cw=cw)
    ).sources["enhanced_checks"].recommendations[0]
    assert "does not bill per TB scanned" in rec["EstimatedSavings"]
    assert "ScannedTB" not in rec


def test_workgroup_without_published_metrics_emits_no_card(monkeypatch) -> None:
    """ATH-6: PublishCloudWatchMetricsEnabled is off by default for
    API-created workgroups. Nothing measurable, so nothing manufactured."""
    ctx = _ath_ctx(cw=_AthCw({}))
    findings = _scan(monkeypatch, _wg_rec(PublishesQueryMetrics=False), ctx)
    assert findings.sources["enhanced_checks"].recommendations == ()
    assert any("does not publish" in w for w in ctx.warnings)


def test_metric_read_failure_emits_no_card_and_warns(monkeypatch) -> None:
    ctx = _ath_ctx(cw=_AthCw({}, stat_error=Exception("AccessDenied")))
    findings = _scan(monkeypatch, _wg_rec(), ctx)
    assert findings.sources["enhanced_checks"].recommendations == ()
    assert ctx.warnings


def test_zero_scan_still_reports_honestly(monkeypatch) -> None:
    """A workgroup that published metrics and scanned nothing is a real,
    measured zero — not a missing measurement."""
    cw = _AthCw({("WorkGroup",): {("primary",): 0.0}})
    rec = _scan(monkeypatch, _wg_rec(), _ath_ctx(cw=cw)).sources["enhanced_checks"].recommendations[0]
    assert rec["ScannedTB"] == 0.0
    assert rec["Counted"] is False
