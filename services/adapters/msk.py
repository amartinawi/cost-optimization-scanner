"""Advisory adapter for MSK (broker/storage savings are utilization-gated $0)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.msk import MSK_OPTIMIZATION_DESCRIPTIONS, get_enhanced_msk_checks

HOURS_PER_MONTH: int = 730

# Provisioned MSK broker-storage list rate ($/GB-month). Validated us-east-1 via
# the AWS Pricing API (AmazonMSK, usagetype ``USE1-Kafka.Storage.GP2`` →
# "$0.10 per GB-Mo", publication 2026-04). It is a module constant (no dedicated
# PricingEngine method), so it is region-scaled at the call site via
# ``ctx.pricing_multiplier`` — unlike the broker rate, which comes region-correct
# from ``PricingEngine.get_msk_broker_hourly_price``.
MSK_STORAGE_RATE_PER_GB_MONTH: float = 0.10


def _current_cost_basis(ctx: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """Price the rec's real current MSK spend for a defensible $0 advisory.

    Consumes the fixed ``PricingEngine.get_msk_broker_hourly_price`` Broker-hours
    SKU for the broker leg (msk H1) and the rec's real per-broker
    ``BrokerStorageGB`` for the storage leg (msk H3). The storage leg is OMITTED
    when the volume size is unknown — never defaulted to a phantom 100 GB. No
    saving is asserted: MSK exposes no utilization / target-broker-size signal at
    scan time, so the realizable saving stays $0 (rejects the prior blanket 30%).

    Args:
        ctx: ScanContext with ``pricing_engine``, ``pricing_multiplier``, ``region``.
        rec: A source recommendation from ``get_enhanced_msk_checks``.

    Returns:
        A structured ``AuditBasis`` dict (new object; ``rec`` is not mutated).
    """
    basis: dict[str, Any] = {"region": getattr(ctx, "region", None)}
    pricing_engine = getattr(ctx, "pricing_engine", None)
    multiplier = getattr(ctx, "pricing_multiplier", 1.0)
    instance_type = rec.get("InstanceType")
    num_brokers = rec.get("NumberOfBrokerNodes")

    current_monthly_cost = 0.0
    priced_any_leg = False

    # Broker leg — H1: real Broker-hours rate from the (now fixed) live SKU path.
    if instance_type and num_brokers and pricing_engine is not None:
        hourly = pricing_engine.get_msk_broker_hourly_price(instance_type)
        broker_monthly = hourly * HOURS_PER_MONTH * num_brokers
        basis["broker_instance_type"] = instance_type
        basis["broker_count"] = num_brokers
        basis["broker_hourly_rate"] = round(hourly, 4)
        basis["broker_monthly_cost"] = round(broker_monthly, 2)
        current_monthly_cost += broker_monthly
        priced_any_leg = True

    # Storage leg — H3: real per-broker VolumeSize, or omit (no phantom 100 GB).
    storage_gb = rec.get("BrokerStorageGB")
    if storage_gb and num_brokers:
        storage_monthly = storage_gb * MSK_STORAGE_RATE_PER_GB_MONTH * num_brokers * multiplier
        basis["storage_gb_per_broker"] = storage_gb
        basis["storage_rate_per_gb_month"] = MSK_STORAGE_RATE_PER_GB_MONTH
        basis["storage_monthly_cost"] = round(storage_monthly, 2)
        current_monthly_cost += storage_monthly
        priced_any_leg = True
    else:
        basis["storage_leg"] = "omitted — per-broker VolumeSize unknown"

    if priced_any_leg:
        basis["current_monthly_cost"] = round(current_monthly_cost, 2)

    basis["realizable_monthly_savings"] = 0.0
    basis["unmeasured_inputs"] = ["broker_utilization", "target_broker_size"]
    basis["reason"] = (
        "Current spend priced from the live AmazonMSK Broker-hours SKU "
        "(get_msk_broker_hourly_price) + the $0.10/GB-mo provisioned-storage "
        "rate; the realizable saving is utilization-dependent and needs a target "
        "broker size / retention signal MSK does not expose at scan time. "
        "Counted $0 per cost-scope rule (rejected the prior blanket 30% factor)."
    )
    return basis


def _to_advisory_rec(ctx: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW $0-advisory rec carrying the live-priced AuditBasis."""
    new_rec = dict(rec)
    new_rec["Counted"] = False
    new_rec["EstimatedMonthlySavings"] = 0.0
    new_rec["EstimatedSavings"] = (
        "$0.00/month — advisory: review MSK broker sizing / serverless migration; "
        "realized saving is utilization-dependent and needs a target broker size + "
        "run-hour signal to quantify"
    )
    new_rec["AuditBasis"] = _current_cost_basis(ctx, rec)
    return new_rec


class MskModule(BaseServiceModule):
    """ServiceModule adapter for MSK. Advisory-only ($0 Counted=False) strategy."""

    key: str = "msk"
    cli_aliases: tuple[str, ...] = ("msk",)
    display_name: str = "MSK"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for MSK scanning."""
        return ("kafka",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan MSK clusters for cost optimization opportunities.

        Consults enhanced MSK checks. Every rec is emitted as a $0
        ``Counted=False`` advisory (the prior blanket 30% flat-rate factor was
        removed — msk C1): the realized broker-rightsizing / storage saving is
        utilization-dependent and needs a target broker size + run-hour signal
        MSK does not expose at scan time. The live current-spend SKUs are still
        priced into each rec's AuditBasis so the advisory is defensible.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        # The previous ``(broker_monthly + storage_monthly) × 0.30`` credited a
        # blanket 30% with no utilization signal and no target broker size — the
        # rec's own note admitted it was directional only. An exact current→
        # target broker price delta (one size down, via get_msk_broker_hourly_price)
        # needs a real utilization metric MSK does not expose at scan time, so the
        # saving stays $0 advisory (msk C1). We still price the *current* spend
        # from the live Broker-hours SKU (msk H1) and the real per-broker storage
        # size (msk H3) into each rec's AuditBasis so the advisory is defensible.
        result = get_enhanced_msk_checks(ctx)
        source_recs = result.get("recommendations", [])
        recs = [_to_advisory_rec(ctx, rec) for rec in source_recs]
        savings = 0.0

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        # Count hygiene (mirror batch / step_functions / quicksight): every MSK
        # rec is a $0 Counted=False advisory — it renders but must not inflate the
        # counted rec-count headline.
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="MSK",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MSK_OPTIMIZATION_DESCRIPTIONS,
        )
