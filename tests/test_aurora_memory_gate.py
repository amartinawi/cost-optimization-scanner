"""M360-1 / C18 — Aurora may not downsize a database on CPU alone.

Live evidence, M360 / ap-south-1 / 2026-08-11 (account 817879235465). Both
counted Aurora recs — **$2,372.50, 49% of the whole report headline** — cut a
memory-optimized DB class's RAM on a CPU-only signal::

    prod-360vuz-db-writer  db.r6g.4xlarge -> db.r6g.large   128 -> 16 GiB  CPU 2%
    prod-integrations      db.r6g.4xlarge -> db.r6g.2xlarge 128 -> 64 GiB  CPU 5%

The first drops EIGHT rungs and 8x the RAM on a **production writer**, because
`rightsize_target_size` is purely ``current_vcpu x peak_cpu% x 1.2``. Rates and
arithmetic were verified EXACT against the live Pricing API, so the defect is
evidential — the AFS-1 class in a different adapter.

Two things make it worse than AFS-1, and they shape this fix:

* For a database, memory IS the product. Aurora's buffer pool holds the working
  set, so low CPU beside a large pool is the signature of a well-cached database.
* The evidence is FREE. Unlike EC2's ``mem_used_percent`` (CloudWatch agent
  required, which is why AFS-1 could only demote), AWS publishes
  ``AWS/RDS FreeableMemory`` for every Aurora/RDS instance with no agent.

So this GATES rather than merely demoting: the target must satisfy CPU *and*
memory, which yields a correct smaller recommendation where one exists instead of
a wrong big one. Only an unreadable metric demotes to a $0 advisory (C18: absent
evidence must not resolve toward counting). Modelled on the ElastiCache adapter,
which already gates the identical lever on peak memory usage plus evictions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.aurora_logic import (
    instance_memory_gib,
    memory_floor_size,
    rightsize_target_size,
)

_GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# Class memory
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("family", "vcpu", "expected"),
    [
        ("db.r6g", 16, 128.0),   # db.r6g.4xlarge — the M360 instance
        ("db.r6g", 2, 16.0),     # db.r6g.large   — the target it proposed
        ("db.r6g", 8, 64.0),     # db.r6g.2xlarge
        ("db.r5", 96, 768.0),
        ("db.m6g", 16, 64.0),    # general purpose = 4 GiB/vCPU
        ("db.m5", 2, 8.0),
        ("db.x2g", 2, 32.0),     # high memory = 16 GiB/vCPU
    ],
)
def test_class_memory_is_derived_from_the_family_ratio(family, vcpu, expected) -> None:
    assert instance_memory_gib(family, vcpu) == expected


def test_unknown_family_is_unverifiable_not_guessed() -> None:
    """Burstable t-family memory is NOT linear in vCPU (t3.micro 1 GiB and
    t3.large 8 GiB both have 2 vCPU), so it has no ratio and must return None
    rather than a fabricated number."""
    assert instance_memory_gib("db.t3", 2) is None
    assert instance_memory_gib("db.c6g", 4) is None
    assert instance_memory_gib("", 4) is None


# --------------------------------------------------------------------------- #
# The memory floor
# --------------------------------------------------------------------------- #
def test_the_memory_floor_blocks_the_m360_recommendation() -> None:
    """prod-360vuz-db-writer: 128 GiB total, 20 GiB freeable at its lowest, so
    ~108 GiB is in use. db.r6g.large holds 16 GiB — it cannot take the working
    set, and the CPU-only path recommended exactly that."""
    used = 128.0 - 20.0                       # ~108 GiB in use
    # 108 x 1.2 = 129.6 GiB required; db.r6g.4xlarge holds 128, so the floor is
    # the next rung up — i.e. no downsize at all, let alone eight rungs.
    assert memory_floor_size(used, gib_per_vcpu=8.0) == "8xlarge"


def test_a_genuinely_idle_instance_still_downsizes() -> None:
    """The gate must not blunt the lever: an instance whose memory really is free
    still yields a small target."""
    used = 128.0 - 124.0          # 4 GiB in use of 128
    assert memory_floor_size(used, gib_per_vcpu=8.0) == "large"


def test_the_floor_applies_headroom() -> None:
    # 12 GiB used x 1.2 = 14.4 -> large (16 GiB) fits, medium would not.
    assert memory_floor_size(12.0, gib_per_vcpu=8.0) == "large"
    # 14 GiB used x 1.2 = 16.8 -> large (16 GiB) is too small, step up.
    assert memory_floor_size(14.0, gib_per_vcpu=8.0) == "xlarge"


def test_a_workload_too_big_for_any_size_returns_none() -> None:
    assert memory_floor_size(100_000.0, gib_per_vcpu=8.0) is None


# --------------------------------------------------------------------------- #
# CPU path unchanged
# --------------------------------------------------------------------------- #
def test_the_cpu_function_is_untouched() -> None:
    """Kept pure and CPU-only on purpose — the caller composes the two floors,
    exactly as AFS-1 kept `_classify_utilization` separate from countability."""
    assert rightsize_target_size(16, 10.0) == "large"     # the M360 CPU verdict
    assert rightsize_target_size(16, 31.0) == "2xlarge"
    assert rightsize_target_size(2, 90.0) is None


# --------------------------------------------------------------------------- #
# Composition — the two floors together
# --------------------------------------------------------------------------- #
def _combined(current_vcpu: int, peak_cpu: float, family: str, freeable_gib: float):
    """Mirror the adapter: take the LARGER of the CPU and memory floors."""
    from services.aurora_logic import combined_rightsize_target

    total = instance_memory_gib(family, current_vcpu)
    used = None if total is None else total - freeable_gib
    return combined_rightsize_target(current_vcpu, peak_cpu, family, used)


def test_the_m360_writer_no_longer_downsizes_to_large() -> None:
    """CPU says 'large'; memory says '16xlarge'. The larger floor wins, and since
    it is not smaller than the current 4xlarge there is NO recommendation."""
    assert _combined(16, 10.0, "db.r6g", freeable_gib=20.0) is None


def test_a_real_over_provision_still_produces_a_rec() -> None:
    """Low CPU AND genuinely free memory: the lever survives, which is the point
    of gating rather than deleting."""
    assert _combined(16, 10.0, "db.r6g", freeable_gib=124.0) == "large"


def test_memory_can_soften_rather_than_kill_a_recommendation() -> None:
    """Memory does not only veto — where it binds above the CPU floor but still
    below the current class, the result is a SMALLER but honest downsize.

    db.r6g.16xlarge = 64 vCPU / 512 GiB. CPU peak 10% -> floor 2xlarge (8 vCPU).
    With 100 GiB genuinely in use the memory floor is 4xlarge (128 GiB), so the
    rec lands on 4xlarge instead of the CPU-only 2xlarge — still a real saving,
    just one the data supports.
    """
    assert _combined(64, 10.0, "db.r6g", freeable_gib=412.0) == "4xlarge"
    # Barely any memory in use -> the CPU floor binds and the full drop stands.
    assert _combined(64, 10.0, "db.r6g", freeable_gib=500.0) == "2xlarge"


def test_unverifiable_memory_yields_no_target() -> None:
    """None used-memory (metric unreadable, or a family with no ratio) must not
    fall through to the CPU-only answer — that is the defect itself."""
    from services.aurora_logic import combined_rightsize_target

    assert combined_rightsize_target(16, 10.0, "db.r6g", None) is None
    assert combined_rightsize_target(16, 10.0, "db.t3", None) is None


# --------------------------------------------------------------------------- #
# End to end through the adapter — the exact M360 instance
# --------------------------------------------------------------------------- #
def _adapter_recs(freeable_gib: float | None, cls: str = "db.r6g.4xlarge"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from services.adapters.aurora import _check_provisioned_instances

    rds = MagicMock()
    pag = MagicMock()
    pag.paginate.return_value = [{"DBInstances": [{
        "DBInstanceIdentifier": "prod-360vuz-db-writer", "DBInstanceClass": cls,
        "Engine": "aurora-mysql", "DBInstanceStatus": "available",
    }]}]
    rds.get_paginator.return_value = pag

    cw = MagicMock()

    def _stats(**kw):
        if kw.get("MetricName") == "FreeableMemory":
            if freeable_gib is None:
                return {"Datapoints": []}          # metric unreadable / absent
            return {"Datapoints": [{"Minimum": freeable_gib * (1024 ** 3)}]}
        return {"Datapoints": [{"Average": 2.0, "Maximum": 10.0}]}   # the M360 CPU

    cw.get_metric_statistics.side_effect = _stats

    pe = MagicMock()
    pe.get_rds_instance_monthly_price.side_effect = lambda engine, c, **k: {
        "db.r6g.4xlarge": 2.3630 * 730, "db.r6g.2xlarge": 1.1810 * 730,
        "db.r6g.large": 0.2950 * 730, "db.r6g.xlarge": 0.5900 * 730,
        "db.r6g.8xlarge": 4.7260 * 730,
    }.get(c, 0.0)
    ctx = SimpleNamespace(region="ap-south-1", pricing_multiplier=1.0, fast_mode=False,
                          pricing_engine=pe)
    ctx.client = lambda n, region=None: {"rds": rds, "cloudwatch": cw}.get(n)
    ctx.warn = MagicMock()
    ctx.permission_issue = MagicMock()
    recs = _check_provisioned_instances(ctx, rds, cw, fast_mode=False)
    return [r for r in recs if r.get("check_type") == "instance_rightsizing"], ctx


def test_the_m360_production_writer_gets_no_rightsizing_rec() -> None:
    """128 GiB class with only 20 GiB freeable — ~108 GiB in use. The CPU-only
    path counted $1,509.64 to move this to a 16 GiB class."""
    recs, ctx = _adapter_recs(freeable_gib=20.0)
    assert recs == []
    assert ctx.warn.called          # the withheld lever is disclosed, not silent


def test_an_unreadable_memory_metric_yields_no_rec() -> None:
    recs, ctx = _adapter_recs(freeable_gib=None)
    assert recs == []
    msg = " ".join(str(c) for c in ctx.warn.call_args_list)
    assert "unreadable" in msg or "memory floor" in msg


def test_a_genuinely_over_provisioned_writer_still_counts() -> None:
    """The lever must survive where the evidence supports it — otherwise this is
    a deletion, not a gate."""
    recs, _ = _adapter_recs(freeable_gib=120.0)      # ~8 GiB in use of 128
    assert len(recs) == 1
    assert recs[0]["TargetSize"] == "db.r6g.large"
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(1509.64, abs=0.01)
    # The memory evidence rides on the card so the reader can see WHY it is safe.
    assert recs[0]["PeakMemoryUsedGiB"] == pytest.approx(8.0, abs=0.1)
    assert recs[0]["TotalMemoryGiB"] == 128.0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
