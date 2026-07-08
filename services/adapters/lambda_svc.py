"""Multi-source adapter for Lambda with Cost Hub and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import mark_zero_savings_advisory
from services.advisor import get_lambda_compute_optimizer_recommendations
from services.lambda_svc import (
    LAMBDA_OPTIMIZATION_DESCRIPTIONS,
    PC_UTIL_LOOKBACK_DAYS as _PC_UTIL_WINDOW_DAYS,
    get_enhanced_lambda_checks,
)

# AWS Lambda Provisioned Concurrency rates (us-east-1, verified via Pricing API
# 2026-06-26). PC bills 24/7 for allocated GB-seconds at this dedicated rate,
# distinct from on-demand x86 duration ($0.0000166667/GB-s) and on-demand ARM
# duration ($0.0000133334/GB-s). arm64 PC is ~20% cheaper than x86 PC, so the
# function's architecture selects the rate. Both are region-scaled via
# pricing_multiplier at the per-rec emit site below.
#   x86 PC  : SKU BMKCD2ZCEYKTYYCB  $0.0000041667/GB-s
#   arm64 PC: SKU MV7PTBS82AVCMXJJ  $0.0000033334/GB-s
_LAMBDA_PC_PRICE_PER_GB_SEC: float = 0.0000041667
_LAMBDA_PC_PRICE_PER_GB_SEC_ARM: float = 0.0000033334

_HOURS_PER_MONTH: int = 730
_SECONDS_PER_HOUR: int = 3600


def _normalize_lambda_fn_name(value: str) -> str:
    """Reduce a Lambda identifier to its bare function name for cross-source dedup.

    Handles a full ARN (``arn:aws:lambda:region:acct:function:NAME``), a
    qualified ARN whose ``:version``/``:alias`` suffix must be stripped
    (``...:function:NAME:PROD`` / ``...:function:NAME:1``), and a bare name
    (optionally ``NAME:alias``). Returns ``""`` for an empty input. Used so Cost
    Hub, Compute Optimizer, and the enhanced heuristics all key on the same name
    — mirrors ``_normalize_lambda_co_rec``'s ARN handling in services/advisor.py.
    """
    if not value:
        return ""
    if ":function:" in value:
        return value.split(":function:")[-1].split(":")[0]
    return value.split(":")[0]


class LambdaModule(BaseServiceModule):
    """ServiceModule adapter for Lambda. Multi-source savings strategy."""

    key: str = "lambda"
    cli_aliases: tuple[str, ...] = ("lambda",)
    display_name: str = "Lambda"
    # Shim hits cloudwatch.get_metric_statistics for ARM-migration analysis
    # (Invocations) and Provisioned Concurrency utilization per function; flag
    # must reflect actual usage.
    requires_cloudwatch: bool = True
    # The shim short-circuits its CloudWatch reads when ctx.fast_mode is set
    # (ARM/PC savings degrade to advisory), so declare that it reads the flag.
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Lambda scanning."""
        return ("lambda", "compute-optimizer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Lambda functions for cost optimization opportunities.

        Consults Cost Optimization Hub, Compute Optimizer (memorySize
        rightsizing), and enhanced Lambda checks. Cost-Hub and CO savings
        come from AWS APIs (already region-correct). Enhanced-check savings
        for Excessive Memory and ARM Migration scale with the function's
        measured weekly invocations (from CW); for Provisioned Concurrency,
        the allocated GB-seconds bill 24/7 so the formula uses the full
        month at the PC rate, not the on-demand x86 rate.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cost_optimization_hub, compute_optimizer,
            and enhanced_checks sources.
        """

        cost_hub_recs = ctx.cost_hub_splits.get("lambda", [])
        co_recs = get_lambda_compute_optimizer_recommendations(ctx)
        enhanced_result = get_enhanced_lambda_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        # The advisor returns a synthetic $0 "enable Compute Optimizer" placeholder
        # (ResourceId="compute-optimizer-service") when CO is not opted in. That is
        # an informational signal, not a cost recommendation — surface it as a
        # warning instead of a $0-savings finding that inflates the count (mirrors
        # EC2Module / RDSModule).
        if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_recs):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — Lambda memory-rightsizing "
                "recommendations from Compute Optimizer are unavailable (enable it "
                "for additional savings detection).",
                service="lambda",
            )
        co_recs = [r for r in co_recs if r.get("ResourceId") != "compute-optimizer-service"]

        # Dedupe FIRST so the savings sum cannot count a function twice.
        # If a function appears in Cost Hub, keep the Cost Hub version and
        # drop it from enhanced_checks. (CO recs surface independently
        # because they carry memorySize rightsizing the others don't.)
        # All sources are keyed on the NORMALIZED bare function name so a
        # qualified CoH ARN ("...:function:NAME:PROD") still matches CO's bare
        # name — see _normalize_lambda_fn_name.
        seen_functions: set[str] = set()
        for rec in cost_hub_recs:
            raw = rec.get("resourceArn") or rec.get("resourceId") or rec.get("FunctionName", "")
            fn = _normalize_lambda_fn_name(raw)
            if fn:
                seen_functions.add(fn)

        # Compute Optimizer is lower authority than Cost Hub. A function that
        # appears in BOTH gets the same memory-rightsizing lever from each
        # source — keep the Cost Hub version and drop the CO duplicate so the
        # saving is never counted twice (mirrors EC2/EBS dedupe_by_authority).
        deduped_co: list[dict[str, Any]] = []
        for rec in co_recs:
            fn = _normalize_lambda_fn_name(rec.get("resource_name", "") or rec.get("FunctionName", ""))
            if fn and fn in seen_functions:
                continue
            deduped_co.append(rec)
            if fn:
                seen_functions.add(fn)
        co_recs = deduped_co

        deduped_enhanced: list[dict[str, Any]] = []
        for rec in enhanced_recs:
            fn = _normalize_lambda_fn_name(rec.get("FunctionName", ""))
            if fn not in seen_functions:
                deduped_enhanced.append(rec)
        enhanced_recs = deduped_enhanced

        # Cost Hub recs arrive with only AWS's camelCase ``estimatedMonthlySavings``
        # and no rendered string. The reporter's per-rec card reads PascalCase
        # ``EstimatedSavings`` (and ``_group_counted_savings`` reads PascalCase
        # ``EstimatedMonthlySavings``), so an un-normalized CoH rec renders the
        # literal "Cost optimization" placeholder while its dollar is silently
        # summed — counted != rendered. Normalize each CoH rec the way EBS/EC2 do
        # so the counted $ also appears on the card. The camelCase key stays the
        # source of truth for the sum below (no double count).
        for rec in cost_hub_recs:
            amt = float(rec.get("estimatedMonthlySavings", 0.0) or 0.0)
            rec["EstimatedSavings"] = f"${amt:.2f}/month"
            rec["EstimatedMonthlySavings"] = round(amt, 2)
            rec.setdefault("Counted", True)

        hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)

        formula_savings = 0.0
        for rec in enhanced_recs:
            mem_mb = rec.get("MemorySize", 256)
            category = rec.get("CheckCategory", "")
            mem_gb = mem_mb / 1024

            if "Excessive Memory" in category:
                # Memory-rightsizing savings depend on actual GB-seconds
                # consumed. Without metric backing, emit 0 + warn so the
                # number is honest rather than inflated by a 730-hour
                # 24/7 assumption.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "requires actual invocation seconds (CW Duration metric)"
                continue
            if "Provisioned Concurrency" in category:
                # Provisioned Concurrency bills 24/7 for allocated GB-seconds at
                # the dedicated PC rate (architecture-specific), NOT the on-demand
                # duration rate. The realizable saving is the *unused* fraction of
                # the allocation, derived from peak ProvisionedConcurrencyUtilization
                # (1 − max_util): we never deprovision below observed peak need.
                # Without a utilization metric (fast mode / no datapoints) the saving
                # cannot be proven, so emit $0 advisory rather than a flat haircut.
                arch = rec.get("Architecture", "x86_64")
                pc_rate = (
                    _LAMBDA_PC_PRICE_PER_GB_SEC_ARM if arch == "arm64" else _LAMBDA_PC_PRICE_PER_GB_SEC
                )
                pc_count = rec.get("ProvisionedConcurrency", 1)
                allocation = (
                    mem_gb * pc_rate * _HOURS_PER_MONTH * _SECONDS_PER_HOUR * pc_count
                )
                max_util = rec.get("MaxUtilization")
                if max_util is None:
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["PricingWarning"] = "requires ProvisionedConcurrencyUtilization metric"
                    continue
                unused_fraction = max(0.0, 1.0 - float(max_util))
                pc_savings = allocation * unused_fraction * ctx.pricing_multiplier
                rec["EstimatedMonthlySavings"] = round(pc_savings, 2)
                rec["AuditBasis"] = {
                    "rate_per_gb_sec": pc_rate,
                    "architecture": arch,
                    "region_multiplier": round(ctx.pricing_multiplier, 4),
                    "metric": f"ProvisionedConcurrencyUtilization max over {_PC_UTIL_WINDOW_DAYS}d",
                    "max_utilization": round(float(max_util), 4),
                    "provisioned_concurrency": pc_count,
                    "memory_gb": round(mem_gb, 4),
                    "formula": (
                        "mem_gb x rate x 730h x 3600s x pc_count x (1 - max_util) x region_multiplier"
                    ),
                }
                formula_savings += pc_savings
                continue
            if "ARM Migration" in category:
                # ARM savings need actual invocation time. Shim collects
                # WeeklyInvocations; without per-invocation Duration we
                # cannot derive GB-seconds. Emit 0 + warn.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "requires actual GB-seconds (CW Invocations + Duration)"
                continue

        # formula_savings already carries pricing_multiplier per PC rec above —
        # do NOT scale again here.

        # Memory/ARM recs that could not be quantified (no CW Duration/GB-seconds)
        # emit $0 — they are metric-gated nudges, not counted opportunities. Mark
        # them advisory so they are shown but excluded from the counted total
        # (mirrors the metric-gated NAT/monitoring advisories).
        mark_zero_savings_advisory(
            enhanced_recs, lambda r: float(r.get("EstimatedMonthlySavings", 0) or 0)
        )

        # AWS Compute Optimizer returns region-priced savings; do NOT
        # multiply by pricing_multiplier here (the helper applies it once
        # inside _normalize_lambda_co_rec — see services/advisor.py:255).
        co_savings = sum(rec.get("estimatedMonthlySavings", 0.0) for rec in co_recs)

        savings = hub_savings + formula_savings + co_savings

        # Active-commitment demotion: a Compute Savings Plan covers Lambda
        # duration cost (EC2-Instance SPs do not). Under an active Compute SP the
        # commitment bills regardless of memory-rightsizing, so the on-demand
        # CoH/Compute-Optimizer figures are not realizable — demote them to
        # advisory and drop their gross from the headline. No Compute SP → no
        # change.
        coverage = getattr(ctx, "commitment_coverage", None)
        if coverage is not None and coverage.covers_lambda():
            for rec in list(cost_hub_recs) + list(co_recs):
                gross = float(rec.get("estimatedMonthlySavings", 0.0) or 0.0)
                rec["Counted"] = False
                rec["AdvisoryEstimate"] = gross
                rec["CommitmentCoverageNote"] = (
                    "Covered by an active Compute Savings Plan; the on-demand "
                    f"${gross:,.2f}/mo is not realizable while the commitment bills — not counted."
                )
            savings = formula_savings

        total_recs = len(cost_hub_recs) + len(co_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="Lambda",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=LAMBDA_OPTIMIZATION_DESCRIPTIONS,
        )
