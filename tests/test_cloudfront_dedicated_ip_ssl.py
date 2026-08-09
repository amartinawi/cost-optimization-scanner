"""CF-4 — the dedicated-IP custom SSL fee.

AWS charges "$600 per month for each custom SSL certificate associated with one
or more CloudFront distributions using the Dedicated IP version of custom SSL
certificate support" (verified verbatim against the pricing page, and against
the live Pricing API: AmazonCloudFront, usagetype ``SSL-Cert-Custom``, SKU
QUEZ7XDZJJXURBU7, $600.00/Mo, location "Any").

Two details in that sentence drive the tests below:

* **per certificate, not per distribution** — two distributions sharing one
  certificate cost $600 total. Counting per distribution would double it.
* **"associated with ... and you enable the distribution"** — the charge starts
  when the distribution is enabled, so a disabled one is not billing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.adapters.cloudfront import CloudfrontModule
from services.cloudfront import get_enhanced_cloudfront_checks

_FEE = 600.0
_CATEGORY = "CloudFront Dedicated IP SSL"


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self) -> list[dict[str, Any]]:
        return self._pages


class _FakeCloudFront:
    def __init__(self, distributions: list[dict[str, Any]]) -> None:
        self._items = distributions

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator([{"DistributionList": {"Items": self._items}}])


class _FakeCloudWatch:
    """No datapoints — keeps the traffic-gated levers quiet."""

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        return {"Datapoints": []}


def _dist(
    dist_id: str,
    *,
    ssl: str | None = "vip",
    cert_arn: str | None = "arn:aws:acm:us-east-1:1:certificate/abc",
    enabled: bool = True,
    **cert_extra: Any,
) -> dict[str, Any]:
    cert: dict[str, Any] = dict(cert_extra)
    if ssl is not None:
        cert["SSLSupportMethod"] = ssl
    if cert_arn is not None:
        cert["ACMCertificateArn"] = cert_arn
    return {
        "Id": dist_id,
        "DomainName": f"{dist_id}.cloudfront.net",
        "Status": "Deployed",
        "Enabled": enabled,
        "PriceClass": "PriceClass_100",  # not PriceClass_All -> no CW lever
        "ViewerCertificate": cert,
    }


def _ctx(distributions: list[dict[str, Any]], *, pricing: Any = "default", fast: bool = False) -> SimpleNamespace:
    engine = (
        SimpleNamespace(get_cloudfront_dedicated_ip_ssl_monthly_price=lambda: _FEE)
        if pricing == "default"
        else pricing
    )
    ctx = SimpleNamespace(
        pricing_engine=engine,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast,
        warnings=[],
    )
    clients = {"cloudfront": _FakeCloudFront(distributions), "cloudwatch": _FakeCloudWatch()}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda message, service="": ctx.warnings.append((service, message))
    ctx.permission_issue = lambda message, service="", action=None: None
    return ctx


def _recs(distributions: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    out = get_enhanced_cloudfront_checks(_ctx(distributions, **kw))
    return [r for r in out["recommendations"] if r["CheckCategory"] == _CATEGORY]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_dedicated_ip_distribution_is_counted() -> None:
    recs = _recs([_dist("E1")])
    assert len(recs) == 1
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(_FEE)
    assert recs[0].get("Counted") is not False
    assert recs[0]["DistributionIds"] == ["E1"]


def test_static_ip_counts_too() -> None:
    """The legacy dedicated-IP allocation carries the same fee."""
    assert len(_recs([_dist("E1", ssl="static-ip")])) == 1


def test_sni_only_is_free_and_emits_nothing() -> None:
    assert _recs([_dist("E1", ssl="sni-only")]) == []


def test_default_certificate_emits_nothing() -> None:
    assert _recs([{"Id": "E1", "Enabled": True, "ViewerCertificate": {"CloudFrontDefaultCertificate": True}}]) == []


# --------------------------------------------------------------------------- #
# The two billing details
# --------------------------------------------------------------------------- #
def test_two_distributions_sharing_a_certificate_are_one_fee() -> None:
    """The charge is per CERTIFICATE. Per-distribution counting would say $1,200."""
    recs = _recs([_dist("E1"), _dist("E2")])
    assert len(recs) == 1
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(_FEE)
    assert recs[0]["DistributionIds"] == ["E1", "E2"]
    assert recs[0]["DistributionCount"] == 2


def test_distinct_certificates_are_distinct_fees() -> None:
    recs = _recs(
        [
            _dist("E1", cert_arn="arn:aws:acm:us-east-1:1:certificate/aaa"),
            _dist("E2", cert_arn="arn:aws:acm:us-east-1:1:certificate/bbb"),
        ]
    )
    assert len(recs) == 2
    assert sum(r["EstimatedMonthlySavings"] for r in recs) == pytest.approx(2 * _FEE)


def test_iam_certificate_id_is_a_valid_identity() -> None:
    recs = _recs([_dist("E1", cert_arn=None, IAMCertificateId="IAMCERT1")])
    assert len(recs) == 1 and recs[0]["CertificateId"] == "IAMCERT1"


def test_disabled_distribution_is_not_billing() -> None:
    """AWS: the charge begins when the certificate is associated AND the
    distribution is enabled."""
    assert _recs([_dist("E1", enabled=False)]) == []


def test_disabled_sibling_does_not_suppress_an_enabled_one() -> None:
    recs = _recs([_dist("E1", enabled=False), _dist("E2")])
    assert len(recs) == 1 and recs[0]["DistributionIds"] == ["E2"]


def test_unidentifiable_certificates_collapse_into_one_fee() -> None:
    """An unreadable identity must under-count (one fee), never multiply $600
    across distributions that may share the very same certificate."""
    recs = _recs([_dist("E1", cert_arn=None), _dist("E2", cert_arn=None)])
    assert len(recs) == 1
    assert recs[0]["CertificateId"] == "unidentified"
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(_FEE)
    assert "may be higher" in recs[0]["PricingWarning"]


# --------------------------------------------------------------------------- #
# Pricing + mode behaviour
# --------------------------------------------------------------------------- #
def test_fee_comes_from_the_pricing_engine_not_a_constant() -> None:
    engine = SimpleNamespace(get_cloudfront_dedicated_ip_ssl_monthly_price=lambda: 720.0)
    recs = _recs([_dist("E1")], pricing=engine)
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(720.0)
    assert recs[0]["AuditBasis"]["rate_monthly"] == pytest.approx(720.0)


def test_no_pricing_engine_falls_back_to_the_published_rate() -> None:
    recs = _recs([_dist("E1")], pricing=None)
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(_FEE)


def test_zero_rate_abstains_rather_than_emitting_a_counted_zero() -> None:
    engine = SimpleNamespace(get_cloudfront_dedicated_ip_ssl_monthly_price=lambda: 0.0)
    assert _recs([_dist("E1")], pricing=engine) == []


def test_lever_survives_fast_mode() -> None:
    """The evidence is in the ListDistributions summary: no CloudWatch, no
    get_distribution_config, so --fast has nothing to skip."""
    assert len(_recs([_dist("E1")], fast=True)) == 1


# --------------------------------------------------------------------------- #
# Adapter: the blanket advisory demotion must not eat this lever
# --------------------------------------------------------------------------- #
def test_adapter_keeps_the_fee_counted_and_totals_it() -> None:
    findings = CloudfrontModule().scan(_ctx([_dist("E1")]))
    assert findings.total_monthly_savings == pytest.approx(_FEE)
    assert findings.total_recommendations == 1
    rec = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    )
    assert rec.get("Counted") is not False
    assert "$600.00/month" in rec["EstimatedSavings"]


def test_adapter_still_demotes_every_other_lever() -> None:
    """A PriceClass_All distribution with traffic produces the advisory lever;
    it must stay $0 and uncounted."""
    dist = _dist("E9", ssl="sni-only")
    dist["PriceClass"] = "PriceClass_All"

    ctx = _ctx([dist])
    busy = SimpleNamespace(
        get_metric_statistics=lambda **kw: {"Datapoints": [{"Sum": 50_000.0}]}
    )
    clients = {"cloudfront": _FakeCloudFront([dist]), "cloudwatch": busy}
    ctx.client = lambda name, region=None: clients.get(name)

    findings = CloudfrontModule().scan(ctx)
    others = [
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] != _CATEGORY
    ]
    assert others, "expected the price-class advisory to be present"
    assert all(r["Counted"] is False and r["EstimatedMonthlySavings"] == 0.0 for r in others)
    assert findings.total_monthly_savings == 0.0
