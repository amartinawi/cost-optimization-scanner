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


@pytest.mark.parametrize("service_key", ["opensearch", "api_gateway", "step_functions"])
def test_no_flat_fifty_fabrication_when_total_is_zero(service_key: str, reporter: HTMLReportGenerator) -> None:
    """SR-2 — the reporter must never invent a dollar the adapter did not count.

    Previously a $0 service_total for {opensearch, api_gateway, step_functions}
    triggered ``$50 × recs`` in the tab headline, exec-summary, and
    reconciliation footnote. With ``_FLAT_SAVINGS_SERVICES`` deleted, a
    zero-total service with N recs must compute $0.00 (the recs still render as
    advisory cards via Counted=False). This guards against any future
    re-introduction of the flat-$50 fabrication.
    """
    n_recs = 3
    service_data = {
        "total_monthly_savings": 0,
        "sources": {
            "enhanced_checks": {
                "count": n_recs,
                "recommendations": [{"Recommendation": f"advisory rec {i}", "Counted": False} for i in range(n_recs)],
            }
        },
    }
    assert reporter._calculate_service_savings(service_key, service_data) == 0.0


@pytest.mark.parametrize(
    "service_key, rec_text",
    [
        ("ec2", "schedule stop/start outside business hours"),  # was the $150 "schedule" keyword
        ("ec2", "Consider Lambda for cron jobs"),  # was the $25 _DEFAULT_SAVINGS fallback
        ("dynamodb", "switch to reserved capacity"),  # was the $200 "reserved" keyword
        ("dynamodb", "over-provisioned table"),  # was the $50 _DEFAULT_SAVINGS fallback
    ],
)
def test_ec2_dynamodb_keyword_fabrication_removed(service_key: str, rec_text: str, reporter: HTMLReportGenerator) -> None:
    """Cost-fidelity (extends SR-2 to ec2/dynamodb) — the reporter must not invent
    a dollar the adapter did not count.

    ``_calculate_service_savings`` previously synthesized $25-$200/rec from
    recommendation-text keywords (e.g. ec2 "schedule" -> $150, dynamodb "reserved"
    -> $200) whenever an adapter's ``total_monthly_savings`` was $0 — re-fabricating
    at the report layer exactly the advisory dollars the ec2 H2 / dynamodb fixes
    demote to $0. A $0-canonical service with keyword-laden advisory recs must now
    compute $0.00.
    """
    service_data = {
        "total_monthly_savings": 0,
        "sources": {
            "enhanced_checks": {
                "count": 2,
                "recommendations": [{"Recommendation": rec_text, "Counted": False} for _ in range(2)],
            }
        },
    }
    assert reporter._calculate_service_savings(service_key, service_data) == 0.0


def test_canonical_savings_passed_through(reporter: HTMLReportGenerator) -> None:
    """A non-zero counted total is returned verbatim (never re-derived/inflated)."""
    service_data = {"total_monthly_savings": 1234.56, "sources": {}}
    assert reporter._calculate_service_savings("ec2", service_data) == pytest.approx(1234.56)


def test_scan_diagnostics_rendered_when_present(scan_results: dict) -> None:
    """scan_warnings + permission_issues surface in the HTML (they were JSON-only
    before, so a reader could not see suppressed/degraded operations)."""
    enriched = {
        **scan_results,
        "scan_warnings": ["Cost Hub recommends deleting vol-abc but attached — dropping.", "ce: no data"],
        "permission_issues": [{"message": "missing iam:GetMetricStatistics"}],
    }
    out = HTMLReportGenerator(enriched)._render_scan_diagnostics()
    assert "<details" in out
    assert "3 scan diagnostics" in out and "1 permission issue" in out
    assert "vol-abc" in out and "missing iam:GetMetricStatistics" in out


def test_scan_diagnostics_silent_when_clean(scan_results: dict) -> None:
    """A clean scan (no warnings/permission issues) renders nothing — the report
    is byte-for-byte unchanged from before this disclosure was added."""
    clean = {**scan_results, "scan_warnings": [], "permission_issues": []}
    assert HTMLReportGenerator(clean)._render_scan_diagnostics() == ""


def test_advisory_only_service_renders_a_tab(scan_results: dict, tmp_path: Path) -> None:
    """A service with only $0 advisory recs (counted total = 0) must still render
    a tab — restoring the regression that hid S3's 138 advisory cards. A service
    that renders nothing at all must NOT get a tab.
    """
    import copy

    enriched = copy.deepcopy(scan_results)
    enriched["services"]["advtest"] = {
        "service_name": "AdvOnly",
        "total_recommendations": 0,  # counted-only headline is 0
        "total_monthly_savings": 0.0,
        "sources": {
            "checks": {
                "count": 2,
                "recommendations": [
                    {
                        "Counted": False,
                        "EstimatedMonthlySavings": 0.0,
                        "EstimatedSavings": "$0.00/month — advisory: one",
                        "Recommendation": "advisory one",
                        "CheckCategory": "Advisory",
                        "ResourceName": "res-a",
                    },
                    {
                        "Counted": False,
                        "EstimatedMonthlySavings": 0.0,
                        "EstimatedSavings": "$0.00/month — advisory: two",
                        "Recommendation": "advisory two",
                        "CheckCategory": "Advisory",
                        "ResourceName": "res-b",
                    },
                ],
            }
        },
    }
    enriched["services"]["emptytest"] = {
        "service_name": "EmptyOnly",
        "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {},
    }
    out_file = tmp_path / "advisory_tab_report.html"
    HTMLReportGenerator(enriched).generate_html_report(str(out_file))
    html = out_file.read_text()
    # Advisory-only service -> tab + panel present.
    assert 'id="panel-advtest"' in html
    assert 'id="tab-advtest"' in html
    # Truly empty service -> no tab (nothing renders).
    assert 'id="panel-emptytest"' not in html
