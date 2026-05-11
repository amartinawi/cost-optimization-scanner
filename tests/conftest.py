"""
T-002 + T-003: Regression snapshot harness and AWS mock surface.

Provides:
- Golden fixture loading (JSON + HTML)
- Frozen clock (freezegun) pinned to capture timestamp
- Normalizers for timestamp, account_id, filename, and currency comparison
- stubbed_aws fixture combining moto.mock_aws with Stubber fallback
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import Stubber
from freezegun import freeze_time
from moto import mock_aws

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_JSON = FIXTURES_DIR / "golden_scan_results.json"
GOLDEN_HTML = FIXTURES_DIR / "golden_report.html"
CAPTURE_TIMESTAMP_FILE = FIXTURES_DIR / "CAPTURE_TIMESTAMP.txt"


def _read_capture_timestamp() -> str:
    """Read the frozen-clock timestamp from the capture file."""
    return CAPTURE_TIMESTAMP_FILE.read_text().strip()


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

# Sentinel used to replace variable fields before comparison
_NORMALIZED = "<NORMALIZED>"


def normalize_timestamps(text: str) -> str:
    """Replace ISO-8601 timestamps with a sentinel."""
    # Matches patterns like 2026-04-29T10:30:00+00:00 or 2026-04-29T10:30:00.123456+00:00
    return re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}",
        _NORMALIZED,
        text,
    )


def normalize_account_id(text: str) -> str:
    """Replace 12-digit AWS account IDs with a sentinel."""
    return re.sub(r"\b\d{12}\b", _NORMALIZED, text)


def normalize_filenames(text: str) -> str:
    """Replace output filename timestamps like cost_optimization_scan_*.json."""
    return re.sub(
        r"cost_optimization_scan_[\w\-\.]+",
        f"cost_optimization_scan_{_NORMALIZED}",
        text,
    )


def _normalize_currency_value(match: re.Match) -> str:
    """Round a currency string to nearest cent for ±$0.01 tolerance."""
    raw = match.group(0)
    # Extract numeric value
    num_match = re.search(r"[\d,]+\.\d{2}", raw)
    if not num_match:
        return raw
    num_str = num_match.group(0).replace(",", "")
    try:
        value = float(num_str)
        rounded = round(value, 2)
        # Reconstruct with the same surrounding text
        prefix = raw[: num_match.start()]
        suffix = raw[num_match.end() :]
        return f"{prefix}{rounded:.2f}{suffix}"
    except ValueError:
        return raw


def normalize_currency(text: str) -> str:
    """Normalize all currency values to nearest cent for ±$0.01 tolerance."""
    # Match $X,XXX.XX patterns (with optional comma separators)
    return re.sub(
        r"\$[\d,]+\.\d{2}",
        _normalize_currency_value,
        text,
    )


def normalize_json_for_comparison(json_text: str) -> str:
    """Apply all normalizers for JSON comparison."""
    result = normalize_timestamps(json_text)
    result = normalize_account_id(result)
    result = normalize_filenames(result)
    result = normalize_currency(result)
    return result


def normalize_html_for_comparison(html_text: str) -> str:
    """Apply all normalizers for HTML comparison."""
    result = normalize_timestamps(html_text)
    result = normalize_account_id(result)
    result = normalize_filenames(result)
    result = normalize_currency(result)
    return result


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def capture_timestamp() -> str:
    """Return the frozen-clock timestamp from the capture file."""
    return _read_capture_timestamp()


@pytest.fixture(scope="session")
def golden_json() -> dict[str, Any]:
    """Load the golden JSON scan results."""
    with open(GOLDEN_JSON) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def golden_json_text() -> str:
    """Load the golden JSON scan results as raw text."""
    return GOLDEN_JSON.read_text()


@pytest.fixture(scope="session")
def golden_html_text() -> str:
    """Load the golden HTML report as raw text."""
    return GOLDEN_HTML.read_text()


@pytest.fixture(scope="session", autouse=True)
def frozen_time(capture_timestamp: str):
    """Freeze the clock to the capture timestamp for every test in the session."""
    with freeze_time(capture_timestamp):
        yield


# ---------------------------------------------------------------------------
# T-003: AWS mock surface (moto + Stubber)
# ---------------------------------------------------------------------------

# Services that moto does NOT implement — need Stubber fallback
_STUBBER_SERVICES: Dict[str, Dict[str, Any]] = {
    "cost-optimization-hub": {
        "api_calls": {
            "ListRecommendations": {"items": [], "nextToken": None},
            "GetRecommendation": {
                "recommendationId": "stub-id",
                "currentResourceType": "Ec2Instance",
                "recommendedResourceType": "Ec2Instance",
                "estimatedMonthlySavings": {"currency": "USD", "value": 0.0},
            },
        },
    },
    "compute-optimizer": {
        "api_calls": {
            "GetEC2InstanceRecommendations": {
                "instanceRecommendations": [],
                "nextToken": None,
            },
            "GetEBSVolumeRecommendations": {
                "volumeRecommendations": [],
                "nextToken": None,
            },
            "GetRDSDatabaseRecommendations": {
                "rdsDBRecommendations": [],
                "nextToken": None,
            },
        },
    },
}


def _attach_stubbers(session: boto3.Session, region: str) -> Dict[str, Stubber]:
    """Attach Stubbers to session clients for services moto doesn't cover.

    Args:
        session: boto3 session (already inside mock_aws context)
        region: AWS region for client creation

    Returns:
        Dict mapping service names to their Stubber instances.
    """
    stubbers: Dict[str, Stubber] = {}

    for service_name, config in _STUBBER_SERVICES.items():
        try:
            client = session.client(service_name, region_name=region)
            stubber = Stubber(client)
            for operation_name, response in config["api_calls"].items():
                stubber.add_response(operation_name, response)
            stubber.activate()
            stubbers[service_name] = stubber
        except Exception:
            pass

    return stubbers


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Run tests with live AWS Pricing API")


@pytest.fixture()
def live_only(request):
    if not request.config.getoption("--live"):
        pytest.skip("Use --live to run against real AWS Pricing API")


@pytest.fixture()
def mock_pricing_engine():
    from core.pricing_engine import PricingEngine

    mock_client = MagicMock()
    mock_client.get_products.return_value = {"PriceList": []}
    return PricingEngine("us-east-1", mock_client)


@pytest.fixture()
def stubbed_aws():
    """Combine moto mock_aws with Stubber fallback for uncovered clients.

    Yields a configured boto3.Session with all clients pre-mocked.
    Works with no AWS credentials, no network.

    Usage::

        def test_something(stubbed_aws):
            optimizer = CostOptimizer('us-east-1')
            results = optimizer.scan_region()
    """
    env_backup: Dict[str, str | None] = {}
    for key in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
    ):
        env_backup[key] = os.environ.pop(key, None)

    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    mock_ctx = mock_aws()
    mock_ctx.__enter__()

    from tests.fixtures.aws_state.us_east_1 import seed_all

    resource_ids = seed_all()

    session = boto3.Session(region_name="us-east-1")

    stubbers = _attach_stubbers(session, "us-east-1")

    class _StubbedSession:
        """Wrapper providing the session and metadata for test assertions."""

        def __init__(
            self,
            boto_session: boto3.Session,
            resources: Dict[str, Any],
            stubber_map: Dict[str, Stubber],
        ) -> None:
            """Initialise with a boto3 session, seeded resources, and active stubbers."""
            self.session = boto_session
            self.resources = resources
            self.stubbers = stubber_map

    yield _StubbedSession(session, resource_ids, stubbers)

    for stubber in stubbers.values():
        try:
            stubber.deactivate()
        except Exception:
            pass

    mock_ctx.__exit__(None, None, None)

    for key, original_value in env_backup.items():
        if original_value is not None:
            os.environ[key] = original_value
        else:
            os.environ.pop(key, None)
