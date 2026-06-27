"""Live-pricing adapter for WorkSpaces."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import mark_zero_savings_advisory
from services.workspaces import (
    WORKSPACE_AUTOSTOP_PRICING,
    WORKSPACE_BUNDLE_MONTHLY,
    WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_workspaces_checks,
)

# us-east-1 rates validated against the live AWS Pricing API on 2026-06-27 (see
# WORKSPACE_BUNDLE_MONTHLY / WORKSPACE_AUTOSTOP_PRICING in services/workspaces.py).
_RATE_SOURCE: str = "AWS Pricing API AmazonWorkSpaces us-east-1 (validated 2026-06-27)"


def _compute_type_of(rec: dict[str, Any]) -> str:
    """Resolve the WorkSpace ComputeType from a rec (C3 propagated field)."""
    compute_type = rec.get("ComputeType", "")
    if not compute_type:
        compute_type = rec.get("WorkspaceProperties", {}).get("ComputeTypeName", "")
    return (compute_type or "").upper()


class WorkspacesModule(BaseServiceModule):
    """ServiceModule adapter for WorkSpaces. Live-pricing savings strategy."""

    key: str = "workspaces"
    cli_aliases: tuple[str, ...] = ("workspaces",)
    display_name: str = "WorkSpaces"
    # The shim reads AWS/WorkSpaces UserConnected to size the AlwaysOn->AutoStop
    # lever, and short-circuits that read when ctx.fast_mode is set.
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for WorkSpaces scanning."""
        return ("workspaces", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan WorkSpaces virtual desktops for cost optimization opportunities.

        Each lever is priced from the actual bundle ComputeType (C3) using the
        AWS-published AlwaysOn / AutoStop price tables (region-scaled once):

        - Billing Mode (C2): AlwaysOn->AutoStop saving = AlwaysOn -
          (AutoStop fee + hourly x measured session hours). Without a CloudWatch
          session-hours metric, or when AutoStop is not cheaper at the measured
          usage, the rec is a $0 advisory rather than a fabricated flat factor.
        - Unused WorkSpaces (C5): an ALWAYS_ON WorkSpace bills the full monthly
          cost even when idle, so termination saves the full bundle price; a
          STOPPED AUTO_STOP WorkSpace already bills only the small fixed monthly
          fee, so termination saves only that residual fee. An unknown running
          mode abstains (a termination rec must not assert a saving it cannot
          prove).
        - Bundle Rightsizing: the shim already computed a concrete current->target
          price delta — counted as-is.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an enhanced_checks SourceBlock.
        """
        result = get_enhanced_workspaces_checks(ctx)
        recs = result.get("recommendations", [])
        multiplier = ctx.pricing_multiplier

        savings = 0.0
        for rec in recs:
            category = rec.get("CheckCategory", "")
            compute_type = _compute_type_of(rec)
            always_on = WORKSPACE_BUNDLE_MONTHLY.get(compute_type, 0.0)

            if category == "Bundle Rightsizing":
                # Shim already computed the concrete current->target bundle delta
                # (region-scaled). Count it as-is; single-source the display string.
                amount = rec.get("EstimatedSavingsAmount")
                if isinstance(amount, (int, float)) and amount > 0:
                    rec["EstimatedMonthlySavings"] = round(float(amount), 2)
                    rec["EstimatedSavings"] = f"${float(amount):.2f}/month"
                    savings += float(amount)
                else:
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["EstimatedSavings"] = "$0.00/month — advisory: no bundle price delta"
                continue

            if category == "Billing Mode Optimization":
                self._price_billing_mode(rec, compute_type, always_on, multiplier)
                savings += float(rec.get("EstimatedMonthlySavings", 0.0) or 0.0)
                continue

            if category == "Unused WorkSpaces":
                self._price_unused(rec, compute_type, always_on, multiplier)
                savings += float(rec.get("EstimatedMonthlySavings", 0.0) or 0.0)
                continue

            # Unknown category — no concrete saving.
            rec["EstimatedMonthlySavings"] = 0.0

        # Count hygiene: $0 (metric-gated / abstained) recs are shown but excluded
        # from the counted total AND the rec-count headline (mirrors lambda_svc).
        mark_zero_savings_advisory(recs, lambda r: float(r.get("EstimatedMonthlySavings", 0) or 0))
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="WorkSpaces",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
        )

    @staticmethod
    def _advisory(rec: dict[str, Any], reason: str) -> None:
        """Render a rec as a $0 advisory (shown, not counted)."""
        rec["EstimatedMonthlySavings"] = 0.0
        rec["EstimatedSavings"] = f"$0.00/month — advisory: {reason}"

    def _price_billing_mode(
        self, rec: dict[str, Any], compute_type: str, always_on: float, multiplier: float
    ) -> None:
        """C2: price AlwaysOn->AutoStop on measured session hours (else $0 advisory)."""
        autostop = WORKSPACE_AUTOSTOP_PRICING.get(compute_type)
        measured_hours = rec.get("MeasuredMonthlyHours")
        if always_on <= 0 or autostop is None:
            self._advisory(
                rec, f"no validated AlwaysOn/AutoStop price for bundle {compute_type or 'UNKNOWN'}"
            )
            return
        if measured_hours is None:
            self._advisory(
                rec,
                "requires AWS/WorkSpaces UserConnected session-hours metric to project AutoStop cost",
            )
            return
        fee, hourly = autostop
        hours = float(measured_hours)
        projected_autostop = fee + hourly * hours
        delta = always_on - projected_autostop
        if delta <= 0:
            self._advisory(
                rec,
                f"at {hours:.0f} connected hrs/mo AutoStop (${projected_autostop:.2f}) is not "
                f"cheaper than AlwaysOn (${always_on:.2f})",
            )
            return
        saving = delta * multiplier
        rec["EstimatedMonthlySavings"] = round(saving, 2)
        rec["EstimatedSavings"] = f"${saving:.2f}/month"
        rec["AuditBasis"] = {
            "compute_type": compute_type,
            "always_on_monthly": always_on,
            "autostop_fee_monthly": fee,
            "autostop_hourly": hourly,
            "measured_monthly_hours": round(hours, 1),
            "region_multiplier": round(multiplier, 4),
            "metric": "AWS/WorkSpaces UserConnected hours over 30d, scaled to 730h",
            "formula": "(always_on - (autostop_fee + autostop_hourly x hours)) x region_multiplier",
            "rate_source": _RATE_SOURCE,
        }

    def _price_unused(
        self, rec: dict[str, Any], compute_type: str, always_on: float, multiplier: float
    ) -> None:
        """C5: price termination by running mode (full AlwaysOn vs residual AutoStop fee)."""
        running_mode = rec.get("RunningMode")
        if always_on <= 0:
            self._advisory(rec, f"no validated price for bundle {compute_type or 'UNKNOWN'}")
            return
        if running_mode == "ALWAYS_ON":
            saving = always_on * multiplier
            basis = "ALWAYS_ON bills the full monthly cost even when not in use"
            counted_value: float | None = always_on
        elif running_mode == "AUTO_STOP":
            autostop = WORKSPACE_AUTOSTOP_PRICING.get(compute_type)
            if autostop is None:
                self._advisory(
                    rec, f"no validated AutoStop fee for bundle {compute_type or 'UNKNOWN'}"
                )
                return
            fee = autostop[0]
            saving = fee * multiplier
            basis = "AUTO_STOP already bills only the fixed monthly fee (root+user volumes)"
            counted_value = fee
        else:
            # Fail safe: a termination rec with an unknown running mode must not
            # assert a near-full-cost saving it cannot prove.
            self._advisory(rec, "running mode unknown; termination saving not asserted")
            return
        if saving <= 0:
            self._advisory(rec, basis)
            return
        rec["EstimatedMonthlySavings"] = round(saving, 2)
        rec["EstimatedSavings"] = f"${saving:.2f}/month"
        rec["AuditBasis"] = {
            "compute_type": compute_type,
            "running_mode": running_mode,
            "counted_monthly": counted_value,
            "counted_basis": basis,
            "region_multiplier": round(multiplier, 4),
            "rate_source": _RATE_SOURCE,
        }
