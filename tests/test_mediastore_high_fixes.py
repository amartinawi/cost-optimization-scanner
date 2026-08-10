"""MediaStore — the counted path is gone (MS-1/MS-3/MS-4).

The adapter used to price `EstimatedStorageGB` at an S3 rate. That field came
from an `AWS/MediaStore BucketSizeBytes` read — a metric MediaStore does not
publish (that is the S3 name) — so it was always 0 and **the counted branch was
unreachable on every account since it was written**. The tests below used to
pass only because their fakes supplied the field directly, which is the
"fixture models an impossible shape" trap this repo has hit repeatedly.

The branch is deleted rather than repaired: MediaStore exposes no storage-size
metric and `Container` carries no size field, so the QUANTITY half of
rate x quantity is unobtainable at scan time, and a rate with no quantity is not
a saving. Deleting it removes MS-3 (engine-priced string vs fallback-priced
numeric) and MS-4 (`Datapoints[-1]`, an ordering CloudWatch does not guarantee)
by construction.

No replacement advisory is emitted: this project's scope is strictly cost and
every rec must carry a concrete account-specific dollar. An EOL/migration notice
carries none — the same reasoning that deleted the OpenSearch version-upgrade
nudges. This tab's `total_monthly_savings` is now a structural 0.0.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.mediastore as mediastore_adapter

S3_STANDARD_RATE = 0.023  # $/GB-Mo, us-east-1, validated live (see module docstring)


class _FakePricing:
    """Returns a fixed region-correct S3 STANDARD $/GB-Mo."""

    def __init__(self, rate: float = S3_STANDARD_RATE) -> None:
        self._rate = rate

    def get_s3_monthly_price_per_gb(self, storage_class: str) -> float:
        assert storage_class == "STANDARD"
        return self._rate


def _ctx(*, pricing_engine=_FakePricing(), pricing_multiplier: float = 1.0, client=None) -> SimpleNamespace:
    return SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=pricing_multiplier,
        region="us-east-1",
        account_id="123456789012",
        fast_mode=False,
        warnings=[],
        permission_issues=[],
        client=client or (lambda name, region=None: None),
        warn=lambda message, service=None: None,
        permission_issue=lambda message, service=None, action=None: None,
    )


def _patch_checks(monkeypatch, recs):
    monkeypatch.setattr(
        mediastore_adapter, "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )


# --------------------------------------------------------------------------- #
# Pure logic — counted dollar
# --------------------------------------------------------------------------- #
def test_storage_backed_rec_is_no_longer_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a rec carrying EstimatedStorageGB counts nothing now. Nothing in
    production ever produced that field, and the rate it was priced at was
    borrowed from S3."""
    _patch_checks(monkeypatch, [{"ContainerName": "c1", "EstimatedStorageGB": 100.0,
                                 "CheckCategory": "Unused Resource Cleanup"}])
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())

    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0


def test_tab_total_is_structurally_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_checks(monkeypatch, [
        {"ContainerName": "a", "EstimatedStorageGB": 10.0},
        {"ContainerName": "b"},
        {"ContainerName": "c", "EstimatedStorageGB": 0},
    ])
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())
    assert findings.total_monthly_savings == 0.0
    assert all(r["Counted"] is False for r in findings.sources["enhanced_checks"].recommendations)


