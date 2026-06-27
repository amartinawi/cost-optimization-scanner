"""HIGH cost-correctness fixes for the MediaStore adapter (mediastore H1).

mediastore H1 — count hygiene: a `$0` no-storage "recommendation"
(`EstimatedStorageGB <= 0`) was counted in `total_recommendations`, inflating
the rec headline with no realizable dollar. The fix demotes it to a
`Counted=False` advisory and counts only `Counted is not False` recs.

Two drive paths are exercised:
  * pure logic — `scan()` with `get_enhanced_mediastore_checks` monkeypatched to
    return hand-built recs, proving each counted dollar / advisory $0 explicitly;
  * the real `scan()` -> `get_enhanced_mediastore_checks` shim driven end-to-end
    with fake boto3 `mediastore` + `cloudwatch` clients, proving the count
    hygiene survives the actual rec-building path.

Validated rate (live AWS Pricing API, us-east-1, 2026-06-27): S3 STANDARD
`TimedStorage-ByteHrs` = $0.023/GB-Mo (first 50 TB tier, SKU WP9ANXZGBYYSGJEA,
volumeType=Standard / storageClass=General Purpose / unit GB-Mo). This matches
the adapter's fallback constant and the per-GB rate used for the counted dollar.
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


# --------------------------------------------------------------------------- #
# Pure logic — counted dollar
# --------------------------------------------------------------------------- #
def test_counted_rec_prices_storage_at_s3_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "ContainerName": "media-c1",
            "EstimatedStorageGB": 100.0,
            "CheckCategory": "Unused Resource Cleanup",
        }
    ]
    monkeypatch.setattr(
        mediastore_adapter,
        "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())
    expected = round(100.0 * S3_STANDARD_RATE, 2)  # $2.30
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    assert findings.total_recommendations == 1  # the real dollar IS counted
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected, abs=0.001)
    assert rec.get("Counted") is not False
    # Counted dollar carries a defensible AuditBasis (rate/region/window/formula).
    basis = rec["AuditBasis"]
    assert basis["region"] == "us-east-1"
    assert "GB-Mo" in basis["rate"]
    assert "/mo" in basis["formula"]


# --------------------------------------------------------------------------- #
# Pure logic — mediastore H1: no-storage rec is advisory, NOT counted
# --------------------------------------------------------------------------- #
def test_no_storage_rec_is_advisory_zero_and_uncounted(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "ContainerName": "media-empty",
            "EstimatedStorageGB": 0,
            "CheckCategory": "Unused Resource Cleanup",
        }
    ]
    monkeypatch.setattr(
        mediastore_adapter,
        "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec.get("Counted") is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    # H1 count hygiene: the $0 advisory renders but is excluded from the headline.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    # ...yet it is still present in the source block (rendered, not counted).
    assert findings.sources["enhanced_checks"].count == 1


# --------------------------------------------------------------------------- #
# Pure logic — mixed: only the storage-backed rec feeds the headline count
# --------------------------------------------------------------------------- #
def test_mixed_recs_count_only_the_realizable_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {"ContainerName": "has-gb", "EstimatedStorageGB": 50.0, "CheckCategory": "Unused Resource Cleanup"},
        {"ContainerName": "no-gb", "EstimatedStorageGB": 0, "CheckCategory": "Unused Resource Cleanup"},
    ]
    monkeypatch.setattr(
        mediastore_adapter,
        "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = mediastore_adapter.MediastoreModule().scan(_ctx())
    expected = round(50.0 * S3_STANDARD_RATE, 2)  # $1.15
    assert findings.total_recommendations == 1  # only the storage-backed rec
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    # counted == rendered: headline dollar equals the one counted card's dollar.
    counted = [r for r in findings.sources["enhanced_checks"].recommendations if r.get("Counted") is not False]
    assert sum(r["EstimatedMonthlySavings"] for r in counted) == pytest.approx(
        findings.total_monthly_savings, abs=0.001
    )


# --------------------------------------------------------------------------- #
# Pure logic — fallback path (no pricing_engine) applies the region multiplier
# --------------------------------------------------------------------------- #
def test_fallback_path_applies_pricing_multiplier(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [{"ContainerName": "c", "EstimatedStorageGB": 100.0, "CheckCategory": "Unused Resource Cleanup"}]
    monkeypatch.setattr(
        mediastore_adapter,
        "get_enhanced_mediastore_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = mediastore_adapter.MediastoreModule().scan(_ctx(pricing_engine=None, pricing_multiplier=1.25))
    expected = round(100.0 * (S3_STANDARD_RATE * 1.25), 2)
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    assert findings.total_recommendations == 1
    # immutability: the shim's source rec list is untouched by scan().
    assert "Counted" not in recs[0]


# --------------------------------------------------------------------------- #
# Pure logic — immutability: scan() must not mutate the shim's rec dicts
# --------------------------------------------------------------------------- #
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
class _FakeMediaStore:
    def __init__(self, containers):
        self._containers = containers

    def list_containers(self):
        return {"Containers": self._containers}


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


def test_scan_path_counts_storage_backed_container() -> None:
    ms = _FakeMediaStore([{"Name": "live", "Status": "ACTIVE"}])
    cw = _FakeCloudWatch(size_average_gb=10.0)  # 10 GB stored
    ctx = _ctx(client=_client_factory(ms, cw))
    findings = mediastore_adapter.MediastoreModule().scan(ctx)
    expected = round(10.0 * S3_STANDARD_RATE, 2)  # $0.23
    assert findings.total_recommendations == 1
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["ContainerName"] == "live"
    assert rec.get("Counted") is not False
    assert "AuditBasis" in rec


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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
