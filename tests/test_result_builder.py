"""Unit tests for ScanResultBuilder summary and serialisation logic."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from core.contracts import ServiceFindings, SourceBlock
from core.result_builder import ScanResultBuilder


class TestSummary:
    """Tests for ScanResultBuilder._summary aggregation logic."""

    def test_total_services_scanned_quirk(self) -> None:
        """Verify total_services_scanned counts services with recommendations or total_count > 0."""
        findings: dict[str, ServiceFindings] = {
            "ec2": ServiceFindings(
                service_name="EC2",
                total_recommendations=5,
                total_monthly_savings=100.0,
                sources={"enhanced_checks": SourceBlock(count=5, recommendations=())},
            ),
            "ami": ServiceFindings(
                service_name="AMI",
                total_recommendations=0,
                total_monthly_savings=0.0,
                sources={},
                total_count=2,
            ),
            "s3": ServiceFindings(
                service_name="S3",
                total_recommendations=3,
                total_monthly_savings=50.0,
                sources={"s3_bucket_analysis": SourceBlock(count=3, recommendations=())},
            ),
            "rds": ServiceFindings(
                service_name="RDS",
                total_recommendations=0,
                total_monthly_savings=0.0,
                sources={},
            ),
        }

        summary = ScanResultBuilder._summary(findings)
        assert summary["total_services_scanned"] == 3
        assert summary["total_recommendations"] == 8
        assert abs(summary["total_monthly_savings"] - 150.0) < 0.01


class TestSerialize:
    """Tests for ScanResultBuilder._serialize field filtering."""

    def test_drops_zero_total_count(self) -> None:
        """Verify total_count is omitted when zero."""
        f = ServiceFindings(
            service_name="S3",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={},
        )
        result = ScanResultBuilder._serialize(f)
        assert "total_count" not in result
        assert "schema_version" not in result

    def test_preserves_ami_total_count(self) -> None:
        """Verify total_count is preserved when non-zero."""
        f = ServiceFindings(
            service_name="AMI",
            total_recommendations=2,
            total_monthly_savings=10.0,
            sources={},
            total_count=5,
        )
        result = ScanResultBuilder._serialize(f)
        assert result["total_count"] == 5

    def test_handles_mapping_proxy_extras(self) -> None:
        """Verify MappingProxyType extras are flattened into the serialised output."""
        f = ServiceFindings(
            service_name="EC2",
            total_recommendations=1,
            total_monthly_savings=5.0,
            sources={},
            extras=MappingProxyType({"custom_field": "value", "count": 42}),
        )
        result = ScanResultBuilder._serialize(f)
        assert result["custom_field"] == "value"
        assert result["count"] == 42
