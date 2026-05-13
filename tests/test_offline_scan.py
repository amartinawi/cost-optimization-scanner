"""
T-003: Verify CostOptimizer runs offline with mocked AWS.

This test proves that the scanner can execute a full scan without any
AWS credentials or network access. It uses the ``stubbed_aws`` fixture
which combines moto (for supported services) with Stubber fallback
(for unsupported services like Cost Optimization Hub and Compute Optimizer).

PREREQUISITES:
    - moto[all] >= 5.0 installed
    - No AWS credentials in environment (the fixture clears them)
    - No network access required

This test MUST work with:
    unset AWS_PROFILE; unset AWS_ACCESS_KEY_ID; unset AWS_SECRET_ACCESS_KEY
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_optimizer import CostOptimizer

EXPECTED_SERVICE_KEYS = {
    "ec2",
    "ami",
    "ebs",
    "rds",
    "file_systems",
    "s3",
    "sagemaker",
    "dynamodb",
    "containers",
    "network",
    "network_cost",
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
    "bedrock",
    "compute_optimizer",
    # cost_optimization_hub retired from ALL_MODULES (2026-05-14):
    # findings are now distributed into per-service tabs via
    # ScanOrchestrator._prefetch_advisor_data + ctx.cost_hub_splits.
    "aurora",
    "commitment_analysis",
    "cost_anomaly",
    "eks_cost",
}


def test_scan_with_stubbed_aws(stubbed_aws) -> None:
    """Verify CostOptimizer runs a full scan offline with mocked AWS."""
    optimizer = CostOptimizer(region="us-east-1")

    results = optimizer.scan_region()

    assert isinstance(results, dict), "scan_region must return a dict"

    assert "services" in results, "Results must contain 'services' key"
    actual_keys = set(results["services"].keys())
    assert actual_keys == EXPECTED_SERVICE_KEYS, (
        f"Missing: {EXPECTED_SERVICE_KEYS - actual_keys}, Extra: {actual_keys - EXPECTED_SERVICE_KEYS}"
    )

    assert "account_id" in results
    assert "region" in results
    assert results["region"] == "us-east-1"

    assert "summary" in results
    summary = results["summary"]
    assert "total_recommendations" in summary
    assert "total_monthly_savings" in summary
    assert isinstance(summary["total_recommendations"], int)
    assert isinstance(summary["total_monthly_savings"], (int, float))
