"""Shared AWS error classification for service adapters.

Sub-shims and adapters must never swallow a describe/CloudWatch failure with a
logger call alone — a permission gap or throttle that vanishes silently turns
into a missing finding (and a missing dollar) with no signal to the operator.
Route every caught AWS exception through :func:`record_aws_error` so it lands on
``ctx.permission_issue`` (IAM gaps) or ``ctx.warn`` (everything else).
"""

from __future__ import annotations

from typing import Any

# Error codes that mean "the caller is not allowed / not opted in" — these are
# IAM/account-configuration gaps, not transient failures.
_PERMISSION_CODES: frozenset = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "OptInRequired",
        "SubscriptionRequiredException",
        "AuthFailure",
    }
)


def _error_code(exc: BaseException) -> str:
    """Best-effort extraction of the AWS error code from a botocore ClientError."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


def is_permission_error(exc: BaseException) -> bool:
    """Return True when the exception denotes an IAM/opt-in permission gap."""
    if _error_code(exc) in _PERMISSION_CODES:
        return True
    text = str(exc)
    return any(
        marker in text
        for marker in ("AccessDenied", "UnauthorizedOperation", "not authorized", "OptInRequired")
    )


def record_aws_error(ctx: Any, exc: BaseException, *, service: str, context: str) -> None:
    """Record an AWS exception on ctx, classifying permission gaps vs other failures.

    AccessDenied / UnauthorizedOperation / OptInRequired → ``ctx.permission_issue``;
    anything else → ``ctx.warn``. Never silently swallows.
    """
    if is_permission_error(exc):
        ctx.permission_issue(f"{context}: {exc}", service=service, action=_error_code(exc) or None)
    else:
        ctx.warn(f"{context}: {exc}", service=service)
