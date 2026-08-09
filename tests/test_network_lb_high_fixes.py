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

from datetime import datetime, timedelta, timezone
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
        tgs_by_lb: dict[str, list[str]] | None = None,
        targets_by_tg: dict[str, list[dict[str, Any]]] | None = None,
        tg_error: Exception | None = None,
    ) -> None:
        self._lbs = load_balancers
        self._listeners = listeners_by_arn
        self._tags = tags_by_arn or {}
        self._rules = rules_by_listener or {}
        # NET-E: {lb_arn: [tg_arn, ...]} and {tg_arn: [target, ...]}
        self._tgs_by_lb = tgs_by_lb or {}
        self._targets_by_tg = targets_by_tg or {}
        self._tg_error = tg_error

    def describe_target_groups(self, LoadBalancerArn: str) -> dict[str, Any]:  # noqa: N803
        if self._tg_error is not None:
            raise self._tg_error
        return {
            "TargetGroups": [
                {"TargetGroupArn": arn} for arn in self._tgs_by_lb.get(LoadBalancerArn, [])
            ]
        }

    def describe_target_health(self, TargetGroupArn: str) -> dict[str, Any]:  # noqa: N803
        return {"TargetHealthDescriptions": self._targets_by_tg.get(TargetGroupArn, [])}

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
    fast_mode: bool = False,
) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=pricing_multiplier,
        fast_mode=fast_mode,
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


_CLB_MONTHLY = 18.25  # $0.025/hr x 730 (AWSELB "Load Balancer" productFamily)


def _pe(
    alb: float = _ALB_MONTHLY,
    nlb: float = _ALB_MONTHLY,
    gwlb: float = 9.49,
    clb: float = _CLB_MONTHLY,
) -> SimpleNamespace:
    return SimpleNamespace(
        get_alb_monthly_price=lambda: alb,
        get_nlb_monthly_price=lambda: nlb,
        get_gwlb_monthly_price=lambda: gwlb,
        get_clb_monthly_price=lambda: clb,
    )


# Old enough to clear _MIN_IDLE_AGE_DAYS; individual tests override it.
_AGED = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _alb(name: str, created: datetime | None = _AGED) -> dict[str, Any]:
    arn = f"arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/{name}/abc123"
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        **({"CreatedTime": created} if created is not None else {}),
        "Type": "application",
        "Scheme": "internet-facing",
    }


def _listener(arn_suffix: str) -> dict[str, Any]:
    return {"ListenerArn": f"listener/{arn_suffix}"}


def _build(
    names_with_listener_counts: dict[str, int], created: datetime | None = _AGED
) -> tuple[list[dict], dict[str, list]]:
    """Build (load_balancers, listeners_by_arn) for the given {name: listener_count}."""
    lbs: list[dict[str, Any]] = []
    listeners_by_arn: dict[str, list[dict[str, Any]]] = {}
    for name, n in names_with_listener_counts.items():
        lb = _alb(name, created)
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
    monkeypatch.setattr(
        network_mod, "get_nat_gateway_checks", lambda c, **kw: {"recommendations": [], "nat_vpc_map": {}}
    )
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
    # NET-B: full precision, and the numeric agrees with the string — the old
    # ":.0f"-only rec made the headline take $16 for a $16.43 LB while every
    # numeric consumer read $0.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(_ALB_MONTHLY, abs=0.01)
    assert rec["EstimatedMonthlySavings"] == pytest.approx(_ALB_MONTHLY, abs=0.01)
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


# --------------------------------------------------------------------------- #
# NET-E — idle LB lever: zero registered targets is DEFINITIVE, and every
# ambiguous read abstains (a delete rec must never rest on missing evidence).
# --------------------------------------------------------------------------- #
def _idle_case(tgs_by_lb, targets_by_tg, tg_error=None):
    lbs, listeners = _build({"app-1": 1})  # has a listener, so NET-B doesn't fire
    elbv2 = _FakeElbv2(
        lbs, listeners, tgs_by_lb=tgs_by_lb, targets_by_tg=targets_by_tg, tg_error=tg_error
    )
    return get_load_balancer_checks(_ctx(elbv2, pricing_engine=_pe()))


