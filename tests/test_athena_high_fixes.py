"""Athena L1 — outer ``list_work_groups`` failure must be classified.

The outer handler previously called ``ctx.warn`` directly, so an account-wide
``AccessDenied`` / ``UnauthorizedOperation`` surfaced as a generic warning rather
than a permission gap. It now routes through
``services/_aws_errors.record_aws_error`` so an IAM denial lands on
``ctx.permission_issue`` (and a transient failure still falls back to ``ctx.warn``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.athena as athena


def _access_denied(op: str = "ListWorkGroups") -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, op)


class _Ctx:
    """Minimal ScanContext double capturing warn / permission_issue calls."""

    def __init__(self, clients: dict[str, Any]):
        self._clients = clients
        self.region = "us-east-1"
        self.account_id = "123456789012"
        self.warnings: list[tuple] = []
        self.permission_issues: list[tuple] = []

    def client(self, name: str, region: str | None = None) -> Any:
        return self._clients.get(name)

    def warn(self, message: str, service: str | None = None) -> None:
        self.warnings.append((service, message))

    def permission_issue(self, message: str, service: str | None = None, action: str | None = None) -> None:
        self.permission_issues.append((service, message, action))


class _Boom:
    """A client whose every attribute, when called, raises ``exc``."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def __getattr__(self, _name: str):
        def _raise(*_a: Any, **_k: Any):
            raise self._exc

        return _raise


def _perm_services(ctx: _Ctx) -> list[str]:
    return [svc for svc, *_ in ctx.permission_issues]


def test_athena_list_work_groups_access_denied_classified() -> None:
    ctx = _Ctx({"athena": _Boom(_access_denied("ListWorkGroups"))})
    result = athena.get_enhanced_athena_checks(ctx)
    # Permission gap surfaced, not buried as a generic warning, and no recs emitted.
    assert "athena" in _perm_services(ctx)
    assert ctx.warnings == []
    assert result["recommendations"] == []


def test_athena_list_work_groups_transient_failure_warns() -> None:
    ctx = _Ctx({"athena": _Boom(RuntimeError("throttled"))})
    athena.get_enhanced_athena_checks(ctx)
    # Non-permission failure still falls back to ctx.warn, never silently swallowed.
    assert ctx.permission_issues == []
    assert any(svc == "athena" for svc, _ in ctx.warnings)
