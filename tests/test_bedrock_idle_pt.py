"""Tests for the bedrock idle Provisioned-Throughput tightening (C1 follow-up).

A genuinely idle PT emits **no** CloudWatch ``Invocations`` datapoints (AWS does
not publish zero-value datapoints), so the old ``_get_pt_invocation_sum`` — which
returned ``None`` for both a read failure *and* absent datapoints — made every
idle PT invisible: ``_check_idle_pt`` bailed before the counted branch. The
tightening makes the helper tri-state so the caller can tell:

    - read failure      → abstain (never recommend deleting an unmeasured PT)
    - no datapoints      → candidate idle → $0 advisory (surfaced, Counted=False)
    - explicit Sum == 0  → proven idle    → counted recoverable commitment

These tests drive ``_check_idle_pt`` / ``_get_pt_invocation_sum`` directly with a
fake CloudWatch client so the counted dollar (and the advisory labeling) is
proven, not inferred.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.bedrock as bedrock

_HAIKU_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku"
_HAIKU_VERSIONED_ARN = (
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
)
_UNKNOWN_ARN = "arn:aws:bedrock:us-east-1::foundation-model/cohere.command-r"
# anthropic.claude-3-haiku → $0.80/hr × 1 unit × 730 hr = $584.00/mo
_HAIKU_MONTHLY = 0.80 * 1 * bedrock.HOURS_PER_MONTH


class _FakeCloudWatch:
    """Minimal CloudWatch double for ``get_metric_statistics``.

    ``datapoints=None`` + ``raise_exc=True`` simulates an API error (read
    failure); ``datapoints=[]`` simulates a successful read with no datapoints
    (the idle-PT case); a non-empty list simulates explicit datapoints.
    """

    def __init__(self, datapoints: list[dict[str, Any]] | None = None, raise_exc: bool = False):
        self._datapoints = datapoints
        self._raise = raise_exc
        self.last_kwargs: dict[str, Any] = {}

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        if self._raise:
            raise RuntimeError("AccessDenied: simulated CloudWatch failure")
        return {"Datapoints": self._datapoints or []}


def _pt(model_arn: str, *, units: int = 1, pt_id: str = "pt-1") -> dict[str, Any]:
    return {
        "provisionedModelId": pt_id,
        "currentModelArn": model_arn,
        "modelUnits": units,
        "status": "InService",
    }


# --------------------------------------------------------------------------- #
# _get_pt_invocation_sum — tri-state contract
# --------------------------------------------------------------------------- #
def test_invocation_sum_read_failure_returns_none_not_definitive() -> None:
    assert bedrock._get_pt_invocation_sum(_FakeCloudWatch(raise_exc=True), "m") == (None, False)


def test_invocation_sum_no_datapoints_returns_zero_not_definitive() -> None:
    # Candidate idle: absent metric is suggestive, not proof.
    assert bedrock._get_pt_invocation_sum(_FakeCloudWatch(datapoints=[]), "m") == (0.0, False)


def test_invocation_sum_with_datapoints_aggregates_and_is_definitive() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 3.0}, {"Sum": 4.0}])
    assert bedrock._get_pt_invocation_sum(cw, "m") == (7.0, True)


# --------------------------------------------------------------------------- #
# _check_idle_pt — surfacing + counted/advisory split
# --------------------------------------------------------------------------- #
def test_explicit_zero_datapoint_is_advisory_with_indicative_figure() -> None:
    """BR-2: PT_HOURLY_PRICE is unverified against the Pricing API's PT SKUs
    (order-of-magnitude discrepancies; rates vary by commitmentDuration) — a
    proven-idle PT surfaces the figure in PotentialMonthlySavings, never as a
    counted dollar."""
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["monthly_savings"] == 0.0
    assert rec["Counted"] is False
    assert rec["PotentialMonthlySavings"] == pytest.approx(_HAIKU_MONTHLY, abs=0.01)
    assert "unverified" in rec["pricing_warning"]
    assert "explicit metric" in rec["reason"]


def test_no_datapoints_is_surfaced_as_zero_advisory() -> None:
    cw = _FakeCloudWatch(datapoints=[])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1  # idle PT is now visible (previously dropped)
    rec = recs[0]
    assert rec["monthly_savings"] == 0.0
    assert rec["Counted"] is False
    assert "no Invocations datapoints" in rec["pricing_warning"]
    # The estimated recoverable spend is still named in the advisory reason.
    assert f"{_HAIKU_MONTHLY:.2f}" in rec["reason"]


def test_read_failure_abstains_no_rec() -> None:
    cw = _FakeCloudWatch(raise_exc=True)
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert recs == []  # never recommend deleting a PT we could not measure


def test_active_pt_emits_no_rec() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 1500.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert recs == []


def test_unknown_rate_is_advisory_even_when_definitive() -> None:
    # Explicit Sum=0 (definitive idle) but no PT_HOURLY_PRICE entry → never
    # fabricate a dollar; surface as a $0 advisory naming the unknown rate.
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_UNKNOWN_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["monthly_savings"] == 0.0
    assert rec["Counted"] is False
    assert "committed rate unknown" in rec["pricing_warning"]


def test_versioned_arn_maps_rate_key_and_surfaces_potential() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_VERSIONED_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1
    # -20240307-v1:0 stripped for the RATE key only → haiku figure surfaced.
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(_HAIKU_MONTHLY, abs=0.01)
    assert recs[0]["Counted"] is False


def test_invocation_query_uses_full_versioned_model_id_dimension() -> None:
    """BR-1: the CloudWatch ModelId dimension VALUE is the full canonical id
    including the version suffix — stripping it made every metric read match
    nothing, killing the idle lever on real accounts. Only the rate-table key
    is stripped."""
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    bedrock._check_idle_pt(_pt(_HAIKU_VERSIONED_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    dims = cw.last_kwargs["Dimensions"]
    assert dims == [{"Name": "ModelId", "Value": "anthropic.claude-3-haiku-20240307-v1:0"}]
    assert cw.last_kwargs["MetricName"] == "Invocations"


def test_titan_bare_version_suffix_maps_to_rate_key() -> None:
    """Titan ids carry a bare -vN (no date): amazon.titan-text-lite-v1 must
    resolve the amazon.titan-text-lite rate entry (BR-1)."""
    titan_arn = "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-text-lite-v1"
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(titan_arn), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(0.30 * bedrock.HOURS_PER_MONTH, abs=0.01)
    # And the CW read used the full id, unstripped.
    assert cw.last_kwargs["Dimensions"] == [{"Name": "ModelId", "Value": "amazon.titan-text-lite-v1"}]


def test_active_commitment_term_is_named_in_recommendation() -> None:
    """BR-3: AWS refuses deleting a PT before its commitment term ends — an
    idle PT under an active term must say so instead of recommending an
    impossible delete."""
    pt = _pt(_HAIKU_ARN)
    pt["commitmentDuration"] = "SixMonths"
    pt["commitmentExpirationTime"] = "2026-12-01T00:00:00Z"
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(pt, cw, pricing_multiplier=1.0, fast_mode=False)
    assert "SixMonths" in recs[0]["recommended_value"]
    assert "2026-12-01" in recs[0]["recommended_value"]
    # No commitment -> deletable now.
    cw2 = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs2 = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw2, pricing_multiplier=1.0, fast_mode=False)
    assert "deletable now" in recs2[0]["recommended_value"]


def test_breakeven_is_advisory_with_indicative_figures() -> None:
    """BR-4: both breakeven legs are indicative (blended token rate + the
    unverified PT table) — $0 advisory, never a counted dollar."""
    cw = _FakeCloudWatch(datapoints=[{"Sum": 1000.0}])  # token sums per read
    recs = bedrock._check_pt_breakeven(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=False)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["monthly_savings"] == 0.0
    assert rec["Counted"] is False
    assert rec["PotentialMonthlySavings"] > 0
    assert "blended" in rec["pricing_warning"]


def test_pricing_multiplier_scales_potential_figure() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.5, fast_mode=False)
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(_HAIKU_MONTHLY * 1.5, abs=0.01)


def test_multi_unit_pt_scales_potential_figure() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN, units=3), cw, pricing_multiplier=1.0, fast_mode=False)
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(_HAIKU_MONTHLY * 3, abs=0.01)


def test_fast_mode_skips_pt_check() -> None:
    cw = _FakeCloudWatch(datapoints=[{"Sum": 0.0}])
    recs = bedrock._check_idle_pt(_pt(_HAIKU_ARN), cw, pricing_multiplier=1.0, fast_mode=True)
    assert recs == []


# --------------------------------------------------------------------------- #
# Silent-swallow fix: a real PT-enumeration failure is classified, not hidden.
# --------------------------------------------------------------------------- #
class _BoomBedrock:
    def __init__(self, exc: BaseException):
        self._exc = exc

    def get_paginator(self, _name: str):
        raise self._exc

    def list_provisioned_model_throughputs(self, **_kw: Any):
        raise self._exc


def test_list_pt_enumeration_failure_is_classified() -> None:
    """Paginator AND direct call failing is a real enumeration failure — it must
    surface (AccessDenied -> permission_issue), not masquerade as 'no PTs'."""
    from types import SimpleNamespace

    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "ListProvisionedModelThroughputs",
    )
    perms: list[tuple] = []
    ctx = SimpleNamespace(region="us-east-1", account_id="123")
    ctx.permission_issue = lambda msg, service=None, action=None, **k: perms.append((service, action, msg))
    ctx.warn = lambda msg, service=None, **k: None

    out = bedrock._list_provisioned_throughputs(_BoomBedrock(err), ctx)
    assert out == []
    assert any(svc == "bedrock" for svc, *_ in perms)


def test_list_pt_without_ctx_is_backward_compatible() -> None:
    """Called without ctx (legacy), a failure still returns [] without crashing."""
    assert bedrock._list_provisioned_throughputs(_BoomBedrock(RuntimeError("boom"))) == []
