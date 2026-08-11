"""AFS-1 / C10 — CPU is not the binding dimension on a memory-optimized instance.

Live evidence, afs-prod / af-south-1 / 2026-08-11 (account 370525687312). All
three counted EC2 rightsizing recs — **$2,682.34, 39% of the whole report
headline** — were memory-optimized instances told to HALVE their RAM on a
CPU-only signal::

    i-0b2e25be910fac823  r6i.8xlarge -> r6i.4xlarge  256->128 GiB  avg CPU 1.8%
    i-0bad855b6ad4b7288  r6i.4xlarge -> r6i.2xlarge  128-> 64 GiB  avg CPU 1.2%
    i-0447467b36b8732d7  r8i.2xlarge -> r8i.xlarge    64-> 32 GiB  avg CPU 4.0%

The rates and arithmetic were verified EXACT against the live Pricing API — the
defect is evidential. `_classify_utilization` computes
``memory_bound = mem_pct is not None and mem_pct > 80``, and ``mem_pct`` comes
from ``CWAgent mem_used_percent``, which requires the CloudWatch agent. So on
the overwhelmingly common no-agent account, UNKNOWN memory resolved toward
COUNTING the dollar — inverting this project's rule that ambiguity resolves
toward under-counting.

Low CPU on an r-family host is the EXPECTED signature of a correctly-sized
memory-bound workload: 256 GiB is the reason an r6i.8xlarge was chosen at all.
The same argument applies to the ``idle`` verdict, whose dollar is LARGER (the
whole instance, not a one-size delta), so both verdicts require the evidence.

Deliberately NOT suppressed, only demoted: the card still renders with its
figure in ``AdvisoryEstimate``, so the opportunity stays visible and the reader
knows to check memory or install the agent. General-purpose and compute families
are unchanged — there, low CPU IS evidence of over-provisioning.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services._savings import parse_dollar_savings
from services.ec2 import _classify_utilization, _is_memory_optimized, _rightsize_evidence_ok


# --------------------------------------------------------------------------- #
# Family classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "instance_type",
    ["r6i.8xlarge", "r5.large", "r8i.2xlarge", "r7g.medium",
     "x2idn.16xlarge", "x1e.xlarge", "x8g.large",
     "z1d.large", "u-6tb1.metal", "u7i-12tb.224xlarge"],
)
def test_memory_optimized_families_are_recognised(instance_type: str) -> None:
    assert _is_memory_optimized(instance_type) is True


@pytest.mark.parametrize(
    "instance_type",
    ["m5.large", "m6i.8xlarge", "c6i.large", "c5a.4xlarge", "t3.nano",
     "t3a.2xlarge", "i4i.large", "d3.xlarge", "g5.xlarge", "p4d.24xlarge"],
)
def test_other_families_are_not_memory_optimized(instance_type: str) -> None:
    """i/d (storage) and g/p (accelerated) also have a non-CPU binding dimension,
    but that is a separate finding with no live evidence yet — they stay counted
    so this fix does not silently widen beyond what was demonstrated."""
    assert _is_memory_optimized(instance_type) is False


def test_unparseable_type_is_not_treated_as_memory_optimized() -> None:
    for junk in ["", "garbage", "r", "."]:
        assert _is_memory_optimized(junk) is False


# --------------------------------------------------------------------------- #
# The evidence rule
# --------------------------------------------------------------------------- #
def test_memory_optimized_without_memory_data_is_not_countable() -> None:
    """The exact af-south-1 case."""
    assert _rightsize_evidence_ok("r6i.8xlarge", None) is False


def test_memory_optimized_with_memory_data_is_countable() -> None:
    """Evidence present and below the pressure threshold — count it."""
    assert _rightsize_evidence_ok("r6i.8xlarge", 40.0) is True


def test_general_purpose_without_memory_data_is_unchanged() -> None:
    """Regression guard: this is the common case and must keep counting."""
    assert _rightsize_evidence_ok("m5.4xlarge", None) is True
    assert _rightsize_evidence_ok("c6i.2xlarge", None) is True
    assert _rightsize_evidence_ok("t3.nano", None) is True


def test_high_memory_pressure_still_suppresses_entirely() -> None:
    """Unchanged: memory data ABOVE the threshold kills the verdict outright,
    which is stronger than demoting it."""
    assert _classify_utilization(12.0, 30.0, mem_pct=92.0) is None
    assert _classify_utilization(2.0, 8.0, mem_pct=92.0) == "idle"


def test_classifier_itself_is_untouched() -> None:
    """The evidence rule is separate from classification on purpose: the verdict
    still says what the CPU shows, and the caller decides whether it may be
    COUNTED. Keeping them apart is what lets the card still render."""
    assert _classify_utilization(1.8, 12.6) == "rightsize"
    assert _classify_utilization(2.0, 8.0) == "idle"


# --------------------------------------------------------------------------- #
# End to end through the shim
# --------------------------------------------------------------------------- #
_PRICES = {
    "r6i.8xlarge": 4.16, "r6i.4xlarge": 2.08, "r6i.2xlarge": 1.04,
    "m5.4xlarge": 1.00, "m5.2xlarge": 0.50,
}


class _FakeCw:
    def get_metric_statistics(self, **_kw):
        return {"Datapoints": [{"Average": 3.0, "Maximum": 12.0}]}


def _run(instance_type: str, verdict: str, mem: float | None):
    """Drive get_enhanced_ec2_checks with one instance and a pinned verdict.

    The verdict is pinned rather than derived so these tests exercise the
    EVIDENCE rule in isolation — `_classify_utilization` has its own tests.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import services.ec2 as ec2_mod

    paginator = MagicMock()
    paginator.paginate.return_value = [{"Reservations": [{"Instances": [
        {"InstanceId": "i-afs1", "InstanceType": instance_type,
         "State": {"Name": "running"}, "PlatformDetails": "Windows", "Tags": []}
    ]}]}]
    ec2_client = MagicMock()
    ec2_client.get_paginator.return_value = paginator
    ec2_client.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
    cw = _FakeCw()

    def _price(itype, os_name="Linux", license_model="No License required", quiet=False):
        return _PRICES[itype]

    engine = MagicMock()
    engine.get_ec2_hourly_price.side_effect = _price
    ctx = SimpleNamespace(
        region="af-south-1", fast_mode=False, pricing_multiplier=1.0,
        pricing_engine=engine,
        client=lambda name, region=None: cw if name == "cloudwatch" else ec2_client,
        warn=MagicMock(), permission_issue=MagicMock(),
    )

    mp = pytest.MonkeyPatch()
    mp.setattr(ec2_mod, "_classify_utilization", lambda *a, **k: verdict)
    mp.setattr(ec2_mod, "_network_bytes_per_hour", lambda *a, **k: None)
    mp.setattr(ec2_mod, "_memory_used_percent", lambda *a, **k: mem)
    try:
        return ec2_mod.get_enhanced_ec2_checks(ctx, 1.0, fast_mode=False)["recommendations"]
    finally:
        mp.undo()


