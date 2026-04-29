from __future__ import annotations

from unittest.mock import MagicMock

from core.client_registry import ClientRegistry
from core.contracts import ServiceFindings
from core.scan_context import ScanContext


def _make_ctx() -> ScanContext:
    registry = MagicMock(spec=ClientRegistry)
    return ScanContext(
        region="us-east-1",
        account_id="123456789012",
        profile=None,
        fast_mode=False,
        clients=registry,
    )


def _broken_module() -> MagicMock:
    m = MagicMock()
    m.key = "broken"
    m.display_name = "Broken"
    m.scan.side_effect = RuntimeError("boom")
    return m


def test_safe_scan_catches_exception():
    ctx = _make_ctx()
    from core.scan_orchestrator import safe_scan

    findings = safe_scan(_broken_module(), ctx)

    assert findings.service_name == "Broken"
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert len(ctx._warnings) == 1
    assert "boom" in ctx._warnings[0].message
