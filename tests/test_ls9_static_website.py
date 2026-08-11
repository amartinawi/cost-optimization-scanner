"""LS-9 — "static website" is not an optimization class, and it hid a real dollar.

Filed as a LOW: S3 emitted *"Static website detected: Enable CloudFront CDN for
reduced data transfer costs and improved performance"*, `$0.00/month - data
transfer dependent (CloudFront CDN)` — no dollar, and half its justification
("improved performance") is outside the strictly-cost scope.

Scouting it turned up something worse. Website hosting was treated as an
*optimization class*, short-circuiting two different gap checks:

* `_classify_opportunities` returned ``static_website`` BEFORE testing lifecycle
  and tiering, and ``static_website`` is absent from `_GAP_OPPORTUNITY_CLASSES`.
  So a static-website bucket with no lifecycle policy could never set
  ``has_gap`` and was **excluded from the evidence-gated counted saving** — even
  when it had Standard bytes, request metrics proved them cold, and the average
  object cleared the 128 KiB IA minimum. A false negative on a COUNTED dollar.
* `reporter_phase_b` grouped on ``IsStaticWebsite`` first, so the same bucket
  landed under "Static Website Optimization" and was shown the CloudFront nudge
  instead of the lifecycle/tiering recommendation that actually applies.

The two properties are independent: hosting a website says nothing about whether
cold Standard bytes would be cheaper in Standard-IA. The saving was already
gated on the real evidence (0 GETs over 30d, >=128 KiB average object,
StandardGB > 0), and a bucket with 0 GETs in 30 days is not serving anything.

Why the CloudFront card cannot be repaired the way LS-3/LS-4 were — both halves
of the delta are unobtainable (live-probed 2026-08-11):

* **Quantity.** S3 publishes no egress metric. `BytesDownloaded` is an opt-in
  PAID request metric, and it cannot separate internet egress (billed) from
  same-region egress to EC2 (free) or from CloudFront's own origin pulls.
* **Rate.** The AWS Pricing API's only ``AWS Outbound`` -> ``External`` SKU from
  us-east-1 prices at **$0.00/GB over 0-Inf**, so the S3 side of the delta has
  no usable retail rate at all; CloudFront's outbound rates run $0.06-$0.08+/GB
  depending on the edge-location group serving the request, which cannot be
  known in advance. CloudFront also adds per-request charges that can exceed the
  transfer saving for small objects, so even the SIGN is not determined.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.s3 import (
    _GAP_OPPORTUNITY_CLASSES,
    _classify_opportunities,
    _finalize_bucket_savings,
)


# --------------------------------------------------------------------------- #
# The classifier describes the GAP, not the workload
# --------------------------------------------------------------------------- #
def test_a_static_website_with_both_gaps_is_classified_by_its_gaps() -> None:
    """The defect in one line: this returned "static_website", which is not a
    gap class, so the bucket could never earn the counted IA-delta saving."""
    bucket = {"HasLifecyclePolicy": False, "HasIntelligentTiering": False, "IsStaticWebsite": True}
    assert _classify_opportunities(bucket) == "both_missing"
    assert _classify_opportunities(bucket) in _GAP_OPPORTUNITY_CLASSES


def test_website_hosting_never_changes_the_class() -> None:
    """Hosting a website says nothing about whether cold Standard bytes would be
    cheaper in Standard-IA, so the flag must not move the classification."""
    for lifecycle in (True, False):
        for tiering in (True, False):
            base = {"HasLifecyclePolicy": lifecycle, "HasIntelligentTiering": tiering}
            assert _classify_opportunities({**base, "IsStaticWebsite": True}) == \
                   _classify_opportunities({**base, "IsStaticWebsite": False})


def test_the_existing_lifecycle_exclusion_is_untouched() -> None:
    """Regression guard: S3-N4 excludes buckets that already have a lifecycle
    policy, because an existing rule may already perform the transition."""
    assert "intelligent_tiering" not in _GAP_OPPORTUNITY_CLASSES
    assert _classify_opportunities(
        {"HasLifecyclePolicy": True, "HasIntelligentTiering": False, "IsStaticWebsite": True}
    ) == "intelligent_tiering"


def test_static_website_is_no_longer_a_class_at_all() -> None:
    assert "static_website" not in _GAP_OPPORTUNITY_CLASSES
    for lifecycle in (True, False):
        for tiering in (True, False):
            assert _classify_opportunities({
                "HasLifecyclePolicy": lifecycle,
                "HasIntelligentTiering": tiering,
                "IsStaticWebsite": True,
            }) != "static_website"


# --------------------------------------------------------------------------- #
# The $0 string
# --------------------------------------------------------------------------- #
def test_a_gapped_bucket_gets_the_gap_advisory_not_the_cloudfront_nudge() -> None:
    bucket: dict[str, Any] = {"IsStaticWebsite": True}
    _finalize_bucket_savings(bucket, 0.0, "both_missing", True, 120.0, False)
    assert "CloudFront" not in bucket["EstimatedSavings"]
    assert bucket["Counted"] is False
    assert bucket["Advisory"] is True


def test_a_counted_saving_survives_on_a_static_website_bucket() -> None:
    bucket: dict[str, Any] = {"IsStaticWebsite": True}
    _finalize_bucket_savings(bucket, 42.5, "both_missing", True, 120.0, False)
    assert bucket["Counted"] is True
    assert bucket["EstimatedMonthlySavings"] == 42.5
    assert "CloudFront" not in bucket["EstimatedSavings"]


# --------------------------------------------------------------------------- #
# Emission + the API call that fed it
# --------------------------------------------------------------------------- #
class _S3:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def list_buckets(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {"Buckets": [{"Name": "site", "CreationDate": datetime(2020, 1, 1, tzinfo=UTC)}]}

    def get_bucket_location(self, **_kw: Any) -> dict[str, Any]:
        return {"LocationConstraint": None}

    def get_bucket_website(self, **_kw: Any) -> dict[str, Any]:
        self.calls.append("get_bucket_website")
        return {"IndexDocument": {"Suffix": "index.html"}}

    def get_bucket_lifecycle_configuration(self, **_kw: Any) -> dict[str, Any]:
        raise Exception("NoSuchLifecycleConfiguration")

    def __getattr__(self, name: str) -> Any:
        def _stub(**_kw: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {}

        return _stub


def _ctx(s3: _S3) -> SimpleNamespace:
    """Context whose per-bucket client resolves to the same fake.

    ``ctx.clients._factory`` is required, not decorative: `_bucket_s3_client`
    reaches through it for a region-scoped client, and without it the
    AttributeError escapes to the function's outer handler and every result is
    swallowed. A test that only asserts a card is ABSENT then passes because
    nothing ran at all.
    """
    ctx = SimpleNamespace(pricing_engine=None, pricing_multiplier=1.0, fast_mode=False,
                          region="us-east-1", warnings=[], permissions=[])
    ctx.warn = lambda msg, service="", **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service="", action=None, **k: ctx.permissions.append(msg)
    ctx.client = lambda name, region=None: s3
    ctx.clients = SimpleNamespace(
        _factory=SimpleNamespace(
            session=lambda: SimpleNamespace(client=lambda *a, **k: s3)
        )
    )
    return ctx


@pytest.fixture(autouse=True)
def _clear_region_caches() -> Any:
    """`_LIVE_S3_REGIONS` / `_DEAD_S3_REGIONS` are module-level and leak between
    tests, so a region retired by one test would silently skip another's loop."""
    import services.s3 as s3_mod

    s3_mod._LIVE_S3_REGIONS.clear()
    s3_mod._DEAD_S3_REGIONS.clear()
    yield
    s3_mod._LIVE_S3_REGIONS.clear()
    s3_mod._DEAD_S3_REGIONS.clear()