def test_no_pricing_lookup_is_performed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter must not consult the pricing engine at all — there is no
    quantity to price."""
    calls: list[str] = []
    engine = SimpleNamespace(
        get_s3_monthly_price_per_gb=lambda *a, **k: calls.append("s3") or 0.023
    )
    _patch_checks(monkeypatch, [{"ContainerName": "c1", "EstimatedStorageGB": 100.0}])
    mediastore_adapter.MediastoreModule().scan(_ctx(pricing_engine=engine))
    assert calls == []


def test_scan_does_not_mutate_source_recs(monkeypatch: pytest.MonkeyPatch) -> None:
    source = {"ContainerName": "z", "EstimatedStorageGB": 0, "CheckCategory": "Unused Resource Cleanup"}
    monkeypatch.setattr(
        mediastore_adapter,
        "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [source]},
    )
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())
    # The rendered rec is advisory, but the ORIGINAL dict the shim handed over
    # must be unchanged (no Counted / EstimatedMonthlySavings keys injected).
    assert "Counted" not in source
    assert "EstimatedMonthlySavings" not in source
    rendered = findings.sources["enhanced_checks"].recommendations[0]
    assert rendered.get("Counted") is False


# --------------------------------------------------------------------------- #
# scan() path through the REAL shim with fake boto3 clients
# --------------------------------------------------------------------------- #
class _FakePaginator:
    """Minimal boto3-style paginator yielding one page dict per page."""

    def __init__(self, pages):
        self._pages = pages

    def paginate(self):
        for page in self._pages:
            yield {"Containers": page}


class _FakeMediaStore:
    def __init__(self, containers, *, pages=None):
        # `pages` spans multiple paginator pages; default is a single page so
        # existing single-page call sites keep working (mediastore L1).
        self._pages = pages if pages is not None else [containers]

    def get_paginator(self, operation_name):
        assert operation_name == "list_containers"
        return _FakePaginator(self._pages)


class _FakeCloudWatch:
    """Returns metric Datapoints keyed by MetricName.

    ``size_average_gb`` drives BucketSizeBytes; activity metrics return a single
    Sum=0 datapoint so the container is a *confirmed*-idle "unused" candidate
    (seen>0 AND total==0), the only path that emits a recommendation.
    """

    def __init__(self, *, size_average_gb: float | None):
        self._size_average_gb = size_average_gb

    def get_metric_statistics(self, **kwargs):
        name = kwargs["MetricName"]
        if name in ("RequestCount", "BytesDownloaded", "BytesUploaded"):
            # Confirmed-idle: a real datapoint that sums to zero activity.
            return {"Datapoints": [{"Sum": 0.0}]}
        if name == "BucketSizeBytes":
            if self._size_average_gb is None:
                return {"Datapoints": []}
            return {"Datapoints": [{"Average": self._size_average_gb * (1024**3)}]}
        return {"Datapoints": []}


def _client_factory(mediastore_client, cloudwatch_client):
    def factory(name, region=None):
        if name == "mediastore":
            return mediastore_client
        if name == "cloudwatch":
            return cloudwatch_client
        raise AssertionError(f"unexpected client {name}")

    return factory


def test_scan_path_emits_an_uncounted_advisory() -> None:
    ms = _FakeMediaStore([{"Name": "live", "Status": "ACTIVE"}])
    cw = _FakeCloudWatch(size_average_gb=10.0)  # 10 GB stored
    ctx = _ctx(client=_client_factory(ms, cw))
    findings = mediastore_adapter.MediastoreModule().scan(ctx)
    # The fake CloudWatch answers BucketSizeBytes; the real AWS/MediaStore
    # namespace does not publish it, which is why this path counted nothing in
    # production and counts nothing here now.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["ContainerName"] == "live"
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "cannot be measured" in rec["EstimatedSavings"]


def test_scan_path_demotes_zero_storage_container() -> None:
    ms = _FakeMediaStore([{"Name": "empty", "Status": "ACTIVE"}])
    cw = _FakeCloudWatch(size_average_gb=None)  # no BucketSizeBytes datapoints -> 0 GB
    ctx = _ctx(client=_client_factory(ms, cw))
    findings = mediastore_adapter.MediastoreModule().scan(ctx)
    # A rec is still produced (confirmed idle) but with EstimatedStorageGB=0...
    assert findings.sources["enhanced_checks"].count == 1
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec.get("Counted") is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # ...and H1 keeps it out of the headline count and dollar total.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# mediastore L1: paginate list_containers — containers beyond page 1 are kept
# --------------------------------------------------------------------------- #
def test_scan_path_follows_pagination_across_pages() -> None:
    # Two paginator pages, each a distinct storage-backed confirmed-idle
    # container; before the fix only the first page was read, silently dropping
    # the second container's realizable saving.
    ms = _FakeMediaStore(
        [],
        pages=[
            [{"Name": "page1-c", "Status": "ACTIVE"}],
            [{"Name": "page2-c", "Status": "ACTIVE"}],
        ],
    )
    cw = _FakeCloudWatch(size_average_gb=10.0)  # 10 GB stored per container
    ctx = _ctx(client=_client_factory(ms, cw))
    findings = mediastore_adapter.MediastoreModule().scan(ctx)
    names = {r["ContainerName"] for r in findings.sources["enhanced_checks"].recommendations}
    assert names == {"page1-c", "page2-c"}  # second page is NOT dropped
    # MS-1: no counted dollar exists for MediaStore any more, on either page.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
