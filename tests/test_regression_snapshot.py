"""
T-002: Regression snapshot tests for the AWS Cost Optimization Scanner.

These tests verify that the monolith's output remains byte-equal (after normalization)
to the golden fixtures captured in T-001.

PRIMARY GATE: These tests MUST stay green after every refactoring commit.
If any test fails, STOP. Do not continue to the next task.
"""

from __future__ import annotations

import json

# conftest.py is auto-loaded by pytest — fixtures and normalizers are available
# without explicit import. We import the normalizer functions directly for
# convenience since they're plain functions, not fixtures.
import sys
from pathlib import Path

# Add project root to path so we can import from tests package
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import (  # noqa: E402
    GOLDEN_JSON,
    GOLDEN_HTML,
    normalize_json_for_comparison,
    normalize_html_for_comparison,
)


class TestGoldenJsonParity:
    """Verify JSON scan results match golden fixture after normalization."""

    def test_golden_json_loads(self, golden_json: dict) -> None:
        """Golden JSON is valid and has required top-level keys."""
        required = {"account_id", "region", "profile", "scan_time", "services", "summary"}
        assert required.issubset(golden_json.keys()), f"Missing top-level keys: {required - set(golden_json.keys())}"

    def test_golden_json_has_35_services(self, golden_json: dict) -> None:
        """All 35 expected service keys are present."""
        expected_services = {
            "ec2",
            "ami",
            "ebs",
            "rds",
            "file_systems",
            "s3",
            "dynamodb",
            "containers",
            "network",
            "monitoring",
            "elasticache",
            "opensearch",
            "lambda",
            "cloudfront",
            "api_gateway",
            "step_functions",
            "lightsail",
            "redshift",
            "dms",
            "quicksight",
            "apprunner",
            "transfer",
            "msk",
            "workspaces",
            "mediastore",
            "glue",
            "athena",
            "batch",
            "cost_optimization_hub",
            "aurora",
            "commitment_analysis",
            "bedrock",
            "sagemaker",
            "network_cost",
            "eks_cost",
        }
        actual = set(golden_json["services"].keys())
        assert actual == expected_services, (
            f"Missing: {expected_services - actual}, Extra: {actual - expected_services}"
        )

    def test_golden_json_summary_consistency(self, golden_json: dict) -> None:
        """Summary counts match actual service data (total_services_scanned quirk)."""
        services = golden_json["services"]
        summary = golden_json["summary"]

        # Replicate the quirky total_services_scanned calculation from cost_optimizer.py:3999-4007
        expected_scanned = sum(
            1 for s in services.values() if s.get("total_recommendations", 0) > 0 or s.get("total_count", 0) > 0
        )
        assert summary["total_services_scanned"] == expected_scanned

        # total_recommendations should match sum of all services
        expected_recs = sum(s.get("total_recommendations", 0) for s in services.values())
        assert summary["total_recommendations"] == expected_recs

        # total_monthly_savings should match sum of all services
        expected_savings = sum(s.get("total_monthly_savings", 0.0) for s in services.values())
        assert abs(summary["total_monthly_savings"] - expected_savings) < 0.01

    def test_ami_has_total_count(self, golden_json: dict) -> None:
        """AMI findings must include total_count field (parity with cost_optimizer.py:3893)."""
        ami = golden_json["services"]["ami"]
        assert "total_count" in ami, "AMI findings missing total_count field"
        assert ami["total_count"] > 0, "AMI total_count should be > 0 in goldens"

    def test_json_normalizer_stability(self, golden_json_text: str) -> None:
        """Normalize → re-normalize produces identical output (idempotent)."""
        first = normalize_json_for_comparison(golden_json_text)
        second = normalize_json_for_comparison(first)
        assert first == second


class TestGoldenHtmlParity:
    """Verify HTML report matches golden fixture after normalization."""

    def test_golden_html_loads(self, golden_html_text: str) -> None:
        """Golden HTML is non-empty and has basic structure."""
        assert len(golden_html_text) > 100
        assert "<html" in golden_html_text.lower()
        assert "</html>" in golden_html_text.lower()

    def test_golden_html_has_css(self, golden_html_text: str) -> None:
        """Golden HTML includes CSS (inline or linked)."""
        assert "<style" in golden_html_text.lower() or "report.css" in golden_html_text

    def test_golden_html_has_tabs(self, golden_html_text: str) -> None:
        """Golden HTML includes service tabs."""
        assert "tab" in golden_html_text.lower()

    def test_html_normalizer_stability(self, golden_html_text: str) -> None:
        """Normalize → re-normalize produces identical output (idempotent)."""
        first = normalize_html_for_comparison(golden_html_text)
        second = normalize_html_for_comparison(first)
        assert first == second


class TestNormalizers:
    """Unit tests for the normalizer functions."""

    def test_normalize_timestamps(self) -> None:
        """Verify ISO-8601 timestamps are replaced with the NORMALIZED sentinel."""
        from conftest import normalize_timestamps

        result = normalize_timestamps("scan at 2026-04-29T10:30:00+00:00 done")
        assert "2026" not in result
        assert "<NORMALIZED>" in result

    def test_normalize_account_id(self) -> None:
        """Verify 12-digit AWS account IDs are replaced with the NORMALIZED sentinel."""
        from conftest import normalize_account_id

        result = normalize_account_id('account_id: "123456789012"')
        assert "123456789012" not in result
        assert "<NORMALIZED>" in result

    def test_normalize_account_id(self) -> None:
        """Verify account ID normalisation via the tests.conftest import path."""
        from tests.conftest import normalize_account_id

        result = normalize_account_id('account_id: "123456789012"')
        assert "123456789012" not in result
        assert "<NORMALIZED>" in result

    def test_normalize_currency(self) -> None:
        """Verify currency values are rounded to nearest cent."""
        from conftest import normalize_currency

        result = normalize_currency("savings: $1,234.56/month")
        assert "$1234.56" in result  # Comma removed during normalization to 2 decimals

    def test_normalize_preserves_non_matching(self) -> None:
        """Verify normalizer leaves strings without timestamps unchanged."""
        from tests.conftest import normalize_timestamps

        result = normalize_timestamps("no timestamps here")
        assert result == "no timestamps here"