def test_no_static_website_card_is_emitted() -> None:
    from services.s3 import get_enhanced_s3_checks

    calls: list[str] = []
    out = get_enhanced_s3_checks(_ctx(_S3(calls)), pricing_multiplier=1.0)

    assert "static_website_optimization" not in out
    for rec in out.get("recommendations", []):
        assert rec.get("CheckCategory") != "Static Website Optimization"
        assert "CloudFront" not in str(rec.get("Recommendation", ""))


def test_the_get_bucket_website_call_is_gone() -> None:
    """Nothing consumes the flag for a cost decision any more, so the per-bucket
    call is pure waste — the same cleanup LS-4 and LS-5 made."""
    from services.s3 import get_enhanced_s3_checks

    calls: list[str] = []
    get_enhanced_s3_checks(_ctx(_S3(calls)), pricing_multiplier=1.0)
    assert "get_bucket_website" not in calls


def test_the_lifecycle_card_still_renders_with_one_recommendation() -> None:
    """Removing the website branch must not take the lifecycle gap card with
    it — that card is the visibility flag for a real transition gap."""
    from services.s3 import get_enhanced_s3_checks

    out = get_enhanced_s3_checks(_ctx(_S3([])), pricing_multiplier=1.0)
    lifecycle = [r for r in out.get("recommendations", [])
                 if r.get("CheckCategory") == "Storage Class Optimization"]
    assert len(lifecycle) == 1
    assert "lifecycle" in lifecycle[0]["Recommendation"].lower()


# --------------------------------------------------------------------------- #
# Render side — the same short-circuit lived in the reporter
# --------------------------------------------------------------------------- #
def _s3_rec(**over: Any) -> dict[str, Any]:
    base = {
        "BucketName": "site", "SizeGB": 500.0, "EstimatedMonthlyCost": 11.5,
        "HasLifecyclePolicy": False, "HasIntelligentTiering": False,
        "IsStaticWebsite": True, "StorageClass": "STANDARD",
    }
    base.update(over)
    return base


def test_a_static_website_bucket_groups_by_its_gaps() -> None:
    from reporter_phase_b import _render_s3_bucket_analysis

    out = _render_s3_bucket_analysis([_s3_rec()], "s3_bucket_analysis", {})
    assert "Both Missing" in out
    assert "Static Website Optimization" not in out
    assert "CloudFront" not in out


def test_the_other_render_groups_are_unchanged() -> None:
    from reporter_phase_b import _render_s3_bucket_analysis

    out = _render_s3_bucket_analysis(
        [_s3_rec(HasLifecyclePolicy=False, HasIntelligentTiering=True, IsStaticWebsite=False)],
        "s3_bucket_analysis", {},
    )
    assert "No Lifecycle Policy" in out


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
