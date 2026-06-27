"""DPU-based pricing adapter for Glue."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.glue import GLUE_OPTIMIZATION_DESCRIPTIONS, get_enhanced_glue_checks

# Glue per-DPU-hour rate (us-east-1, $0.44/DPU-hour). Validated live against the
# AWS Pricing API 2026-06-27: usagetype ``USE1-DEVED-DPU-Hour`` (dev endpoints,
# SKU H5SXAYMQH485TMM7, "$0.44 per Data Processing Unit-Hour for AWS Glue
# development endpoints") and ``USE1-ETL-DPU-Hour`` both = $0.4400/DPU-Hour.
# Region-scaled via ``ctx.pricing_multiplier`` at the emit site (module constant).
GLUE_DPU_HOURLY: float = 0.44

# A READY dev endpoint provisions DPUs 24/7 until deleted, so its monthly cost
# is the full month of DPU-hours.
DEV_ENDPOINT_MONTHLY_HOURS: float = 730.0

# AWS default DPU allocation for a dev endpoint when neither a WorkerType
# footprint nor a legacy NumberOfNodes is reported.
DEFAULT_DEV_ENDPOINT_DPU: float = 5.0

# WorkerType -> DPU-per-worker multiplier. Glue prices the same $0.44/DPU-hour
# but a worker is NOT one DPU for the larger types: treating NumberOfWorkers as
# a raw DPU count under-prices G.2X/G.4X/G.8X by 2-8x and over-prices G.025X
# ~16x (glue C1/H3). Dev endpoints support Standard/G.1X/G.2X; the rest are
# included so the multiplier is correct if a footprint reports them.
WORKER_TYPE_DPU: dict[str, float] = {
    "Standard": 1.0,
    "G.025X": 0.25,
    "G.1X": 1.0,
    "G.2X": 2.0,
    "G.4X": 4.0,
    "G.8X": 8.0,
    "G.12X": 12.0,
    "G.16X": 16.0,
    "Z.2X": 2.0,
}

_DEV_ENDPOINT_CATEGORY: str = "Glue Dev Endpoints"


def _dev_endpoint_dpu(rec: dict[str, Any]) -> tuple[float, str]:
    """Resolve a READY dev endpoint's provisioned DPU footprint.

    Prefer the explicit ``WorkerType`` x ``NumberOfWorkers`` footprint (applying
    the WorkerType->DPU multiplier), then the legacy ``NumberOfNodes`` (already a
    DPU count), and finally the AWS default allocation (5 DPU) when neither is
    reported.

    Args:
        rec: A dev-endpoint recommendation dict from the Glue shim.

    Returns:
        ``(dpu_count, basis)`` where ``basis`` names the source used
        (``"worker_type"`` / ``"number_of_nodes"`` / ``"default_5_dpu"``).
    """
    worker_type = rec.get("WorkerType")
    num_workers = rec.get("NumberOfWorkers")
    if worker_type and num_workers:
        mult = WORKER_TYPE_DPU.get(str(worker_type))
        if mult is not None:
            try:
                return float(num_workers) * mult, "worker_type"
            except (TypeError, ValueError):
                pass

    num_nodes = rec.get("NumberOfNodes")
    if num_nodes:
        try:
            return float(num_nodes), "number_of_nodes"
        except (TypeError, ValueError):
            pass

    return DEFAULT_DEV_ENDPOINT_DPU, "default_5_dpu"


class GlueModule(BaseServiceModule):
    """ServiceModule adapter for AWS Glue. DPU-based savings strategy."""

    key: str = "glue"
    cli_aliases: tuple[str, ...] = ("glue",)
    display_name: str = "Glue"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Glue scanning."""
        return ("glue",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Glue jobs, dev endpoints, and crawlers for cost optimization.

        Consults the glue service module. A READY dev endpoint is the one
        concrete, account-specific Glue saving: it provisions DPUs 24/7 until
        deleted, so it is **counted** at its own DPU footprint x $0.44/DPU-hour
        x 730 hr (glue H2), single-sourcing the displayed string from the
        counted dollar with an ``AuditBasis``. Job-rightsizing levers remain a
        $0 advisory: the realized saving is run-volume dependent and needs
        aggregate DPU-hours from ``glue.get_job_runs`` to quantify (glue C2), so
        no dollar is fabricated from a fixed 160-hour assumption.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        result = get_enhanced_glue_checks(ctx)
        recs = result.get("recommendations", [])

        multiplier = float(getattr(ctx, "pricing_multiplier", 1.0) or 1.0)

        savings = 0.0
        for rec in recs:
            if rec.get("CheckCategory") == _DEV_ENDPOINT_CATEGORY:
                # glue H2: price the endpoint from its own DPU footprint and
                # COUNT it. Counted == rendered: the EstimatedSavings string is
                # derived from the same number summed into total_monthly_savings.
                dpu, basis = _dev_endpoint_dpu(rec)
                monthly = round(dpu * GLUE_DPU_HOURLY * DEV_ENDPOINT_MONTHLY_HOURS * multiplier, 2)
                rec["Counted"] = True
                rec["EstimatedMonthlySavings"] = monthly
                rec["EstimatedSavings"] = f"${monthly:,.2f}/month"
                rec["AuditBasis"] = {
                    "rate_per_dpu_hour": GLUE_DPU_HOURLY,
                    "dpu_count": dpu,
                    "dpu_basis": basis,
                    "worker_type": rec.get("WorkerType"),
                    "number_of_workers": rec.get("NumberOfWorkers"),
                    "monthly_hours": DEV_ENDPOINT_MONTHLY_HOURS,
                    "pricing_multiplier": round(multiplier, 4),
                    "region": "us-east-1 baseline, region-scaled via pricing_multiplier",
                    "metric_window": "n/a — dev endpoint bills continuously while READY",
                    "formula": "dpu x $0.44/DPU-hr x 730 hr x pricing_multiplier",
                    "rate_source": "AWS Pricing API USE1-DEVED-DPU-Hour (validated 2026-06-27)",
                }
                savings += monthly
                continue

            # Job-rightsizing levers: $0 advisory (glue C2). Resolve DPU count by
            # truthiness so a present-but-zero ``MaxCapacity`` (the shim always
            # injects it, default 0) falls through to ``NumberOfWorkers`` for
            # modern worker-based jobs.
            raw_dpu = rec.get("MaxCapacity") or rec.get("NumberOfWorkers")
            if raw_dpu is None:
                rec["Counted"] = False
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "MaxCapacity / NumberOfWorkers not set on rec"
                continue
            try:
                dpu_count = float(raw_dpu)
            except (TypeError, ValueError):
                rec["Counted"] = False
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "DPU count not numeric"
                continue

            # ASSUMED_MONTHLY_DPU_HOURS (160) was multiplied into every counted
            # saving with no run history — fabricating a dollar for jobs that may
            # run rarely. Realized saving needs aggregate DPU-hours from
            # ``glue.get_job_runs`` (Sum ExecutionTime x DPU over a trailing
            # window); absent that, demote to $0 advisory (glue C2).
            rec["Counted"] = False
            rec["EstimatedMonthlySavings"] = 0.0
            rec["EstimatedSavings"] = (
                "$0.00/month — advisory: rightsize Glue DPU allocation; realized "
                "saving is run-volume dependent and needs glue.get_job_runs "
                "DPU-hour history to quantify"
            )
            rec["AuditBasis"] = {
                "rate_per_dpu_hour": GLUE_DPU_HOURLY,
                "dpu_count": dpu_count,
                "unmeasured_inputs": ["monthly_dpu_hours_from_get_job_runs"],
                "reason": "rejected 160-hr assumption; advisory per cost-scope rule",
            }

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        # Count hygiene: $0 advisory recs (Counted=False) are rendered but must
        # NOT inflate the rec headline (mirrors services/_savings.mark_zero_savings_advisory
        # and the lambda/mediastore adapters).
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="Glue",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=GLUE_OPTIMIZATION_DESCRIPTIONS,
        )
