"""Unit tests for the load-balancer HIGH cost-audit fix (network NET-01).

Same SimpleNamespace-ctx + fake-boto3 style as
``tests/test_audit_fixes_counted_dollars.py`` / ``tests/test_lambda_audit_fixes.py``.

NET-01 (double-count): the same standalone ALBs were counted twice ---
``single_service_albs`` counted every single-listener ALB at full ``alb_monthly``
("eliminated through consolidation") while ``shared_alb_opportunity`` independently
counted ``(standalone_count - 2) x alb_monthly`` on top. Neither is backed by
per-ALB LCU/traffic evidence, and consolidation merges services onto a *surviving*
ALB so not every ALB can be eliminated.

Fix verified here: both levers are demoted to $0 advisory (``Counted=False``,
``EstimatedMonthlySavings=0.0``, ``EstimatedSavings`` parses to $0), mirroring the
sibling ``nlb_vs_alb``/``old_classic_elbs`` advisory recs. The genuinely-evidenced
``idle_listeners`` config lever (LB with zero listeners = deletable) is left counted
to prove the fix is surgical.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import services.adapters.network as network_mod
from services._savings import parse_dollar_savings
from services.adapters.network import NetworkModule
from services.load_balancer import get_load_balancer_checks


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeElbv2:
    """Minimal elbv2 client: paginated describe_load_balancers + listener/tag/rule lookups."""

    def __init__(
        self,
        load_balancers: list[dict[str, Any]],
        listeners_by_arn: dict[str, list[dict[str, Any]]],
        tags_by_arn: dict[str, list[dict[str, str]]] | None = None,
        rules_by_listener: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._lbs = load_balancers
        self._listeners = listeners_by_arn
        self._tags = tags_by_arn or {}
        self._rules = rules_by_listener or {}

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_load_balancers":
            return _FakePaginator([{"LoadBalancers": self._lbs}])
        return _FakePaginator([{}])

    def describe_listeners(self, LoadBalancerArn: str) -> dict[str, Any]:  # noqa: N803
        return {"Listeners": self._listeners.get(LoadBalancerArn, [])}

    def describe_tags(self, ResourceArns: list[str]) -> dict[str, Any]:  # noqa: N803
        arn = ResourceArns[0]
        return {"TagDescriptions": [{"ResourceArn": arn, "Tags": self._tags.get(arn, [])}]}

    def describe_rules(self, ListenerArn: str) -> dict[str, Any]:  # noqa: N803
        return {"Rules": self._rules.get(ListenerArn, [])}


class _FakeElb:
    """Minimal classic-elb client: paginated describe_load_balancers."""

    def __init__(self, classic_lbs: list[dict[str, Any]] | None = None) -> None:
        self._clbs = classic_lbs or []

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator([{"LoadBalancerDescriptions": self._clbs}])


def _ctx(
    elbv2: Any = None,
    elb: Any = None,
    *,
    pricing_engine: Any = None,
    pricing_multiplier: float = 1.0,
) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=pricing_multiplier,
        fast_mode=False,
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service="": ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service="", action=None: ctx.permissions.append((service, action, msg))
    clients = {"elbv2": elbv2, "elb": elb if elb is not None else _FakeElb()}
    ctx.client = lambda name, region=None: clients.get(name)
    return ctx


# Validated live (AWS Pricing API, AWSELB, us-east-1, 2026-06-27):
# ALB base = $0.0225/hr -> $0.0225 * 730 = $16.43/mo.
_ALB_MONTHLY = 16.43


def _pe(alb: float = _ALB_MONTHLY, nlb: float = _ALB_MONTHLY, gwlb: float = 9.49) -> SimpleNamespace:
    return SimpleNamespace(
        get_alb_monthly_price=lambda: alb,
        get_nlb_monthly_price=lambda: nlb,
        get_gwlb_monthly_price=lambda: gwlb,
    )


def _alb(name: str) -> dict[str, Any]:
    arn = f"arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/{name}/abc123"
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        "Type": "application",
        "Scheme": "internet-facing",
    }


def _listener(arn_suffix: str) -> dict[str, Any]:
    return {"ListenerArn": f"listener/{arn_suffix}"}


def _build(names_with_listener_counts: dict[str, int]) -> tuple[list[dict], dict[str, list]]:
    """Build (load_balancers, listeners_by_arn) for the given {name: listener_count}."""
    lbs: list[dict[str, Any]] = []
    listeners_by_arn: dict[str, list[dict[str, Any]]] = {}
    for name, n in names_with_listener_counts.items():
        lb = _alb(name)
        lbs.append(lb)
        listeners_by_arn[lb["LoadBalancerArn"]] = [_listener(f"{name}-{i}") for i in range(n)]
    return lbs, listeners_by_arn


def _category(out: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return list(out.get(key, []))


# --------------------------------------------------------------------------- #
# NET-01 — single_service_albs is a $0 advisory (standalone)
# --------------------------------------------------------------------------- #
def test_single_service_alb_is_advisory_zero() -> None:
    lbs, listeners = _build({"app-1": 1})
    out = get_load_balancer_checks(_ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe()))

    recs = _category(out, "single_service_albs")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # The displayed savings string carries no counted dollar.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    assert rec["CheckCategory"] == "ALB Consolidation Opportunity"
    assert "AuditBasis" in rec
    # alb_count == 1 (<=5) => no aggregate shared rec either.
    assert _category(out, "shared_alb_opportunity") == []
    # NET-05: the never-populated zero_traffic_albs category is removed.
    assert "zero_traffic_albs" not in out


# --------------------------------------------------------------------------- #
# NET-01 — k8s single-service ALB is a $0 advisory too
# --------------------------------------------------------------------------- #
def test_k8s_single_service_alb_is_advisory_zero() -> None:
    lbs, listeners = _build({"k8s-ingress-foo": 1})  # name prefix => k8s-managed
    out = get_load_balancer_checks(_ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe()))

    recs = _category(out, "single_service_albs")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    assert rec["CheckCategory"] == "K8s ALB Consolidation Opportunity"


# --------------------------------------------------------------------------- #
# NET-01 — shared_alb_opportunity (aggregate) is a $0 advisory
# --------------------------------------------------------------------------- #
def test_shared_alb_opportunity_is_advisory_zero() -> None:
    # 6 standalone single-listener ALBs => alb_count(6) > 5, standalone(6) > 2.
    lbs, listeners = _build({f"app-{i}": 1 for i in range(6)})
    out = get_load_balancer_checks(_ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe()))

    shared = _category(out, "shared_alb_opportunity")
    assert len(shared) == 1  # standalone branch only (no k8s ALBs)
    rec = shared[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    # The (standalone_count-2) ceiling lives in the warning, not the counted slot.
    assert "PricingWarning" in rec
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0

    # And every per-ALB single_service rec is advisory $0 as well.
    singles = _category(out, "single_service_albs")
    assert len(singles) == 6
    assert all(r["Counted"] is False for r in singles)
    assert all(parse_dollar_savings(r["EstimatedSavings"]) == 0.0 for r in singles)


# --------------------------------------------------------------------------- #
# NET-01 — the double-count is gone end-to-end (scan path)
# --------------------------------------------------------------------------- #
def test_scan_does_not_double_count_standalone_albs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the LB sub-shim; the other four contribute nothing.
    monkeypatch.setattr(network_mod, "get_elastic_ip_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_nat_gateway_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_vpc_endpoints_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_auto_scaling_checks", lambda c: {"recommendations": []})

    lbs, listeners = _build({f"app-{i}": 1 for i in range(6)})
    ctx = _ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe())

    findings = NetworkModule().scan(ctx)

    # Pre-fix this summed 6*16.43 (single_service) + (6-2)*16.43 (shared) = $164.30.
    pre_fix_double_count = 6 * _ALB_MONTHLY + (6 - 2) * _ALB_MONTHLY
    assert pre_fix_double_count == pytest.approx(164.30)
    # Post-fix: the LB sub-shim contributes $0 (everything advisory).
    assert findings.total_monthly_savings == pytest.approx(0.0)

    lb_recs = findings.sources["load_balancers"].recommendations
    assert len(lb_recs) == 7  # 6 single_service + 1 shared aggregate
    assert all(r.get("Counted") is False for r in lb_recs)


# --------------------------------------------------------------------------- #
# Surgical: a genuinely-evidenced lever (no-listener LB = deletable) still counts
# --------------------------------------------------------------------------- #
def test_idle_listener_lb_still_counts() -> None:
    lbs, listeners = _build({"app-empty": 0})  # zero listeners => config issue, deletable
    out = get_load_balancer_checks(_ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe()))

    idle = _category(out, "idle_listeners")
    assert len(idle) == 1
    rec = idle[0]
    # Counted (no Counted=False flag) and carries the real per-LB base saving.
    assert rec.get("Counted") is not False
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(round(_ALB_MONTHLY))
    # A zero-listener ALB is NOT also flagged as a single_service consolidation candidate.
    assert _category(out, "single_service_albs") == []


# --------------------------------------------------------------------------- #
# Fallback path: pricing_engine=None still yields advisory $0 (no fabricated $)
# --------------------------------------------------------------------------- #
def test_advisory_zero_holds_with_fallback_pricing() -> None:
    lbs, listeners = _build({f"app-{i}": 1 for i in range(6)})
    out = get_load_balancer_checks(
        _ctx(_FakeElbv2(lbs, listeners), pricing_engine=None, pricing_multiplier=2.0)
    )
    singles = _category(out, "single_service_albs")
    shared = _category(out, "shared_alb_opportunity")
    assert all(r["Counted"] is False and parse_dollar_savings(r["EstimatedSavings"]) == 0.0 for r in singles)
    assert all(r["Counted"] is False and parse_dollar_savings(r["EstimatedSavings"]) == 0.0 for r in shared)
