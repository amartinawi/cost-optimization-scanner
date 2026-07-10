"""Unit tests for the S3 adapter and shim — savings factors + dedup + parseable enhanced checks."""

from __future__ import annotations

import pytest

from services._savings import parse_dollar_savings
from services.adapters.s3 import _DEDICATED_CATEGORIES, S3Module
from services.s3 import (
    _GAP_OPPORTUNITY_CLASSES,
    _assess_bucket_coldness,
    _classify_opportunities,
    _cost_from_class_sizes,
    _is_access_denied,
    _s3_price_per_gb,
)


class TestPerClassCosting:
    """Audit S3-A — cost each storage class at its OWN rate, not all at Standard."""

    def test_price_per_gb_standard(self):
        # Fallback path (ctx=None) → module constant × us-east-1 multiplier (1.0).
        assert _s3_price_per_gb(None, "STANDARD", "us-east-1") == pytest.approx(0.023)

    def test_price_per_gb_deep_archive_not_rounded_to_zero(self):
        """Cheap classes must survive (no premature rounding)."""
        assert _s3_price_per_gb(None, "DEEP_ARCHIVE", "us-east-1") == pytest.approx(0.00099)

    def test_cost_sums_each_class_at_own_rate(self):
        """A mostly-Deep-Archive bucket must NOT be priced as if all Standard."""
        class_sizes = {"STANDARD": 100.0, "DEEP_ARCHIVE": 1000.0}
        cost = _cost_from_class_sizes(None, "us-east-1", class_sizes)
        # Correct: 100×0.023 + 1000×0.00099 = 2.30 + 0.99 = 3.29
        assert cost == pytest.approx(3.29)
        # Legacy STANDARD-only bug would have produced 1100×0.023 = 25.30.
        assert cost < 25.30

    def test_empty_classes_cost_zero(self):
        assert _cost_from_class_sizes(None, "us-east-1", {}) == 0.0


class TestEvidenceGatedSavingsClasses:
    """Audit S3-B — only transition-gap classes are savings-eligible."""

    def test_gap_classes_are_the_transitionable_ones(self):
        # S3-N4: a bucket that already has a lifecycle policy ("intelligent_tiering"
        # class) is excluded — its existing rule may already transition the bytes,
        # so crediting the full Standard->IA delta would overstate the saving.
        assert _GAP_OPPORTUNITY_CLASSES == {
            "both_missing",
            "lifecycle_missing",
        }

    def test_intelligent_tiering_is_not_a_gap_class(self):
        # S3-N4: existing-lifecycle buckets must not receive the IA-delta saving.
        assert "intelligent_tiering" not in _GAP_OPPORTUNITY_CLASSES

    def test_static_website_is_not_a_gap_class(self):
        assert "static_website" not in _GAP_OPPORTUNITY_CLASSES

    def test_other_is_not_a_gap_class(self):
        assert "other" not in _GAP_OPPORTUNITY_CLASSES


