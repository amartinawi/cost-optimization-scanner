"""TR-1 — idle ONLINE Transfer Family servers.

An ONLINE server bills every enabled protocol at $0.30/hour ($219/month each)
whether or not any client connects.

The gate rests on a documented subtlety in the metric semantics, quoted from
the AWS metrics reference: BytesIn/BytesOut are "emitted every 5 minutes
**while a connection is established** to the Transfer Family server. If no
files or bytes are transferred in the period, '0' is emitted."

So the SUM is not the idle signal - the PRESENCE of datapoints is:

* empty series  -> nobody ever connected  -> idle
* series of 0s  -> somebody connected and moved nothing -> NOT idle

A naive ``sum == 0`` conflates the two and would recommend stopping a server
someone is actively logging into. That inversion is what these tests pin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import services.transfer_svc as shim
from services.adapters.transfer import TransferModule

_CATEGORY = "Idle Transfer Servers"
_PER_PROTOCOL_MONTH = 0.30 * 730  # $219.00


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self) -> list[dict[str, Any]]:
        return self._pages


class _FakeTransfer:
    def __init__(self, servers: list[dict[str, Any]], *, describe_error: Exception | None = None) -> None:
        self._servers = servers
        self._describe_error = describe_error
        self.describe_calls: list[str] = []

    def get_paginator(self, _name: str) -> _FakePaginator:
        # Real ListedServer shape: no Protocols member.
        return _FakePaginator(
            [{"Servers": [{k: v for k, v in s.items() if k != "Protocols"} for s in self._servers]}]
        )

    def describe_server(self, ServerId: str) -> dict[str, Any]:  # noqa: N803 - boto3 shape
        self.describe_calls.append(ServerId)
        if self._describe_error is not None:
            raise self._describe_error
        for server in self._servers:
            if server.get("ServerId") == ServerId:
                return {"Server": {"ServerId": ServerId, "Protocols": server.get("Protocols", [])}}
        return {"Server": {"ServerId": ServerId, "Protocols": []}}


class _FakeCloudWatch:
    """`datapoints` is the list of Sum values to return per metric call."""

    def __init__(self, datapoints: list[float] | None, *, error: Exception | None = None) -> None:
        self._datapoints = datapoints
        self._error = error

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"Datapoints": [{"Sum": v} for v in (self._datapoints or [])]}


def _ctx(
    servers: list[dict[str, Any]],
    *,
    datapoints: list[float] | None = None,
    cw_error: Exception | None = None,
    describe_error: Exception | None = None,
    fast: bool = False,
) -> SimpleNamespace:
    transfer = _FakeTransfer(servers, describe_error=describe_error)
    cw = _FakeCloudWatch(datapoints, error=cw_error)
    ctx = SimpleNamespace(
        region="us-east-1",
        fast_mode=fast,
        pricing_engine=None,
        pricing_multiplier=1.0,
        warnings=[],
        permissions=[],
    )
    ctx.client = lambda name, region=None: {"transfer": transfer, "cloudwatch": cw}.get(name)
    ctx.warn = lambda message, service=None, **k: ctx.warnings.append(message)
    ctx.permission_issue = lambda message, service=None, action=None, **k: ctx.permissions.append(message)
    ctx._transfer = transfer
    return ctx


def _server(server_id: str = "s-1", *, state: str = "ONLINE", protocols: list[str] | None = None) -> dict:
    return {"ServerId": server_id, "State": state, "Protocols": protocols or ["SFTP"]}


def _idle(ctx: SimpleNamespace) -> list[dict[str, Any]]:
    out = shim.get_enhanced_transfer_checks(ctx)
    return [r for r in out["recommendations"] if r["CheckCategory"] == _CATEGORY]


# --------------------------------------------------------------------------- #
# The metric semantics
# --------------------------------------------------------------------------- #
def test_empty_series_means_nobody_connected() -> None:
    recs = _idle(_ctx([_server()], datapoints=[]))
    assert len(recs) == 1
    assert recs[0]["IdleEvidence"] is True
    assert recs[0]["ConnectionDatapoints"] == 0


def test_a_series_of_zeros_means_the_server_is_in_use() -> None:
    """Somebody connected and transferred nothing. `sum == 0` would call this
    idle and recommend stopping a server people are logging into."""
    assert _idle(_ctx([_server()], datapoints=[0.0, 0.0, 0.0])) == []


def test_traffic_is_obviously_not_idle() -> None:
    assert _idle(_ctx([_server()], datapoints=[1024.0])) == []


# --------------------------------------------------------------------------- #
# Fail-closed paths
# --------------------------------------------------------------------------- #
def test_denied_metric_read_emits_no_idle_rec() -> None:
    ctx = _ctx([_server()], cw_error=Exception("AccessDeniedException"))
    assert _idle(ctx) == []
    assert ctx.permissions, "the denial must be classified, not read as no-traffic"


def test_fast_mode_emits_no_idle_rec() -> None:
    assert _idle(_ctx([_server()], datapoints=[], fast=True)) == []


def test_offline_server_is_not_an_idle_candidate() -> None:
    """A stopped server already bills no protocol hours - the existing
    unused_servers advisory covers it, and stopping it again saves nothing."""
    assert _idle(_ctx([_server(state="STOPPED")], datapoints=[])) == []


def test_offline_server_costs_no_describe_call() -> None:
    ctx = _ctx([_server(state="OFFLINE")], datapoints=[])
    _idle(ctx)
    assert ctx._transfer.describe_calls == []


def test_unreadable_protocols_emit_no_priced_rec() -> None:
    """Without the protocol count there is no hourly charge to compute."""
    ctx = _ctx([_server()], datapoints=[], describe_error=Exception("AccessDeniedException"))
    assert _idle(ctx) == []
    assert ctx.permissions


# --------------------------------------------------------------------------- #
# Pricing through the adapter
# --------------------------------------------------------------------------- #
def test_single_protocol_server_counts_one_protocol_hour() -> None:
    findings = TransferModule().scan(_ctx([_server(protocols=["SFTP"])], datapoints=[]))
    assert findings.total_monthly_savings == pytest.approx(219.0, abs=0.01)


def test_multi_protocol_server_counts_every_enabled_protocol() -> None:
    findings = TransferModule().scan(
        _ctx([_server(protocols=["SFTP", "FTPS", "FTP"])], datapoints=[])
    )
    assert findings.total_monthly_savings == pytest.approx(3 * _PER_PROTOCOL_MONTH, abs=0.01)
    rec = next(
        r
        for r in findings.sources["enhanced_checks"].recommendations
        if r["CheckCategory"] == _CATEGORY
    )
    assert rec["Counted"] is True
    assert rec["AuditBasis"]["protocol_count"] == 3
    assert "if stopped" in rec["EstimatedSavings"]


def test_adapter_will_not_count_a_rec_without_the_evidence_flag() -> None:
    """Defence in depth: even if a rec reached the adapter claiming the
    category, only the shim's evidence flag unlocks the dollar."""
    module = TransferModule()
    ctx = _ctx([_server()], datapoints=[])

    forged = {
        "ServerId": "s-forged",
        "ProtocolCount": 3,
        "CheckCategory": _CATEGORY,
        "Recommendation": "forged",
    }
    original = shim.get_enhanced_transfer_checks
    try:
        shim.get_enhanced_transfer_checks = lambda _ctx: {"recommendations": [forged]}
        import services.adapters.transfer as adapter_mod

        adapter_mod.get_enhanced_transfer_checks = shim.get_enhanced_transfer_checks
        findings = module.scan(ctx)
    finally:
        shim.get_enhanced_transfer_checks = original
        import services.adapters.transfer as adapter_mod

        adapter_mod.get_enhanced_transfer_checks = original
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# TR-3 — the protocol lever was dead because of a shape that cannot occur
# --------------------------------------------------------------------------- #
def test_protocols_come_from_describe_server_not_list_servers() -> None:
    """ListedServer has no Protocols member, so reading it there always gave []
    and the `len(protocols) > 1` lever could never fire."""
    ctx = _ctx([_server(protocols=["SFTP", "FTPS"])], datapoints=[1024.0])
    out = shim.get_enhanced_transfer_checks(ctx)
    proto = out["checks"]["protocol_optimization"]
    assert len(proto) == 1
    assert proto[0]["Protocols"] == ["SFTP", "FTPS"]
    assert ctx._transfer.describe_calls == ["s-1"]
