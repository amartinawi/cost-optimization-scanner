"""LS-8 — an active Database Savings Plan must demote what it already absorbs.

`_fetch_savings_plans` branched on ``Compute`` / ``SageMaker`` / ``EC2Instance``
and nothing else, so a ``Database`` plan matched no branch and fell straight
through. `CommitmentCoverage` had no field for it, and `covers_rds`,
`covers_elasticache`, `covers_opensearch` and `covers_dynamodb` consulted
Reserved-Instance sets only.

Consequence (C6): CoH / Compute-Optimizer rightsizing dollars are quoted on an
**on-demand basis**. On an account holding an active Database SP that already
absorbs the spend, no demotion fired, so those dollars stayed **COUNTED** in the
headline while being unrealizable. This is the same defect the repo already
fixed for Compute SP, SageMaker SP and RIs — reopened by the Database Savings
Plan launch (GA 2025-12).

Scope is taken from the live offering record, not from documentation
(`savingsplans:DescribeSavingsPlansOfferings`, offering
``bf9234b3-5784-4a5e-9ef4-29095d898aaf``, verified 2026-08-10)::

    productTypes = [RDS, DynamoDB, DSQL, Neptune, DocDB, ElastiCache,
                    Timestream, Keyspaces, DMS, OpenSearch]

**Redshift is absent**, so a Database SP must never demote a Redshift rec — that
would under-count a real saving. The offering carries **no ``region`` field**,
exactly like the Compute offering, so the plan is region-flexible and the flag
is a plain global boolean rather than a region-scoped set.

Deliberate imprecision, in the safe direction: the plan's rates cover only
Gen7+ RDS families, Valkey-era ElastiCache nodes and Graviton OpenSearch, but
this flag demotes every family of a covered service. Over-demotion under-counts,
which this project's tie-break rule prefers to any risk of overstating.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import services.commitment_coverage as cc
from services.commitment_coverage import CommitmentCoverage


class _FakeSpClient:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self._plans = plans

    def describe_savings_plans(self, **kw: Any) -> dict[str, Any]:
        assert kw.get("states") == ["active"]
        return {"savingsPlans": self._plans}


def _ctx(plans: list[dict[str, Any]], region: str = "us-east-1") -> SimpleNamespace:
    ctx = SimpleNamespace(region=region, warnings=[], permissions=[])
    ctx.client = lambda name, region=None: _FakeSpClient(plans)
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    return ctx


_DB_PLAN = {"savingsPlanType": "Database", "region": "eu-west-1"}


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_database_plan_is_detected() -> None:
    _fams, _compute, _sagemaker, has_database = cc._fetch_savings_plans(_ctx([_DB_PLAN]))
    assert has_database is True


def test_database_plan_is_region_flexible() -> None:
    """The live offering carries no region field, like the Compute offering, so
    a plan bought in another region still covers the scan region. Scoping it by
    region would silently fail to demote and re-open the overstatement."""
    _f, _c, _s, has_database = cc._fetch_savings_plans(_ctx([_DB_PLAN], region="ap-south-1"))
    assert has_database is True


def test_absent_database_plan_leaves_the_flag_false() -> None:
    _f, _c, _s, has_database = cc._fetch_savings_plans(_ctx([{"savingsPlanType": "Compute"}]))
    assert has_database is False


def test_database_detection_does_not_disturb_the_other_types() -> None:
    ctx = _ctx(
        [
            {"savingsPlanType": "Compute"},
            {"savingsPlanType": "SageMaker"},
            {"savingsPlanType": "EC2Instance", "region": "us-east-1", "ec2InstanceFamily": "m5"},
            _DB_PLAN,
        ]
    )
    fams, has_compute, has_sagemaker, has_database = cc._fetch_savings_plans(ctx)
    assert fams == frozenset({"m5"})
    assert (has_compute, has_sagemaker, has_database) == (True, True, True)


def test_read_failure_is_fail_safe() -> None:
    """A failed read must not assert coverage — that would demote real savings."""
    ctx = _ctx([])
    ctx.client = lambda *a, **k: (_ for _ in ()).throw(Exception("AccessDeniedException"))
    fams, has_compute, has_sagemaker, has_database = cc._fetch_savings_plans(ctx)
    assert (fams, has_compute, has_sagemaker, has_database) == (frozenset(), False, False, False)
    assert ctx.warnings or ctx.permissions


# --------------------------------------------------------------------------- #
# What it covers — from the live productTypes list
# --------------------------------------------------------------------------- #
_DB_ONLY = CommitmentCoverage(region="us-east-1", has_database_sp=True)
_NO_COMMITMENT = CommitmentCoverage(region="us-east-1")


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.covers_rds("db.r7i.4xlarge", "aurora-mysql"),
        lambda c: c.covers_rds("db.m5.large", "mysql"),
        lambda c: c.covers_aurora("db.r6g.xlarge", "aurora-postgresql"),
        lambda c: c.covers_elasticache("cache.m7g.large"),
        lambda c: c.covers_opensearch("m8g.large.search"),
        lambda c: c.covers_dynamodb(),
    ],
)
def test_database_sp_covers_its_product_types(call) -> None:
    assert call(_DB_ONLY) is True
    assert call(_NO_COMMITMENT) is False


def test_database_sp_does_not_cover_redshift() -> None:
    """Redshift is absent from the offering's productTypes. Demoting a Redshift
    rec on the strength of a Database SP would under-count a real saving."""
    assert _DB_ONLY.covers_redshift("ra3.xlplus") is False


def test_database_sp_does_not_cover_ec2() -> None:
    assert _DB_ONLY.covers_ec2("m5.large") is False


def test_dispatch_helper_honours_the_flag() -> None:
    assert _DB_ONLY.covers("rds", "db.r7i.large", "mysql") is True
    assert _DB_ONLY.covers("elasticache", "cache.m7g.large") is True
    assert _DB_ONLY.covers("opensearch", "m8g.large.search") is True
    assert _DB_ONLY.covers("redshift", "ra3.xlplus") is False
    assert _DB_ONLY.covers("ec2", "m5.large") is False


def test_has_any_commitment_sees_a_lone_database_plan() -> None:
    assert _DB_ONLY.has_any_commitment is True
    assert _NO_COMMITMENT.has_any_commitment is False


# --------------------------------------------------------------------------- #
# Engine scoping must not leak
# --------------------------------------------------------------------------- #
def test_database_sp_is_engine_agnostic_unlike_an_ri() -> None:
    """RDS RIs are engine-scoped; a Database SP is not. An engine-tagged RI set
    must not veto the SP's coverage."""
    cov = CommitmentCoverage(
        region="us-east-1",
        has_database_sp=True,
        rds_ri_engine_families=frozenset({("r5", "aurora-mysql")}),
    )
    assert cov.covers_rds("db.m6i.large", "postgres") is True