class TestColdnessAssessment:
    """Audit S3-B — coldness is read from request metrics, never assumed."""

    def _ctx(self):
        from unittest.mock import MagicMock
        return MagicMock()

    def test_no_metrics_config_is_unknown(self):
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {"MetricsConfigurationList": []}
        assert _assess_bucket_coldness(self._ctx(), "b", s3_client, "us-east-1") == "unknown"

    def test_access_denied_is_unknown(self):
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.side_effect = Exception("AccessDenied")
        assert _assess_bucket_coldness(self._ctx(), "b", s3_client, "us-east-1") == "unknown"

    def test_filtered_only_config_is_unknown(self):
        """A metrics config scoped to a prefix can't speak for the whole bucket."""
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {
            "MetricsConfigurationList": [{"Id": "prefixed", "Filter": {"Prefix": "logs/"}}]
        }
        assert _assess_bucket_coldness(self._ctx(), "b", s3_client, "us-east-1") == "unknown"

    def test_zero_get_requests_is_cold(self, monkeypatch):
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {
            "MetricsConfigurationList": [{"Id": "EntireBucket"}]
        }
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        monkeypatch.setattr("services.s3._bucket_cloudwatch_client", lambda *a, **k: cw)
        assert _assess_bucket_coldness(self._ctx(), "b", s3_client, "us-east-1") == "cold"

    def test_nonzero_get_requests_is_warm(self, monkeypatch):
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {
            "MetricsConfigurationList": [{"Id": "EntireBucket"}]
        }
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": [{"Sum": 4200.0}]}
        monkeypatch.setattr("services.s3._bucket_cloudwatch_client", lambda *a, **k: cw)
        assert _assess_bucket_coldness(self._ctx(), "b", s3_client, "us-east-1") == "warm"

    def test_getrequests_access_denied_records_permission_issue(self, monkeypatch):
        """S3-N1: a denied CloudWatch GetMetricStatistics is classified, not just debug-logged."""
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {
            "MetricsConfigurationList": [{"Id": "EntireBucket"}]
        }
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = Exception("AccessDenied")
        monkeypatch.setattr("services.s3._bucket_cloudwatch_client", lambda *a, **k: cw)
        ctx = self._ctx()
        assert _assess_bucket_coldness(ctx, "b", s3_client, "us-east-1") == "unknown"
        ctx.permission_issue.assert_called_once()
        assert ctx.permission_issue.call_args.kwargs["service"] == "cloudwatch"
        assert ctx.permission_issue.call_args.kwargs["action"] == "cloudwatch:GetMetricStatistics"
        ctx.warn.assert_not_called()

    def test_getrequests_throttling_records_warning(self, monkeypatch):
        """S3-N1: a throttled CloudWatch GetMetricStatistics is surfaced as a warning, not silently dropped."""
        from unittest.mock import MagicMock
        s3_client = MagicMock()
        s3_client.list_bucket_metrics_configurations.return_value = {
            "MetricsConfigurationList": [{"Id": "EntireBucket"}]
        }
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = Exception("ThrottlingException")
        monkeypatch.setattr("services.s3._bucket_cloudwatch_client", lambda *a, **k: cw)
        ctx = self._ctx()
        assert _assess_bucket_coldness(ctx, "b", s3_client, "us-east-1") == "unknown"
        ctx.warn.assert_called_once()
        ctx.permission_issue.assert_not_called()


class TestClassifyOpportunities:
    def test_static_website_takes_precedence(self):
        bucket = {"HasLifecyclePolicy": False, "HasIntelligentTiering": False, "IsStaticWebsite": True}
        assert _classify_opportunities(bucket) == "static_website"

    def test_both_missing(self):
        bucket = {"HasLifecyclePolicy": False, "HasIntelligentTiering": False, "IsStaticWebsite": False}
        assert _classify_opportunities(bucket) == "both_missing"

    def test_lifecycle_missing_only(self):
        bucket = {"HasLifecyclePolicy": False, "HasIntelligentTiering": True, "IsStaticWebsite": False}
        assert _classify_opportunities(bucket) == "lifecycle_missing"

    def test_intelligent_tiering_missing_only(self):
        bucket = {"HasLifecyclePolicy": True, "HasIntelligentTiering": False, "IsStaticWebsite": False}
        assert _classify_opportunities(bucket) == "intelligent_tiering"

    def test_fully_optimized(self):
        bucket = {"HasLifecyclePolicy": True, "HasIntelligentTiering": True, "IsStaticWebsite": False}
        assert _classify_opportunities(bucket) == "other"


class TestIsAccessDenied:
    def test_plain_access_denied_string(self):
        assert _is_access_denied(Exception("An error occurred (AccessDenied) when calling …"))

    def test_all_access_disabled(self):
        assert _is_access_denied(Exception("AllAccessDisabled: account disabled"))

    def test_forbidden(self):
        assert _is_access_denied(Exception("Forbidden"))

    def test_non_permission_error(self):
        assert not _is_access_denied(Exception("NoSuchBucket: missing"))

    def test_client_error_with_code(self):
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetBucketLifecycleConfiguration",
        )
        assert _is_access_denied(err)


class TestDedicatedCategoriesDedup:
    """Audit L2-S3-002 — enhanced_checks recs that overlap with bucket_analysis are filtered."""

    def test_dedicated_set_contents(self):
        assert "Storage Class Optimization" in _DEDICATED_CATEGORIES
        assert "Static Website Optimization" in _DEDICATED_CATEGORIES

    def test_non_dedicated_categories_pass_through(self):
        # Replication/Logging "Optimization" were removed entirely (out-of-scope
        # best-practice nudges with no concrete saving); the remaining enhanced
        # categories are cost-relevant and pass through the dedup filter.
        for category in [
            "Incomplete Multipart Uploads",
            "Versioning Optimization",
            "Unused Resources",
        ]:
            assert category not in _DEDICATED_CATEGORIES


