"""Unit tests for the S3 adapter and shim — savings factors + dedup + parseable enhanced checks."""

from __future__ import annotations

import pytest

from services._savings import parse_dollar_savings
from services.adapters.s3 import _DEDICATED_CATEGORIES, S3Module
from services.s3 import (
    S3_SAVINGS_FACTORS,
    _classify_opportunities,
    _is_access_denied,
)


class TestS3SavingsFactors:
    """Audit L2-S3-001 — replaces the legacy blanket × 0.40 multiplier."""

    def test_factor_dict_completeness(self):
        """All four opportunity classes plus the 'other' sentinel must exist."""
        assert set(S3_SAVINGS_FACTORS) == {
            "lifecycle_missing",
            "intelligent_tiering",
            "both_missing",
            "static_website",
            "other",
        }

    def test_factors_are_bounded(self):
        """No factor exceeds the legacy 0.40 cap (audit anchor)."""
        for key, value in S3_SAVINGS_FACTORS.items():
            assert 0.0 <= value <= 0.40, f"{key} = {value} out of bounds"

    def test_static_website_factor_is_zero(self):
        """Static-website CloudFront savings are data-transfer dependent — must not invent storage savings."""
        assert S3_SAVINGS_FACTORS["static_website"] == 0.0

    def test_both_missing_dominates(self):
        """Both-missing should be ≥ either single-gap class."""
        assert S3_SAVINGS_FACTORS["both_missing"] >= S3_SAVINGS_FACTORS["lifecycle_missing"]
        assert S3_SAVINGS_FACTORS["both_missing"] >= S3_SAVINGS_FACTORS["intelligent_tiering"]


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
        for category in [
            "Incomplete Multipart Uploads",
            "Versioning Optimization",
            "Replication Optimization",
            "Logging Optimization",
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
        # 1 from bucket_analysis + 2 non-dedicated enhanced = 3
        assert findings.total_recommendations == 3
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
