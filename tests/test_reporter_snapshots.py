"""
Snapshot tests for HTMLReportGenerator's 4 target methods.

Captures per-service HTML output BEFORE any refactoring of
_get_detailed_recommendations() (2,300 LOC, 62 if-branches).

Usage:
  SNAPSHOT_UPDATE=1 pytest tests/test_reporter_snapshots.py   # create/refresh
  pytest tests/test_reporter_snapshots.py                       # compare

Methods under test:
  - _get_service_stats(service_key, service_data)
  - _get_detailed_recommendations(service_key, service_data)
  - _get_affected_resources_list(service_key, service_data)
  - _calculate_service_savings(service_key, service_data)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from html_report_generator import HTMLReportGenerator

from conftest import (
    FIXTURES_DIR,
    GOLDEN_JSON,
    normalize_html_for_comparison,
)

SNAPSHOT_DIR = FIXTURES_DIR / "reporter_snapshots"

ALL_SERVICES = [
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
]


def _snapshot_update_requested() -> bool:
    """Check whether SNAPSHOT_UPDATE=1 is set in the environment."""
    return os.environ.get("SNAPSHOT_UPDATE", "") == "1"


@pytest.fixture(scope="session")
def scan_results() -> dict:
    """Load the golden JSON scan results for snapshot comparison."""
    with open(GOLDEN_JSON) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def reporter(scan_results: dict) -> HTMLReportGenerator:
    """Provide a session-scoped HTMLReportGenerator instance."""
    return HTMLReportGenerator(scan_results)


@pytest.fixture(scope="session")
def service_data_map(scan_results: dict) -> dict:
    """Provide the services dict from golden scan results."""
    return scan_results["services"]


def _load_snapshot(path: Path) -> str | None:
    """Read a snapshot file, returning None if it does not exist."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _save_snapshot(path: Path, content: str) -> None:
    """Write content to a snapshot file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _snapshot_name(service_key: str, method: str, ext: str) -> str:
    """Build a safe filename for a snapshot file."""
    safe = service_key.replace("/", "_")
    return f"{safe}.{ext}"


@pytest.mark.parametrize("service_key", ALL_SERVICES)
def test_service_stats_snapshot(
    service_key: str,
    reporter: HTMLReportGenerator,
    service_data_map: dict,
) -> None:
    """Verify _get_service_stats output matches the stored HTML snapshot."""
    raw_data = service_data_map[service_key]
    filtered = reporter._filter_recommendations(raw_data)

    actual = reporter._get_service_stats(service_key, filtered)
    actual_normalized = normalize_html_for_comparison(actual)

    snap_path = SNAPSHOT_DIR / "stats" / _snapshot_name(service_key, "stats", "html")

    if _snapshot_update_requested():
        _save_snapshot(snap_path, actual_normalized)
        return

    expected = _load_snapshot(snap_path)
    assert expected is not None, f"Snapshot missing: {snap_path}. Run with --snapshot-update to create."
    assert actual_normalized == expected, (
        f"Stats snapshot mismatch for '{service_key}'. Run --snapshot-update to refresh if change is intentional."
    )


@pytest.mark.parametrize("service_key", ALL_SERVICES)
def test_detailed_recommendations_snapshot(
    service_key: str,
    reporter: HTMLReportGenerator,
    service_data_map: dict,
) -> None:
    """Verify _get_detailed_recommendations output matches the stored HTML snapshot."""
    raw_data = service_data_map[service_key]
    filtered = reporter._filter_recommendations(raw_data)

    actual = reporter._get_detailed_recommendations(service_key, filtered)
    actual_normalized = normalize_html_for_comparison(actual)

    snap_path = SNAPSHOT_DIR / "recommendations" / _snapshot_name(service_key, "recommendations", "html")

    if _snapshot_update_requested():
        _save_snapshot(snap_path, actual_normalized)
        return

    expected = _load_snapshot(snap_path)
    assert expected is not None, f"Snapshot missing: {snap_path}. Run with --snapshot-update to create."
    assert actual_normalized == expected, (
        f"Recommendations snapshot mismatch for '{service_key}'. "
        f"Run --snapshot-update to refresh if change is intentional."
    )


@pytest.mark.parametrize("service_key", ALL_SERVICES)
def test_affected_resources_snapshot(
    service_key: str,
    reporter: HTMLReportGenerator,
    service_data_map: dict,
) -> None:
    """Verify _get_affected_resources_list output matches the stored HTML snapshot."""
    raw_data = service_data_map[service_key]
    filtered = reporter._filter_recommendations(raw_data)

    actual = reporter._get_affected_resources_list(service_key, filtered)
    actual_normalized = normalize_html_for_comparison(actual)

    snap_path = SNAPSHOT_DIR / "resources" / _snapshot_name(service_key, "resources", "html")

    if _snapshot_update_requested():
        _save_snapshot(snap_path, actual_normalized)
        return

    expected = _load_snapshot(snap_path)
    assert expected is not None, f"Snapshot missing: {snap_path}. Run with --snapshot-update to create."
    assert actual_normalized == expected, (
        f"Resources snapshot mismatch for '{service_key}'. Run --snapshot-update to refresh if change is intentional."
    )


@pytest.mark.parametrize("service_key", ALL_SERVICES)
def test_service_savings_snapshot(
    service_key: str,
    reporter: HTMLReportGenerator,
    service_data_map: dict,
) -> None:
    """Verify _calculate_service_savings output matches the stored text snapshot."""
    raw_data = service_data_map[service_key]
    filtered = reporter._filter_recommendations(raw_data)

    actual = reporter._calculate_service_savings(service_key, filtered)
    actual_str = f"{actual:.6f}"

    snap_path = SNAPSHOT_DIR / "savings" / _snapshot_name(service_key, "savings", "txt")

    if _snapshot_update_requested():
        _save_snapshot(snap_path, actual_str)
        return

    expected = _load_snapshot(snap_path)
    assert expected is not None, f"Snapshot missing: {snap_path}. Run with --snapshot-update to create."
    assert actual_str == expected, (
        f"Savings snapshot mismatch for '{service_key}': "
        f"expected {expected}, got {actual_str}. "
        f"Run --snapshot-update to refresh if change is intentional."
    )