class TestS3ModuleMetadata:
    """Audit L1-S3-003 — module flags honest."""

    def test_requires_cloudwatch_true(self):
        assert S3Module().requires_cloudwatch is True

    def test_reads_fast_mode_true(self):
        assert S3Module().reads_fast_mode is True

    def test_required_clients_includes_cloudwatch(self):
        assert "cloudwatch" in S3Module().required_clients()
        assert "s3" in S3Module().required_clients()


class TestAdapterSavingsAggregation:
    """End-to-end: adapter sums SavingsDelta + parses enhanced EstimatedSavings.

    Confirms that the legacy `EstimatedMonthlyCost × 0.40` fallback path is
    gone and that the informational `$0.00/month - …` strings parse to zero
    (audit L2-S3-001 + L2-S3-002).
    """

    def _ctx(self):
        from unittest.mock import MagicMock
        ctx = MagicMock()
        ctx.fast_mode = False
        ctx.pricing_multiplier = 1.0
        return ctx

    def test_adapter_filters_dedicated_categories(self, monkeypatch):
        """Storage Class Optimization + Static Website Optimization are filtered out of enhanced_recs."""
        bucket_analysis_result = {
            "total_buckets": 2,
            "optimization_opportunities": [
                {
                    "Name": "bucket-a",
                    "EstimatedMonthlyCost": 100.0,
                    "SavingsDelta": 30.0,
                    "EstimatedSavings": "$30.00/month",
                    "OpportunityClass": "lifecycle_missing",
                    "HasLifecyclePolicy": False,
                    "HasIntelligentTiering": True,
                    "IsStaticWebsite": False,
                    "SizeGB": 1000.0,
                    "OptimizationOpportunities": ["Configure lifecycle policies"],
                },
            ],
            "buckets_without_lifecycle": ["bucket-a"],
            "buckets_without_intelligent_tiering": [],
            "top_cost_buckets": [],
            "top_size_buckets": [],
        }
        enhanced_result = {
            "recommendations": [
                # Dedicated category — must be filtered out by adapter.
                {"BucketName": "bucket-a", "CheckCategory": "Storage Class Optimization",
                 "EstimatedSavings": "$0.00/month - covered by S3 Bucket Analysis source"},
                # Non-dedicated — must be counted.
                {"BucketName": "bucket-a", "CheckCategory": "Incomplete Multipart Uploads",
                 "EstimatedSavings": "$0.00/month - quantify via S3 Storage Lens"},
                {"BucketName": "bucket-b", "CheckCategory": "Versioning Optimization",
                 "EstimatedSavings": "$0.00/month - quantify via S3 Storage Lens"},
            ],
        }
        monkeypatch.setattr(
            "services.adapters.s3.get_s3_bucket_analysis",
            lambda *_a, **_k: bucket_analysis_result,
        )
        monkeypatch.setattr(
            "services.adapters.s3.get_enhanced_s3_checks",
            lambda *_a, **_k: enhanced_result,
        )
        findings = S3Module().scan(self._ctx())
        # Count hygiene (audit S3-C): only the $30 savings-bearing bucket counts;
        # the two $0 informational enhanced checks are advisory, not counted.
        assert findings.total_recommendations == 1
        assert findings.sources["enhanced_checks"].count == 2  # still rendered
        assert findings.extras["advisory_count"] == 2
        # bucket_analysis contributes $30; enhanced informational both parse to 0
        assert findings.total_monthly_savings == pytest.approx(30.0)

    def test_no_arbitrary_40_percent_path(self, monkeypatch):
        """Confirms adapter does NOT default to EstimatedMonthlyCost × 0.40 when SavingsDelta is absent."""
        bucket_analysis_result = {
            "total_buckets": 1,
            "optimization_opportunities": [
                # No SavingsDelta key: adapter must treat as 0.0, not cost × 0.40.
                {
                    "Name": "bucket-x",
                    "EstimatedMonthlyCost": 1000.0,
                    "HasLifecyclePolicy": True,
                    "HasIntelligentTiering": True,
                    "IsStaticWebsite": False,
                    "SizeGB": 100.0,
                },
            ],
            "buckets_without_lifecycle": [],
            "buckets_without_intelligent_tiering": [],
            "top_cost_buckets": [],
            "top_size_buckets": [],
        }
        monkeypatch.setattr(
            "services.adapters.s3.get_s3_bucket_analysis",
            lambda *_a, **_k: bucket_analysis_result,
        )
        monkeypatch.setattr(
            "services.adapters.s3.get_enhanced_s3_checks",
            lambda *_a, **_k: {"recommendations": []},
        )
        findings = S3Module().scan(self._ctx())
        # If the bug were back: 1000 × 0.40 = 400. With the fix: 0.0.
        assert findings.total_monthly_savings == 0.0
        # And a $0 bucket is not a counted recommendation (audit S3-C).
        assert findings.total_recommendations == 0

    def test_small_advisory_buckets_suppressed_from_render(self, monkeypatch):
        """F2 — sub-1GB $0 advisory buckets are dropped from the rendered cards
        (kept in the suppressed tally); counted buckets and >=1GB advisory buckets
        always render."""
        bucket_analysis_result = {
            "total_buckets": 3,
            "optimization_opportunities": [
                # counted, tiny -> still rendered (a real saving beats the size floor)
                {"Name": "counted-small", "SavingsDelta": 12.0, "EstimatedSavings": "$12.00/month",
                 "SizeGB": 0.004, "OpportunityClass": "lifecycle_missing"},
                # advisory, >= 1GB -> rendered
                {"Name": "big-advisory", "SavingsDelta": 0.0,
                 "EstimatedSavings": "$0.00/month - no evidence", "SizeGB": 820.0,
                 "OpportunityClass": "both_missing"},
                # advisory, < 1GB -> suppressed
                {"Name": "tiny-advisory", "SavingsDelta": 0.0,
                 "EstimatedSavings": "$0.00/month - no evidence", "SizeGB": 0.004,
                 "OpportunityClass": "both_missing"},
            ],
            "buckets_without_lifecycle": [], "buckets_without_intelligent_tiering": [],
            "top_cost_buckets": [], "top_size_buckets": [],
        }
        monkeypatch.setattr(
            "services.adapters.s3.get_s3_bucket_analysis", lambda *_a, **_k: bucket_analysis_result
        )
        monkeypatch.setattr(
            "services.adapters.s3.get_enhanced_s3_checks", lambda *_a, **_k: {"recommendations": []}
        )
        findings = S3Module().scan(self._ctx())

        rendered = findings.sources["s3_bucket_analysis"].recommendations
        names = {r["Name"] for r in rendered}
        assert names == {"counted-small", "big-advisory"}
        assert "tiny-advisory" not in names
        assert findings.extras["suppressed_small_advisory_buckets"] == 1
        # Render suppression must not change the counted dollar or the count.
        assert findings.total_monthly_savings == pytest.approx(12.0)
        assert findings.total_recommendations == 1


