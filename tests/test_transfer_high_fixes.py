"""Unit tests for the Transfer Family HIGH cost-correctness fixes (H1, H2).

Drives both the pure adapter pricing logic (via a monkeypatched
``get_enhanced_transfer_checks``) and the real ``scan()`` path (via a fake
boto3 ``transfer`` paginator + the genuine shim) with a ``SimpleNamespace``
ctx, mirroring ``tests/test_audit_fixes_counted_dollars.py`` /
``tests/test_lambda_audit_fixes.py``.

Covered:

  - **H1** A STOPPED/OFFLINE server (``Unused Transfer Servers``) is a
    *terminate-the-whole-server* rec: it must NOT carry the partial
    ``(len(protocols) - 1)`` protocol-removal dollar, and is emitted as a $0
    advisory (``Counted=False``) because a stopped server is not billing
    endpoint hours.
  - **H2** ``protocol_optimization`` ("remove all-but-one protocol") has no
    per-protocol usage evidence, so it is a $0 advisory — never
    ``(len-1) × $0.30 × 730``. A ``RemovableProtocols`` *count alone* is not
    evidence and must NOT be counted.
  - **H2 evidenced path** When the shim supplies explicit per-protocol usage
    evidence, the counted dollar equals the live-validated
    ``removable × $0.30/hr × 730`` and counted == rendered.
  - **Count hygiene** $0 advisory recs render but are excluded from
    ``total_recommendations``; ``total_monthly_savings`` == Σ counted dollars.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.transfer as transfer_adapter
import services.transfer_svc as transfer_shim
from services.adapters.transfer import TransferModule


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
def _ctx(*, fast_mode: bool = True, transfer_client: Any = None) -> SimpleNamespace:
    """A ctx that records warn / permission_issue and serves fake clients."""
    ctx = SimpleNamespace(
        region="us-east-1",
        account_id="123456789012",
        fast_mode=fast_mode,
        pricing_multiplier=1.25,  # deliberately != 1.0: region-flat rate must ignore it
        pricing_engine=None,
        cost_hub_splits={},
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(
        (service, action, msg)
    )
    clients = {"transfer": transfer_client}
    ctx.client = lambda name, region=None: clients.get(name)
    return ctx


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeTransferClient:
    """Minimal boto3 transfer client driving the enhanced-checks shim."""

    def __init__(self, servers: list[dict[str, Any]]) -> None:
        self._servers = servers

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator([{"Servers": self._servers}])


def _patch_checks(monkeypatch: pytest.MonkeyPatch, recs: list[dict[str, Any]]) -> None:
    """Bypass boto3 and feed the adapter a fixed recommendation list."""
    monkeypatch.setattr(
        transfer_adapter,
        "get_enhanced_transfer_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )


def _recs(findings) -> tuple[dict[str, Any], ...]:
    return findings.sources["enhanced_checks"].recommendations


# --------------------------------------------------------------------------- #
# H1 — unused/stopped server is a $0 advisory, never an (n-1) protocol figure
# --------------------------------------------------------------------------- #
def test_h1_stopped_server_is_zero_advisory_not_protocol_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-stopped",
                "State": "STOPPED",
                "Protocols": ["SFTP", "FTP", "FTPS"],
                "CheckCategory": "Unused Transfer Servers",
            }
        ],
    )
    findings = TransferModule().scan(_ctx())

    # The old defect counted (3-1) × $0.30 × 730 = $438 onto a terminate rec.
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0  # advisory excluded from count

    rec = _recs(findings)[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    # NEVER layer the partial protocol-removal dollar onto the terminate rec.
    assert "438" not in rec["EstimatedSavings"]
    assert rec["AuditBasis"]["counted"] is False


def test_h1_offline_server_detected_by_state_not_just_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Category absent / generic, but State=OFFLINE must still fail safe.
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-offline",
                "State": "OFFLINE",
                "Protocols": ["SFTP", "FTP"],
                "CheckCategory": "Protocol Optimization",
            }
        ],
    )
    findings = TransferModule().scan(_ctx())
    rec = _recs(findings)[0]
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0
    assert "offline" in rec["EstimatedSavings"].lower()


# --------------------------------------------------------------------------- #
# H2 — protocol_optimization with no usage evidence is a $0 advisory
# --------------------------------------------------------------------------- #
def test_h2_protocol_optimization_without_evidence_is_zero_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-online",
                "State": "ONLINE",
                "Protocols": ["SFTP", "FTP", "FTPS"],
                "CheckCategory": "Protocol Optimization",
            }
        ],
    )
    findings = TransferModule().scan(_ctx())

    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    rec = _recs(findings)[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    assert "per-protocol usage evidence" in rec["EstimatedSavings"]


def test_h2_removable_count_alone_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    # A RemovableProtocols COUNT without the evidence flag must NOT be counted
    # (this is exactly the fabricated lever H2 removes).
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-online",
                "State": "ONLINE",
                "Protocols": ["SFTP", "FTP", "FTPS"],
                "RemovableProtocols": 2,
                "CheckCategory": "Protocol Optimization",
            }
        ],
    )
    findings = TransferModule().scan(_ctx())
    assert findings.total_monthly_savings == 0.0
    assert _recs(findings)[0]["Counted"] is False


# --------------------------------------------------------------------------- #
# H2 evidenced path — counted dollar == rendered dollar, live-validated rate
# --------------------------------------------------------------------------- #
def test_h2_evidenced_path_counts_validated_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-evidenced",
                "State": "ONLINE",
                "Protocols": ["SFTP", "FTP", "FTPS"],
                "RemovableProtocols": 2,
                "PerProtocolUsageEvidence": True,
                "CheckCategory": "Protocol Optimization",
            }
        ],
    )
    # pricing_multiplier=1.25 must be IGNORED (region-flat ProtocolHours).
    findings = TransferModule().scan(_ctx())

    # 2 removable × $0.30/hr × 730 = $438.00 (NOT 438 × 1.25 = $547.50).
    assert findings.total_monthly_savings == pytest.approx(438.0, abs=0.01)
    assert findings.total_recommendations == 1
    rec = _recs(findings)[0]
    assert rec["Counted"] is True
    assert rec["EstimatedMonthlySavings"] == pytest.approx(438.0, abs=0.01)
    # counted == rendered: the card string carries the same dollar.
    assert rec["EstimatedSavings"].startswith("$438.00")
    assert rec["AuditBasis"]["rate_per_protocol_hour"] == 0.30
    assert rec["AuditBasis"]["removable_protocols"] == 2


def test_counted_equals_rendered_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mix of one evidenced (counted) + one advisory rec: the summed headline
    # must equal exactly the counted rec's rendered dollar.
    _patch_checks(
        monkeypatch,
        [
            {
                "ServerId": "s-evidenced",
                "State": "ONLINE",
                "Protocols": ["SFTP", "FTP"],
                "RemovableProtocols": 1,
                "PerProtocolUsageEvidence": True,
                "CheckCategory": "Protocol Optimization",
            },
            {
                "ServerId": "s-stopped",
                "State": "STOPPED",
                "Protocols": ["SFTP", "FTP", "FTPS"],
                "CheckCategory": "Unused Transfer Servers",
            },
        ],
    )
    findings = TransferModule().scan(_ctx())

    counted_sum = sum(
        r["EstimatedMonthlySavings"] for r in _recs(findings) if r.get("Counted") is not False
    )
    assert findings.total_monthly_savings == pytest.approx(counted_sum, abs=0.01)
    # 1 removable × $0.30 × 730 = $219.00
    assert findings.total_monthly_savings == pytest.approx(219.0, abs=0.01)
    assert findings.total_recommendations == 1  # only the counted rec
    assert len(_recs(findings)) == 2  # both still render


# --------------------------------------------------------------------------- #
# scan() path through the REAL shim + a fake boto3 transfer paginator
# --------------------------------------------------------------------------- #
def test_scan_path_real_shim_all_advisory_zero_headline() -> None:
    client = _FakeTransferClient(
        [
            {"ServerId": "s-on", "State": "ONLINE", "Protocols": ["SFTP", "FTP", "FTPS"]},
            {"ServerId": "s-stop", "State": "STOPPED", "Protocols": ["SFTP", "FTP"]},
        ]
    )
    ctx = _ctx(fast_mode=True, transfer_client=client)
    findings = TransferModule().scan(ctx)

    # Online 3-protocol server → protocol_optimization advisory ($0);
    # stopped server → unused_servers advisory ($0). Nothing counted.
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    recs = _recs(findings)
    assert len(recs) == 2  # both rendered as advisory
    assert all(r["Counted"] is False for r in recs)
    assert all(r["EstimatedMonthlySavings"] == 0.0 for r in recs)
    # No fabricated protocol-removal figure leaked anywhere.
    assert all("438" not in r["EstimatedSavings"] for r in recs)


def test_shim_protocol_rec_does_not_fabricate_dollar() -> None:
    # The shim itself must not bake the (len-1) × $0.30 × 730 dollar into the
    # protocol_optimization rec's EstimatedSavings (transfer H2).
    client = _FakeTransferClient(
        [{"ServerId": "s-on", "State": "ONLINE", "Protocols": ["SFTP", "FTP", "FTPS"]}]
    )
    ctx = _ctx(fast_mode=True, transfer_client=client)
    result = transfer_shim.get_enhanced_transfer_checks(ctx)
    proto = result["checks"]["protocol_optimization"]
    assert len(proto) == 1
    # 2 × 0.30 × 730 = 438 must NOT appear; honest advisory string instead.
    assert "438" not in proto[0]["EstimatedSavings"]
    assert proto[0]["EstimatedSavings"].startswith("$0.00")
