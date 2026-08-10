"""BR-6 — Bedrock custom (fine-tuned) model storage.

Every custom model bills for as long as it exists, whether or not anything
invokes it, and nothing measured that. Accounts accumulate abandoned
fine-tunes, so the aggregate is worth surfacing.

Rate validated against the live Pricing API 2026-08-10: AmazonBedrock
publishes 22 ``*-Customization-Storage`` SKUs (Nova Pro, Nova Canvas, Titan
Text Express, …) and every one carries **$1.95 with unit Model/month**.

Advisory, deliberately: deleting a fine-tuned model destroys an artifact that
cost money to produce, and Bedrock supports on-demand inference for custom
models — so the absence of a Provisioned Throughput no longer implies the
model cannot be invoked. The figure renders in ``PotentialMonthlySavings``
without the headline claiming it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# --------------------------------------------------------------------------- #
# BR-6 — custom-model storage, an unconditional charge nothing measured.
#
# $1.95 per Model/month, validated against the live Pricing API: AmazonBedrock
# publishes 22 "*-Customization-Storage" SKUs and every one carries that rate
# with unit "Model/month".
# --------------------------------------------------------------------------- #
def _bedrock_ctx_with_models(models, *, list_error=None):
    from types import SimpleNamespace

    class _Bedrock:
        def get_paginator(self, name):
            if name == "list_custom_models":
                if list_error is not None:
                    raise list_error
                return SimpleNamespace(paginate=lambda **kw: [{"modelSummaries": models}])
            return SimpleNamespace(paginate=lambda **kw: [{"provisionedModelSummaries": []}])

        def list_custom_models(self, **kw):
            if list_error is not None:
                raise list_error
            return {"modelSummaries": models}

        def list_provisioned_model_throughputs(self, **kw):
            return {"provisionedModelSummaries": []}

    ctx = SimpleNamespace(
        pricing_engine=None,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        warnings=[],
        permissions=[],
    )
    clients = {"bedrock": _Bedrock(), "bedrock-agent": None, "cloudwatch": None}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    return ctx


def _custom_recs(findings):
    return list(findings.sources["custom_model_storage"].recommendations)


def test_custom_model_storage_is_surfaced_with_the_live_rate() -> None:
    from services.adapters.bedrock import BedrockModule

    models = [
        {"modelName": "support-tuned-v3", "modelArn": "arn:aws:bedrock:::custom-model/x", "baseModelName": "Titan"},
        {"modelName": "old-experiment", "modelArn": "arn:aws:bedrock:::custom-model/y"},
    ]
    findings = BedrockModule().scan(_bedrock_ctx_with_models(models))
    recs = _custom_recs(findings)
    assert len(recs) == 2
    assert all(r["PotentialMonthlySavings"] == pytest.approx(1.95) for r in recs)
    assert {r["custom_model_name"] for r in recs} == {"support-tuned-v3", "old-experiment"}


def test_custom_model_storage_is_advisory_and_says_why() -> None:
    """The charge is unconditional, but deleting a fine-tune destroys an
    artifact and Bedrock supports on-demand custom inference - so the absence
    of a Provisioned Throughput does not prove the model is unused."""
    from services.adapters.bedrock import BedrockModule

    findings = BedrockModule().scan(_bedrock_ctx_with_models([{"modelName": "m1"}]))
    rec = _custom_recs(findings)[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    assert "on-demand inference" in rec["AuditBasis"]["reason"]


def test_custom_model_rate_is_region_scaled() -> None:
    from services.adapters.bedrock import BedrockModule

    ctx = _bedrock_ctx_with_models([{"modelName": "m1"}])
    ctx.pricing_multiplier = 1.08
    rec = _custom_recs(BedrockModule().scan(ctx))[0]
    assert rec["PotentialMonthlySavings"] == pytest.approx(1.95 * 1.08, abs=0.01)


def test_denied_custom_model_enumeration_is_classified_not_read_as_none() -> None:
    """E1 — a denied ListCustomModels looks exactly like an account with no
    custom models unless it is classified."""
    from services.adapters.bedrock import BedrockModule

    ctx = _bedrock_ctx_with_models([], list_error=Exception("AccessDeniedException"))
    findings = BedrockModule().scan(ctx)
    assert _custom_recs(findings) == []
    assert ctx.permissions or ctx.warnings


def test_account_with_no_custom_models_emits_nothing() -> None:
    from services.adapters.bedrock import BedrockModule

    findings = BedrockModule().scan(_bedrock_ctx_with_models([]))
    assert _custom_recs(findings) == []