class TestEnhancedSavingsStringsParse:
    """Audit L2-S3-002 — every enhanced-check savings string is parse-safe."""

    @pytest.mark.parametrize(
        "savings_str,expected",
        [
            ("$0.00/month", 0.0),
            ("$0.00/month - covered by S3 Bucket Analysis source", 0.0),
            ("$0.00/month - quantify via S3 Storage Lens (incomplete-upload bytes)", 0.0),
            ("$0.00/month - data transfer dependent (CloudFront CDN)", 0.0),
            ("$25.50/month", 25.50),
        ],
    )
    def test_parse_dollar_savings_handles_all_enhanced_strings(self, savings_str, expected):
        assert parse_dollar_savings(savings_str) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# afs-prod live incident (2026-07-10): an unreachable bucket region hung the scan
#
# me-south-1 was ENABLED and opted-in, but its endpoint was unroutable from the
# scanning host. Per-bucket S3 calls used the shared session config (10 adaptive
# retries x 60s default connect timeout ~= 10 min/call); with 68 buckets there the
# scan produced no report after 81 minutes.
# --------------------------------------------------------------------------- #
from types import SimpleNamespace

import services.s3 as _s3mod
from botocore.exceptions import ClientError as _ClientError
from botocore.exceptions import ConnectTimeoutError as _ConnectTimeout


