"""Tests for the level-Shoes audit fixes:

  - Lambda must not count the same function via both Cost Hub and Compute
    Optimizer (CoH authority wins); metric-gated $0 recs are advisory.
  - EKS node groups scaled to 0 nodes emit no Spot/Graviton noise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------- #
# Lambda: CoH + CO dedupe + $0 advisory
# --------------------------------------------------------------------------- #
def test_lambda_dedupes_co_against_cost_hub():
    import services.adapters.lambda_svc as lam_mod
    from services.adapters.lambda_svc import LambdaModule

    fn = "datadog-forwarder-Forwarder-y61mIEWZcviZ"
    cost_hub = [{"resourceArn": f"arn:aws:lambda:eu-west-1:1:function:{fn}",
                 "estimatedMonthlySavings": 7.77}]
    co = [{"resource_name": fn, "estimatedMonthlySavings": 7.60}]
    enhanced = [
        {"FunctionName": fn, "CheckCategory": "Lambda ARM Migration", "MemorySize": 256},
        {"FunctionName": "other-fn", "CheckCategory": "Lambda ARM Migration", "MemorySize": 256},
    ]

    lam_mod.get_lambda_compute_optimizer_recommendations = lambda c: list(co)
    lam_mod.get_enhanced_lambda_checks = lambda c: {"recommendations": [dict(r) for r in enhanced]}

    ctx = SimpleNamespace(pricing_multiplier=1.0)
    ctx.cost_hub_splits = {"lambda": [dict(r) for r in cost_hub]}

    findings = LambdaModule().scan(ctx)

    # CO duplicate of the Cost Hub function is dropped → only $7.77 counted.
    assert findings.total_monthly_savings == 7.77
    assert len(findings.sources["compute_optimizer"].recommendations) == 0
    # The ARM nudge on the CoH function is also deduped out; only "other-fn" survives.
    enh = findings.sources["enhanced_checks"].recommendations
    assert [r["FunctionName"] for r in enh] == ["other-fn"]
    # Metric-gated $0 ARM rec is advisory, not counted.
    assert enh[0]["Counted"] is False


# --------------------------------------------------------------------------- #
# EKS: 0-node node groups produce no recs
# --------------------------------------------------------------------------- #
def test_eks_zero_node_group_emits_no_recs():
    from services.adapters.eks import EksCostModule

    eks = MagicMock()
    ng_pager = MagicMock()
    ng_pager.paginate.return_value = [{"nodegroups": ["zero-ng", "live-ng"]}]
    eks.get_paginator.return_value = ng_pager

    def describe_ng(clusterName, nodegroupName):
        desired = 0 if nodegroupName == "zero-ng" else 2
        return {"nodegroup": {"instanceTypes": ["m5.large"], "capacityType": "ON_DEMAND",
                              "scalingConfig": {"desiredSize": desired}}}
    eks.describe_nodegroup.side_effect = describe_ng

    pe = MagicMock()
    pe.get_ec2_hourly_price.return_value = 0.096
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ctx.client = lambda n: eks
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    mod = EksCostModule()
    recs, ng_count = mod._analyze_node_groups(ctx, eks, "cluster-x")

    # Both node groups counted, but only the live one (2 nodes) yields recs.
    assert ng_count == 2
    assert recs, "live node group should yield Spot/Graviton advisory recs"
    assert all("zero-ng" not in r["resource_id"] for r in recs)
    assert all("live-ng" in r["resource_id"] for r in recs)