def test_ri_only_account_is_unaffected() -> None:
    """Regression guard: the flag defaults False, so accounts without a Database
    SP must behave exactly as before."""
    cov = CommitmentCoverage(region="us-east-1", rds_ri_families=frozenset({"r5"}))
    assert cov.covers_rds("db.r5.large") is True
    assert cov.covers_rds("db.m6i.large") is False
    assert cov.covers_dynamodb() is False


# --------------------------------------------------------------------------- #
# The gate that decides whether the plans are read at all
# --------------------------------------------------------------------------- #
class _RecordingCtx(SimpleNamespace):
    """Records which service clients the resolver asks for."""

    def __init__(self, region: str = "us-east-1") -> None:
        super().__init__(region=region, asked=[], warn=lambda *a, **k: None)
        self.permission_issue = lambda *a, **k: None

    def client(self, name: str, region: str | None = None) -> Any:
        self.asked.append(name)
        return _FakeSpClient([_DB_PLAN] if name == "savingsplans" else [])


@pytest.mark.parametrize("service", sorted(cc._DATABASE_SP_SERVICES))
def test_a_database_only_scan_still_reads_the_plans(service: str) -> None:
    """The gating bug this fix nearly shipped with: ``want_sp`` was keyed on the
    COMPUTE services only (ec2/lambda/containers/sagemaker). None of the services
    a Database SP covers appear in that set, so a scan of only database services
    never called `_fetch_savings_plans`, ``has_database_sp`` stayed False, and the
    demotion was inert on exactly the accounts it targets."""
    ctx = _RecordingCtx()
    cov = cc.fetch_commitment_coverage(ctx, {service})
    assert "savingsplans" in ctx.asked
    assert cov.has_database_sp is True


def test_a_scan_touching_neither_sp_family_still_skips_the_read() -> None:
    """The widened gate must not become unconditional — s3 buys no Savings Plan,
    so the call is still skipped and the account is not billed for the read."""
    ctx = _RecordingCtx()
    cov = cc.fetch_commitment_coverage(ctx, {"s3"})
    assert "savingsplans" not in ctx.asked
    assert cov.has_database_sp is False


def test_sp_pager_terminates_on_a_repeated_or_nonstring_token() -> None:
    """The SP pager looped on ``if not token: break``, so a non-string or
    repeated ``nextToken`` spun forever. Reachable on far more scans now that the
    read is no longer compute-gated, so it carries the same guard as the CE pager."""

    class _StuckClient:
        def __init__(self, token: Any) -> None:
            self._token = token
            self.calls = 0

        def describe_savings_plans(self, **kw: Any) -> dict[str, Any]:
            self.calls += 1
            assert self.calls < 10, "pager did not terminate"
            return {"savingsPlans": [_DB_PLAN], "nextToken": self._token}

    for token in (object(), "same-token-forever"):
        stuck = _StuckClient(token)
        ctx = _ctx([])
        ctx.client = lambda name, region=None, _c=stuck: _c
        _f, _c, _s, has_database = cc._fetch_savings_plans(ctx)
        assert has_database is True          # the first page was still consumed
        assert stuck.calls <= 2              # and the loop stopped


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
