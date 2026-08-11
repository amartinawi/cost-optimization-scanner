"""M360-3 / C18 — an OpenSearch node downsize needs heap evidence, not just CPU.

Found by the sweep that M360-2 prompted (see the M360 ledger). M360-2 itself was
WITHDRAWN — `services/rds.py:183`'s `instance_rightsizing` turned out to be an
orphaned report descriptor, not a lever; RDS rightsizing dollars come from
Compute Optimizer and Cost Optimization Hub, which are AWS-computed. But sweeping
every adapter that decides a downsize from CPU turned up a real third instance of
the AFS-1 / M360-1 class:

`services/opensearch.py:235` emits "Underutilized Domain" on
``avg CPU < LOW_CPU_THRESHOLD`` alone, and `services/adapters/opensearch.py:510`
prices it as a one-size-down node delta that stays **COUNTED** whenever the delta
prices (only an unpriceable target or CoH coverage demotes it).

An OpenSearch data node is **heap-bound**: the JVM holds field data, indices and
caches, and a one-rung downsize halves the node's RAM and therefore its heap. Low
CPU is the normal profile of a search cluster serving from a warm heap.

As with Aurora, the evidence is FREE — AWS publishes ``JVMMemoryPressure`` on
``AWS/ES`` with no agent. The threshold is derived, not invented: halving the
heap roughly doubles pressure, and AWS treats sustained pressure above 75% as GC
territory, so a node may only be downsized when its observed MAXIMUM pressure is
below ~37.5%. That is the same reasoning ElastiCache used to land on 35%.

Fail-closed: an unreadable metric withholds the lever (C18 — absent evidence must
not resolve toward counting).

Two adapters already do this correctly and were confirmed unaffected by the
sweep: ElastiCache (peak `DatabaseMemoryUsagePercentage` <= 35% AND zero
evictions) and ECS (`avg CPU < 20% AND avg memory < 30%`, returning None when
either metric is missing).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.opensearch import MAX_JVM_PRESSURE_PCT, heap_headroom_ok


# --------------------------------------------------------------------------- #
# The threshold
# --------------------------------------------------------------------------- #
def test_the_threshold_leaves_room_for_a_halved_heap() -> None:
    """Halving the heap roughly doubles pressure; AWS treats sustained >75% as
    GC territory. The bound must therefore sit at or below half of that."""
    assert MAX_JVM_PRESSURE_PCT <= 37.5
    assert MAX_JVM_PRESSURE_PCT * 2 <= 75.0


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_a_cold_heap_permits_the_downsize() -> None:
    assert heap_headroom_ok(12.0) is True


def test_a_pressured_heap_blocks_it() -> None:
    """55% now becomes ~110% on a halved heap — an unusable node."""
    assert heap_headroom_ok(55.0) is False


def test_the_boundary_is_inclusive() -> None:
    assert heap_headroom_ok(MAX_JVM_PRESSURE_PCT) is True
    assert heap_headroom_ok(MAX_JVM_PRESSURE_PCT + 0.1) is False


def test_an_unreadable_metric_withholds_the_lever() -> None:
    """C18 — absence of evidence is not evidence of headroom."""
    assert heap_headroom_ok(None) is False


# --------------------------------------------------------------------------- #
# End to end through the shim
# --------------------------------------------------------------------------- #
def _run(avg_cpu: float, jvm_max: float | None):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from services.opensearch import get_enhanced_opensearch_checks

    os_client = MagicMock()
    os_client.list_domain_names.return_value = {"DomainNames": [{"DomainName": "d1"}]}
    os_client.describe_domain.return_value = {"DomainStatus": {
        "DomainName": "d1", "Created": True, "Processing": False,
        "ClusterConfig": {"InstanceType": "r6g.large.search", "InstanceCount": 2,
                          "DedicatedMasterEnabled": False, "WarmEnabled": False},
        "EBSOptions": {"EBSEnabled": True, "VolumeSize": 100, "VolumeType": "gp3"},
        "EngineVersion": "OpenSearch_2.11",
    }}

    cw = MagicMock()

    def _stats(**kw):
        name = kw.get("MetricName")
        if name == "JVMMemoryPressure":
            return {"Datapoints": [] if jvm_max is None else [{"Maximum": jvm_max}]}
        if name == "CPUUtilization":
            return {"Datapoints": [{"Average": avg_cpu, "Maximum": avg_cpu}]}
        return {"Datapoints": [{"Average": 5.0, "Maximum": 5.0}]}

    cw.get_metric_statistics.side_effect = _stats
    ctx = SimpleNamespace(region="ap-south-1", pricing_multiplier=1.0, fast_mode=False,
                          account_id="817879235465", pricing_engine=MagicMock())
    ctx.client = lambda n, region=None: {"opensearch": os_client, "es": os_client,
                                         "cloudwatch": cw}.get(n, MagicMock())
    ctx.warn = MagicMock()
    ctx.permission_issue = MagicMock()
    return get_enhanced_opensearch_checks(ctx), ctx


def test_low_cpu_with_a_hot_heap_emits_no_downsize() -> None:
    checks, ctx = _run(avg_cpu=8.0, jvm_max=60.0)
    assert checks.get("underutilized_domains", []) == []
    assert ctx.warn.called


def test_low_cpu_with_a_cold_heap_still_emits() -> None:
    """The lever must survive where the evidence supports it."""
    checks, _ = _run(avg_cpu=8.0, jvm_max=15.0)
    recs = checks.get("underutilized_domains", [])
    assert len(recs) == 1
    assert recs[0]["PeakJVMMemoryPressure"] == 15.0


def test_unreadable_heap_metric_emits_no_downsize() -> None:
    checks, ctx = _run(avg_cpu=8.0, jvm_max=None)
    assert checks.get("underutilized_domains", []) == []
    assert ctx.warn.called


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
