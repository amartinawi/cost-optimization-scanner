"""Keyword-rate adapter for OpenSearch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.opensearch import OPENSEARCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_opensearch_checks

# OpenSearch-managed gp3 EBS storage rate ($/GB-month, us-east-1 baseline).
# Region-scaled via pricing_multiplier at the per-rec emit site.
GP3_PRICE_PER_GB_MONTH: float = 0.11
# Conservative midpoint of cold-tier / UltraWarm storage savings vs gp3.
STORAGE_SAVINGS_FACTOR: float = 0.20


class OpensearchModule(BaseServiceModule):
    """ServiceModule adapter for OpenSearch. Keyword-rate savings strategy."""

    key: str = "opensearch"
    cli_aliases: tuple[str, ...] = ("opensearch",)
    display_name: str = "OpenSearch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for OpenSearch scanning."""
        return ("opensearch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan OpenSearch domains for cost optimization opportunities.

        Consults enhanced OpenSearch checks. Savings calculated via keyword-rate
        heuristics matching Reserved, Graviton, and storage patterns.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        result = get_enhanced_opensearch_checks(ctx)
        recs = result.get("recommendations", [])

        # Instance-discount rate keyed on the structured CheckCategory. Reserved
        # is a COMMITMENT lever (overlaps the commitment tab) → advisory.
        rate_by_category = {
            "Graviton Migration": 0.25,
            "Underutilized Domain": 0.30,  # downsize — alternative to Graviton
        }

        # Price every rec and attach the per-rec dollar figure (the report
        # previously showed only "30-50%", with no per-domain $).
        for rec in recs:
            category = rec.get("CheckCategory", "")
            instance_type = rec.get("InstanceType")
            instance_count = rec.get("InstanceCount", 1) or 1
            value = 0.0
            if "storage" in category.lower():
                ebs = rec.get("EBSVolumeSize", 0) or 0
                value = ebs * GP3_PRICE_PER_GB_MONTH * ctx.pricing_multiplier * STORAGE_SAVINGS_FACTOR
            elif ctx.pricing_engine is not None and instance_type:
                rate = rate_by_category.get(category, 0.0)
                monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
                value = monthly * instance_count * rate
            rec["EstimatedMonthlySavings"] = round(value, 2)
            if "Reserved" in category:
                rec["Counted"] = False  # commitment lever — advisory

        # Dedupe instance-axis levers (Graviton vs downsize) per domain — they are
        # alternatives on the same nodes. Storage is a separate axis (kept). A rec
        # that prices to $0 (e.g. underutilized with no InstanceType) is advisory.
        best_instance: dict[str, dict[str, Any]] = {}
        for rec in recs:
            if rec.get("Counted") is False or "storage" in rec.get("CheckCategory", "").lower():
                continue
            dom = rec.get("DomainName", "")
            cur = best_instance.get(dom)
            if cur is None or rec["EstimatedMonthlySavings"] > cur["EstimatedMonthlySavings"]:
                best_instance[dom] = rec

        best_ids = {id(r) for r in best_instance.values()}
        savings = 0.0
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            is_storage = "storage" in rec.get("CheckCategory", "").lower()
            keep = (is_storage or id(rec) in best_ids) and rec["EstimatedMonthlySavings"] > 0
            if keep:
                rec["Counted"] = True
                savings += rec["EstimatedMonthlySavings"]
            else:
                rec["Counted"] = False

        return ServiceFindings(
            service_name="OpenSearch",
            total_recommendations=len(recs),
            total_monthly_savings=round(savings, 2),
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
        )