def _reset_region_caches():
    _s3mod._DEAD_S3_REGIONS.clear()
    _s3mod._LIVE_S3_REGIONS.clear()


class _DeadEndpoint:
    def __init__(self):
        self.calls = 0

    def head_bucket(self, Bucket):  # noqa: N803
        self.calls += 1
        raise _ConnectTimeout(endpoint_url="https://b.s3.me-south-1.amazonaws.com/")


class _Answers403:
    def __init__(self):
        self.calls = 0

    def head_bucket(self, Bucket):  # noqa: N803
        self.calls += 1
        raise _ClientError({"Error": {"Code": "403"}}, "HeadBucket")


def _warn_ctx():
    warns = []
    return SimpleNamespace(warn=lambda m, service=None: warns.append(m)), warns


def test_unreachable_region_is_probed_once_then_cached():
    _reset_region_caches()
    ctx, warns = _warn_ctx()
    client = _DeadEndpoint()
    assert _s3mod._s3_region_reachable(ctx, client, "me-south-1", "b1") is False
    # Every subsequent bucket in that region short-circuits: no second probe, and
    # crucially no per-bucket S3 call that would each burn the retry budget.
    for i in range(2, 70):
        assert _s3mod._s3_region_reachable(ctx, client, "me-south-1", f"b{i}") is False
    assert client.calls == 1, "probed once per region, not once per bucket"
    assert len(warns) == 1, "one warning per dead region"
    assert "skipping all remaining buckets" in warns[0]


def test_endpoint_that_answers_403_is_reachable():
    # An auth/existence error proves the endpoint responded — only connect/read
    # timeouts retire a region. Treating 403 as dead would silently drop buckets.
    _reset_region_caches()
    ctx, warns = _warn_ctx()
    client = _Answers403()
    assert _s3mod._s3_region_reachable(ctx, client, "eu-west-1", "b1") is True
    assert _s3mod._s3_region_reachable(ctx, None, "eu-west-1", "b2") is True  # cached
    assert client.calls == 1
    assert warns == []


def _total_attempts(cfg):
    """Total attempts a Config permits, normalising botocore's two spellings.

    botocore rewrites `max_attempts: N` -> `total_max_attempts: N + 1` in place the
    first time a client is built from a Config. These Configs are module-level, so
    the surviving key depends on test order — normalise instead of guessing.
    """
    r = cfg.retries or {}
    if "total_max_attempts" in r:
        return r["total_max_attempts"]
    return r["max_attempts"] + 1


def test_bucket_s3_client_uses_bounded_timeouts_not_the_shared_config():
    cfg = _s3mod._BUCKET_S3_TIMEOUT_CONFIG
    assert cfg.connect_timeout == 5
    assert cfg.read_timeout == 10
    # Worst case per call: 3 attempts x 5s connect = 15s, not 10 x 60s = 10 min.
    assert _total_attempts(cfg) == 3
    assert cfg.connect_timeout * _total_attempts(cfg) <= 20


def test_shared_session_config_bounds_the_connect_phase():
    from core.session import AwsSessionFactory

    cfg = AwsSessionFactory(region="eu-west-1")._retry_config
    assert cfg.connect_timeout == 10, "an unbounded connect timeout can stall a scan for minutes"
    assert cfg.read_timeout == 60
    assert _total_attempts(cfg) == 11  # adaptive retries still absorb throttling


def test_connection_closed_counts_as_unreachable():
    """afs-prod's me-central-1 buckets raised ConnectionClosedError, not a timeout.

    The classifier missed it, so UAE was never retired and every bucket there paid
    the retry budget again.
    """
    from botocore.exceptions import ConnectionClosedError

    exc = ConnectionClosedError(endpoint_url="https://b.s3.me-central-1.amazonaws.com/?website")
    assert _s3mod._is_endpoint_unreachable(exc) is True
    # An auth/existence failure is NOT an unreachable endpoint.
    assert _s3mod._is_endpoint_unreachable(_ClientError({"Error": {"Code": "403"}}, "HeadBucket")) is False