def _lb_arn(name: str = "app-1") -> str:
    lbs, _ = _build({name: 1})
    return lbs[0]["LoadBalancerArn"]


def test_lb_with_no_registered_targets_is_counted_idle() -> None:
    arn = _lb_arn()
    out = _idle_case({arn: ["tg-1", "tg-2"]}, {"tg-1": [], "tg-2": []})
    idle = [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"]
    assert len(idle) == 1
    assert idle[0]["EstimatedMonthlySavings"] == pytest.approx(_ALB_MONTHLY, abs=0.01)
    assert idle[0].get("Counted") is not False
    assert "0 registered targets" in idle[0]["AuditBasis"]["metric"]


def test_lb_with_registered_targets_is_not_flagged() -> None:
    arn = _lb_arn()
    out = _idle_case({arn: ["tg-1", "tg-2"]}, {"tg-1": [], "tg-2": [{"Target": {"Id": "i-1"}}]})
    assert [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"] == []


def test_lb_with_no_target_groups_abstains() -> None:
    """No target groups at all is ambiguous (the LB may be wired another way),
    not proof of idleness."""
    out = _idle_case({}, {})
    assert [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"] == []


def test_lb_target_enumeration_failure_abstains() -> None:
    """C8: losing the evidence must never create a counted delete rec."""
    arn = _lb_arn()
    out = _idle_case({arn: ["tg-1"]}, {"tg-1": []}, tg_error=Exception("AccessDenied"))
    assert [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"] == []


def test_classic_lb_with_no_instances_is_counted_idle() -> None:
    clb = {
        "LoadBalancerName": "clb-idle",
        "CreatedTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "Instances": [],
    }
    lbs, listeners = _build({"app-1": 1})
    out = get_load_balancer_checks(
        _ctx(_FakeElbv2(lbs, listeners), _FakeElb([clb]), pricing_engine=_pe())
    )
    idle = [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"]
    assert len(idle) == 1 and idle[0]["LoadBalancerName"] == "clb-idle"
    assert idle[0]["EstimatedMonthlySavings"] == pytest.approx(_CLB_MONTHLY, abs=0.01)


def test_classic_lb_with_instances_is_not_flagged() -> None:
    clb = {
        "LoadBalancerName": "clb-busy",
        "CreatedTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "Instances": [{"InstanceId": "i-1"}],
    }
    lbs, listeners = _build({"app-1": 1})
    out = get_load_balancer_checks(
        _ctx(_FakeElbv2(lbs, listeners), _FakeElb([clb]), pricing_engine=_pe())
    )
    assert [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"] == []


def test_classic_lb_without_instances_key_abstains() -> None:
    """A payload missing Instances entirely is unknown, not empty."""
    clb = {"LoadBalancerName": "clb-unknown", "CreatedTime": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    lbs, listeners = _build({"app-1": 1})
    out = get_load_balancer_checks(
        _ctx(_FakeElbv2(lbs, listeners), _FakeElb([clb]), pricing_engine=_pe())
    )
    assert [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == "Idle Load Balancer"] == []


# --------------------------------------------------------------------------- #
# T5-1 — the two conservatism gaps carried out of the tranche-4 review.
#
# (a) A load balancer younger than _MIN_IDLE_AGE_DAYS may simply be
#     mid-provisioning. The charge is real, but "delete it" is the wrong call,
#     so the rec demotes to a $0 advisory that keeps the figure rather than
#     disappearing. Applies to BOTH idle branches — gating one and not its
#     sibling (which makes the same delete recommendation) would be incoherent.
# (b) The zero-target lever costs 1+N describe calls per LB, so --fast skips it
#     entirely: a fast scan under-counts rather than guesses.
# --------------------------------------------------------------------------- #
_YOUNG = datetime.now(timezone.utc) - timedelta(days=2)


def _idle_rec(out: dict[str, Any], category: str) -> dict[str, Any] | None:
    recs = [r for r in _category(out, "idle_listeners") if r["CheckCategory"] == category]
    assert len(recs) <= 1
    return recs[0] if recs else None


def _zero_listener_case(created: datetime | None) -> dict[str, Any]:
    lbs, listeners = _build({"app-1": 0}, created)
    return get_load_balancer_checks(_ctx(_FakeElbv2(lbs, listeners), pricing_engine=_pe()))


def _zero_target_case(created: datetime | None, *, fast: bool = False) -> dict[str, Any]:
    lbs, listeners = _build({"app-1": 1}, created)
    arn = lbs[0]["LoadBalancerArn"]
    elbv2 = _FakeElbv2(lbs, listeners, tgs_by_lb={arn: ["tg-1"]}, targets_by_tg={"tg-1": []})
    return get_load_balancer_checks(_ctx(elbv2, pricing_engine=_pe(), fast_mode=fast))


def test_young_lb_with_no_listeners_is_advisory_not_counted() -> None:
    rec = _idle_rec(_zero_listener_case(_YOUNG), "Load Balancer Configuration Issue")
    assert rec is not None, "the finding must stay visible, just uncounted"
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["PotentialMonthlySavings"] == pytest.approx(_ALB_MONTHLY, abs=0.01)
    assert "mid-provisioning" in rec["EstimatedSavings"]


def test_young_lb_with_no_targets_is_advisory_not_counted() -> None:
    """Same gate on the sibling branch — both make the same delete call."""
    rec = _idle_rec(_zero_target_case(_YOUNG), "Idle Load Balancer")
    assert rec is not None
    assert rec["Counted"] is False
    assert rec["PotentialMonthlySavings"] == pytest.approx(_ALB_MONTHLY, abs=0.01)


def test_lb_with_unreadable_creation_time_is_advisory() -> None:
    """Unknown age is not old age — demote rather than count."""
    for out, category in (
        (_zero_listener_case(None), "Load Balancer Configuration Issue"),
        (_zero_target_case(None), "Idle Load Balancer"),
    ):
        rec = _idle_rec(out, category)
        assert rec is not None and rec["Counted"] is False
        assert "creation time unreadable" in rec["EstimatedSavings"]


def test_aged_lb_still_counts_and_carries_its_age() -> None:
    for out, category in (
        (_zero_listener_case(_AGED), "Load Balancer Configuration Issue"),
        (_zero_target_case(_AGED), "Idle Load Balancer"),
    ):
        rec = _idle_rec(out, category)
        assert rec is not None
        assert rec.get("Counted") is not False
        assert rec["EstimatedMonthlySavings"] == pytest.approx(_ALB_MONTHLY, abs=0.01)
        assert rec["AgeDays"] > 365


def test_fast_mode_skips_the_zero_target_lever() -> None:
    assert _idle_rec(_zero_target_case(_AGED, fast=True), "Idle Load Balancer") is None
    # ...but the branch that needs no extra API call still fires.
    assert _idle_rec(_zero_listener_case(_AGED), "Load Balancer Configuration Issue") is not None


def test_fast_mode_makes_no_target_group_calls() -> None:
    """Not just "emits nothing" — the calls must not happen at all."""
    lbs, listeners = _build({"app-1": 1})
    arn = lbs[0]["LoadBalancerArn"]
    elbv2 = _FakeElbv2(lbs, listeners, tgs_by_lb={arn: ["tg-1"]}, targets_by_tg={"tg-1": []})
    calls: list[str] = []
    original = elbv2.describe_target_groups
    elbv2.describe_target_groups = lambda **kw: (calls.append("tg"), original(**kw))[1]  # type: ignore[method-assign]
    get_load_balancer_checks(_ctx(elbv2, pricing_engine=_pe(), fast_mode=True))
    assert calls == []


def test_network_module_declares_it_reads_fast_mode() -> None:
    assert NetworkModule.reads_fast_mode is True