def _only(recs: list, category: str) -> dict:
    hits = [r for r in recs if r.get("CheckCategory") == category]
    assert len(hits) == 1, f"expected exactly one {category} rec, got {len(hits)}"
    return hits[0]


def test_the_af_south_1_case_renders_but_no_longer_counts() -> None:
    rec = _only(_run("r6i.8xlarge", "rightsize", None), "Rightsizing Opportunities")
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # B2/B3 lockstep: the tab total parses the STRING, so it must read $0 too.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    # The opportunity is still visible, with its real figure preserved.
    assert rec["AdvisoryEstimate"] == pytest.approx(1518.40, abs=0.01)
    assert "memory" in rec["EstimatedSavings"].lower()


def test_the_same_instance_counts_once_memory_is_measured() -> None:
    rec = _only(_run("r6i.8xlarge", "rightsize", 35.0), "Rightsizing Opportunities")
    assert rec.get("Counted", True) is not False
    assert rec["EstimatedMonthlySavings"] == pytest.approx(1518.40, abs=0.01)
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(1518.40, abs=0.01)
    assert rec["AvgMemory"] == "35.0%"


def test_general_purpose_still_counts_without_memory_data() -> None:
    rec = _only(_run("m5.4xlarge", "rightsize", None), "Rightsizing Opportunities")
    assert rec.get("Counted", True) is not False
    assert rec["EstimatedMonthlySavings"] == pytest.approx(365.0, abs=0.01)


def test_the_idle_verdict_needs_the_same_evidence() -> None:
    """The idle dollar is the WHOLE instance, so leaving this path counted would
    have left a bigger hole than the one being closed."""
    rec = _only(_run("r6i.8xlarge", "idle", None), "Idle Instances")
    assert rec["Counted"] is False
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    assert rec["AdvisoryEstimate"] > 0


def test_idle_general_purpose_is_unchanged() -> None:
    rec = _only(_run("m5.4xlarge", "idle", None), "Idle Instances")
    assert rec.get("Counted", True) is not False
    assert parse_dollar_savings(rec["EstimatedSavings"]) > 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
