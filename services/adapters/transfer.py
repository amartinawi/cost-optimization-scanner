"""Protocol-based pricing adapter for Transfer Family."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.transfer_svc import TRANSFER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_transfer_checks


class TransferModule(BaseServiceModule):
    """ServiceModule adapter for Transfer Family. Protocol-based pricing strategy."""

    key: str = "transfer"
    cli_aliases: tuple[str, ...] = ("transfer",)
    display_name: str = "Transfer Family"
    # The shim gates terminate recs on CloudWatch BytesIn/BytesOut and honors
    # ctx.fast_mode (services/transfer_svc.py:68-75) — transfer L3.
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Transfer Family scanning."""
        return ("transfer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Transfer Family servers for cost optimization opportunities.

        Consults enhanced Transfer Family checks. Protocol-endpoint pricing is
        the AWS Transfer Family ``ProtocolHours`` rate ($0.30/protocol/hour),
        which is REGION-FLAT (identical across regions / endpoint types per the
        live Pricing API — validated 2026-06-27, ``USE1-ProtocolHours`` =
        $0.30/hr for SFTP/FTP/FTPS/AS2), so the EC2-derived
        ``pricing_multiplier`` is NOT applied (transfer C1).

        Two cost-correctness guards (transfer H1, H2):

        * **H1 — fail safe on terminate recs.** A STOPPED/OFFLINE server (the
          ``Unused Transfer Servers`` category) already incurs no per-hour
          endpoint charge, so "terminate it" realizes no *measured* saving. We
          never layer the partial ``(len(protocols) - 1)`` protocol-removal
          figure onto a whole-server-terminate rec; it is emitted as a $0
          advisory (``Counted=False``) unless billing is independently
          evidenced.
        * **H2 — no fabricated protocol $.** "Remove all-but-one protocol"
          counts ``(len(protocols) - 1)`` endpoints as savable with no
          per-protocol usage evidence. Without explicit evidence that specific
          protocols are unused, ``protocol_optimization`` is a $0 advisory.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        result = get_enhanced_transfer_checks(ctx)
        recs = result.get("recommendations", [])

        # Region-flat ProtocolHours rate; live-validated $0.30/protocol-hour
        # (transfer C1 — no pricing_multiplier).
        TRANSFER_PER_PROTOCOL_HOUR: float = 0.30
        HOURS_PER_MONTH: int = 730

        priced_recs: list[dict[str, Any]] = []
        savings = 0.0
        for rec in recs:
            # Immutability: never mutate the shim's rec dicts in place.
            new_rec = dict(rec)
            category = new_rec.get("CheckCategory")
            state = new_rec.get("State")

            # --- H1: unused / stopped-or-offline server (fail safe) --------- #
            # A stopped/offline Transfer server is not billing endpoint hours,
            # so termination has no quantifiable monthly saving. Skip the
            # re-pricing loop entirely and emit an honest $0 advisory; NEVER
            # apply a protocol-removal number to a whole-server-terminate rec.
            if category == "Unused Transfer Servers" or state in ("STOPPED", "OFFLINE"):
                new_rec["Counted"] = False
                new_rec["EstimatedMonthlySavings"] = 0.0
                state_label = (state or "stopped/offline").lower()
                new_rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: server is "
                    f"{state_label}; a stopped/offline Transfer server already "
                    "incurs no per-hour endpoint charge, so termination "
                    "realizes no measured saving (billing not evidenced)."
                )
                new_rec["AuditBasis"] = {
                    "rate_source": "AWS Transfer Family ProtocolHours (region-flat, "
                    "live-validated $0.30/protocol-hr us-east-1 2026-06-27)",
                    "counted": False,
                    "reason": "stopped/offline server not billing endpoint hours; "
                    "full-server termination saving not evidenced",
                }
                priced_recs.append(new_rec)
                continue

            # --- H2: protocol_optimization (gate on per-protocol evidence) -- #
            # Count ONLY when the shim proves specific protocols are unused via
            # per-protocol usage evidence (PerProtocolUsageEvidence=True and an
            # evidenced RemovableProtocols count). Absent that, (len-1) is a
            # fabricated quantity, so demote to a $0 advisory.
            evidence = new_rec.get("PerProtocolUsageEvidence") is True
            removable_evidenced = new_rec.get("RemovableProtocols")
            if evidence and isinstance(removable_evidenced, int) and removable_evidenced > 0:
                endpoint_monthly = round(
                    removable_evidenced * TRANSFER_PER_PROTOCOL_HOUR * HOURS_PER_MONTH, 2
                )
                new_rec["Counted"] = True
                new_rec["EstimatedMonthlySavings"] = endpoint_monthly
                new_rec["EstimatedSavings"] = (
                    f"${endpoint_monthly:.2f}/month from removing "
                    f"{removable_evidenced} unused protocol(s)"
                )
                new_rec["AuditBasis"] = {
                    "rate_source": "AWS Transfer Family ProtocolHours (region-flat, "
                    "live-validated $0.30/protocol-hr us-east-1 2026-06-27)",
                    "rate_per_protocol_hour": TRANSFER_PER_PROTOCOL_HOUR,
                    "hours_per_month": HOURS_PER_MONTH,
                    "removable_protocols": removable_evidenced,
                    "evidence": "per-protocol usage evidence confirms protocols unused",
                    "formula": "removable × $0.30/hr × 730",
                }
                savings += endpoint_monthly
                priced_recs.append(new_rec)
                continue

            # No per-protocol usage evidence → $0 advisory (transfer H2).
            protocols = new_rec.get("Protocols", [])
            protocol_count = len(protocols) if isinstance(protocols, list) else 0
            new_rec["Counted"] = False
            new_rec["EstimatedMonthlySavings"] = 0.0
            new_rec["EstimatedSavings"] = (
                "$0.00/month — advisory: protocol consolidation requires "
                "per-protocol usage evidence (none available). "
                f"{protocol_count} protocol(s) configured at $0.30/hr each — "
                "verify each protocol is actually in use before removing."
            )
            new_rec["AuditBasis"] = {
                "rate_source": "AWS Transfer Family ProtocolHours (region-flat, "
                "live-validated $0.30/protocol-hr us-east-1 2026-06-27)",
                "rate_per_protocol_hour": TRANSFER_PER_PROTOCOL_HOUR,
                "hours_per_month": HOURS_PER_MONTH,
                "counted": False,
                "reason": "no per-protocol usage evidence; (len-1) is not a "
                "defensible removable-protocol count",
            }
            priced_recs.append(new_rec)

        # Count hygiene: $0 advisory recs render but are excluded from the
        # headline rec count (mirrors services/_savings.mark_zero_savings_advisory).
        counted_recs = sum(1 for r in priced_recs if r.get("Counted") is not False)

        sources = {
            "enhanced_checks": SourceBlock(
                count=len(priced_recs), recommendations=tuple(priced_recs)
            )
        }

        return ServiceFindings(
            service_name="Transfer Family",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=TRANSFER_OPTIMIZATION_DESCRIPTIONS,
        )
