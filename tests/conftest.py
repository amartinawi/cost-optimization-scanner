"""
T-002: Regression snapshot harness for the AWS Cost Optimization Scanner.

Provides:
- Golden fixture loading (JSON + HTML)
- Frozen clock (freezegun) pinned to capture timestamp
- Normalizers for timestamp, account_id, filename, and currency comparison
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

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
