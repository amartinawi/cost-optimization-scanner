"""Unit tests for the App Runner HIGH/CRITICAL cost-audit fixes (apprunner C1/H1/H2).

Drives both the pure logic (``services.apprunner.get_enhanced_apprunner_checks`` /
``_monthly_requests``) and the ``scan()`` path (``AppRunnerModule``) with a
``SimpleNamespace`` ctx + fake boto3 clients, mirroring
``tests/test_lambda_audit_fixes.py`` / ``tests/test_audit_fixes_counted_dollars.py``.

Proves:
  - C1/H1  The CloudWatch ``Requests`` read is wired to the REAL published
           dimensions (``ServiceName`` + ``ServiceID``) — the old invalid
           ``Service`` dimension name (a guaranteed no-op) is gone, so an idle
           service is actually flagged and priced.
  - Counted dollar = provisioned-memory charge (memory_GB x $0.007/hr x 730),
           region-scaled once; AuditBasis records the live-validated SKU.
  - H2     Permission/throttle failures on list_services / describe_service /
           CloudWatch are classified via record_aws_error (permission_issue vs
           warn) and NEVER fabricate a counted dollar.
  - Fail-safe: a failed CloudWatch read or a denied describe_service read
           abstains (no counted delete rec) rather than guessing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.apprunner as shim
from services.adapters.apprunner import APP_RUNNER_MEM_GB_HOURLY, HOURS_PER_MONTH, AppRunnerModule


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeCloudWatch:
    """Returns a canned ``Requests`` datapoint, no datapoints, or raises."""

    def __init__(self, requests_sum: float | None = None, error: Exception | None = None) -> None:
        # requests_sum is None -> empty Datapoints (no-data); a number -> one datapoint.
        self._requests_sum = requests_sum
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if self._requests_sum is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Sum": self._requests_sum}]}


class _FakeAppRunner:
    """Minimal boto3 App Runner client driving the enhanced-checks shim."""

    def __init__(
        self,
        services: list[dict[str, Any]],
        configs: dict[str, dict[str, Any]] | None = None,
        describe_errors: dict[str, Exception] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self._services = services
        self._configs = configs or {}  # ServiceArn -> InstanceConfiguration
        self._describe_errors = describe_errors or {}  # ServiceArn -> Exception
        self._list_error = list_error
        self.describe_calls: list[str] = []

    def list_services(self) -> dict[str, Any]:
        if self._list_error is not None:
            raise self._list_error
        return {"ServiceSummaryList": list(self._services)}

    def describe_service(self, ServiceArn: str) -> dict[str, Any]:  # noqa: N803 - boto3 shape
        self.describe_calls.append(ServiceArn)
        if ServiceArn in self._describe_errors:
            raise self._describe_errors[ServiceArn]
        return {"Service": {"InstanceConfiguration": self._configs.get(ServiceArn, {})}}


def _ctx(
    apprunner_client: Any,
    cw_client: Any,
    *,
    pricing_multiplier: float = 1.0,
    fast_mode: bool = False,
) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_multiplier=pricing_multiplier,
        fast_mode=fast_mode,
        warnings=[],
        permissions=[],
    )
    clients = {"apprunner": apprunner_client, "cloudwatch": cw_client}
    ctx.client = lambda name, **_k: clients[name]
    ctx.warn = lambda msg, service=None, **_k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **_k: ctx.permissions.append(
        (service, action, msg)
    )
    return ctx


def _svc(
    name: str = "idle-svc",
    status: str = "RUNNING",
    service_id: str = "0123456789abcdef0123456789abcdef",
) -> dict[str, Any]:
    return {
        "ServiceName": name,
        "ServiceId": service_id,
        "ServiceArn": f"arn:aws:apprunner:us-east-1:123456789012:service/{name}/{service_id}",
        "Status": status,
    }


# --------------------------------------------------------------------------- #
# C1 / H1 — real CloudWatch dimensions + counted dollar
# --------------------------------------------------------------------------- #
def test_idle_service_counted_dollar_end_to_end() -> None:
    svc = _svc(name="idle", service_id="sid1")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB", "Cpu": "1 vCPU"}})
    cw = _FakeCloudWatch(requests_sum=0.0)  # idle: a 0-sum datapoint exists
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    expected = 2 * APP_RUNNER_MEM_GB_HOURLY * HOURS_PER_MONTH  # 2 GB x 0.007 x 730 = 10.22
    assert findings.total_recommendations == 1
    assert findings.total_monthly_savings == pytest.approx(round(expected, 2), abs=0.001)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(10.22, abs=0.01)
    # AuditBasis records the live-validated rate/SKU + the metric evidence.
    basis = rec["AuditBasis"]
    assert basis["rate_per_gb_hour"] == APP_RUNNER_MEM_GB_HOURLY
    assert "USE1-AppRunner-Provisioned-GB-hours" in basis["rate_source"]
    assert "ServiceName+ServiceID" in basis["evidence"]
    # No fabricated permission/warn noise on the happy path.
    assert ctx.permissions == [] and ctx.warnings == []


def test_cloudwatch_uses_servicename_and_serviceid_dimensions() -> None:
    """The invalid 'Service' dimension is replaced by ServiceName + ServiceID."""
    svc = _svc(name="idle", service_id="abc123")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw)

    AppRunnerModule().scan(ctx)

    assert len(cw.calls) == 1
    call = cw.calls[0]
    by_name = {d["Name"]: d["Value"] for d in call["Dimensions"]}
    assert by_name == {"ServiceName": "idle", "ServiceID": "abc123"}
    assert "Service" not in by_name  # the old no-op dimension name is gone
    assert call["Namespace"] == "AWS/AppRunner"
    assert call["MetricName"] == "Requests"
    # 30-day lookback window in seconds.
    assert call["Period"] == 30 * 86400


def test_pricing_multiplier_applied_once() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw, pricing_multiplier=1.5)

    findings = AppRunnerModule().scan(ctx)

    base = 2 * APP_RUNNER_MEM_GB_HOURLY * HOURS_PER_MONTH
    assert findings.total_monthly_savings == pytest.approx(round(base * 1.5, 2), abs=0.001)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["AuditBasis"]["region_multiplier"] == 1.5


# --------------------------------------------------------------------------- #
# L2 — unresolvable Memory config warns + $0 advisory (no fabricated 2 GB dollar)
# --------------------------------------------------------------------------- #
def test_missing_memory_warns_and_emits_zero_advisory() -> None:
    """An idle service whose config has no Memory key must NOT fabricate a 2 GB dollar."""
    svc = _svc(name="no-mem", service_id="sid-nm")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Cpu": "1 vCPU"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    # Rec still renders, but as a $0 advisory — nothing summed into the headline.
    assert findings.total_recommendations == 1
    assert findings.total_monthly_savings == 0.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "AuditBasis" not in rec  # never priced
    assert ctx.warnings, "missing Memory config must surface as a warn"
    service, msg = ctx.warnings[0]
    assert service == "apprunner"
    assert "missing" in msg and "no-mem" in msg


def test_unparseable_memory_warns_and_emits_zero_advisory() -> None:
    """An idle service whose Memory string is unparseable abstains, not a 2 GB guess."""
    svc = _svc(name="bad-mem", service_id="sid-bm")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "bogus"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 1
    assert findings.total_monthly_savings == 0.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "AuditBasis" not in rec
    assert ctx.warnings, "unparseable Memory config must surface as a warn"
    service, msg = ctx.warnings[0]
    assert service == "apprunner"
    assert "unparseable" in msg and "bad-mem" in msg


# --------------------------------------------------------------------------- #
# Idle gating / fail-safe ordering
# --------------------------------------------------------------------------- #
def test_active_service_not_flagged() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=5000.0)
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    # describe_service is only called after idle is confirmed (efficiency + fail-safe).
    assert ar.describe_calls == []


def test_no_datapoints_abstains() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=None)  # genuine no-data
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert ctx.permissions == [] and ctx.warnings == []
    assert ar.describe_calls == []  # abstained before pricing


def test_non_running_service_skipped() -> None:
    svc = _svc(name="paused", status="PAUSED")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert cw.calls == []  # no CW read for a non-running service


def test_fast_mode_skips_cloudwatch_and_emits_nothing() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    # If the shim touches CloudWatch in fast mode, this raises.
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _ctx(ar, cw, fast_mode=True)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert cw.calls == []
    assert ar.describe_calls == []


# --------------------------------------------------------------------------- #
# H2 — error classification, never swallowed, never a fabricated dollar
# --------------------------------------------------------------------------- #
def test_cloudwatch_access_denied_is_permission_issue_no_dollar() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(
        error=Exception("AccessDeniedException: not authorized to cloudwatch:GetMetricStatistics")
    )
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0  # no fabricated counted dollar
    assert ctx.permissions, "CloudWatch AccessDenied must be a permission_issue"
    assert ctx.permissions[0][0] == "apprunner"
    assert ctx.warnings == []


def test_cloudwatch_throttle_is_warning_no_dollar() -> None:
    svc = _svc()
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(error=Exception("ThrottlingException: Rate exceeded"))
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert ctx.warnings, "A throttle must surface as a warn"
    assert ctx.permissions == []  # not a permission gap


def test_describe_service_error_classified_and_abstains() -> None:
    """An idle service whose config read is denied is NOT counted as a delete."""
    svc = _svc(name="idle", service_id="id9")
    arn = svc["ServiceArn"]
    ar = _FakeAppRunner(
        [svc],
        describe_errors={arn: Exception("AccessDeniedException: apprunner:DescribeService")},
    )
    cw = _FakeCloudWatch(requests_sum=0.0)  # idle confirmed
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert ar.describe_calls == [arn]
    assert ctx.permissions, "describe_service AccessDenied must be a permission_issue"
    assert ctx.permissions[0][0] == "apprunner"


def test_list_services_access_denied_is_permission_issue() -> None:
    ar = _FakeAppRunner([], list_error=Exception("AccessDeniedException: apprunner:ListServices"))
    cw = _FakeCloudWatch()
    ctx = _ctx(ar, cw)

    findings = AppRunnerModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert ctx.permissions, "ListServices AccessDenied must be a permission_issue"
    assert ctx.permissions[0][0] == "apprunner"


def test_list_services_transient_is_warning() -> None:
    ar = _FakeAppRunner([], list_error=Exception("InternalServiceException: try later"))
    cw = _FakeCloudWatch()
    ctx = _ctx(ar, cw)

    AppRunnerModule().scan(ctx)

    assert ctx.warnings, "A non-permission failure must surface as a warn"
    assert ctx.permissions == []


# --------------------------------------------------------------------------- #
# Pure-logic unit tests on the shim helpers
# --------------------------------------------------------------------------- #
def test_monthly_requests_sums_datapoints() -> None:
    cw = _FakeCloudWatch(requests_sum=42.0)
    ctx = _ctx(_FakeAppRunner([]), cw)
    assert shim._monthly_requests(cw, "svc", "sid", ctx) == 42.0
    # Dimensions wired correctly on the helper itself.
    by_name = {d["Name"]: d["Value"] for d in cw.calls[0]["Dimensions"]}
    assert by_name == {"ServiceName": "svc", "ServiceID": "sid"}


def test_monthly_requests_none_on_no_data() -> None:
    cw = _FakeCloudWatch(requests_sum=None)
    ctx = _ctx(_FakeAppRunner([]), cw)
    assert shim._monthly_requests(cw, "svc", "sid", ctx) is None
    assert ctx.permissions == [] and ctx.warnings == []


def test_monthly_requests_none_and_classifies_on_error() -> None:
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: denied"))
    ctx = _ctx(_FakeAppRunner([]), cw)
    assert shim._monthly_requests(cw, "svc", "sid", ctx) is None
    assert ctx.permissions, "a failed read must be classified, not swallowed"


def test_shim_emits_idle_rec_with_real_config() -> None:
    svc = _svc(name="idle", service_id="sid")
    ar = _FakeAppRunner([svc], configs={svc["ServiceArn"]: {"Memory": "2 GB"}})
    cw = _FakeCloudWatch(requests_sum=0.0)
    ctx = _ctx(ar, cw)

    result = shim.get_enhanced_apprunner_checks(ctx)

    recs = result["recommendations"]
    assert len(recs) == 1
    assert recs[0]["CheckCategory"] == "Idle Service"
    assert recs[0]["InstanceConfiguration"] == {"Memory": "2 GB"}
    assert recs[0]["MonthlyRequests"] == 0
