"""Shared helpers for adapters that consume Cost Optimization Hub buckets.

The orchestrator buckets CoH recommendations into ``ctx.cost_hub_splits[<svc>]``.
Per-service adapters render their bucket inline alongside heuristic findings.
These helpers normalize resource ids for authority de-duplication
(CoH > heuristic) and filter out recs that belong elsewhere (RI / SP purchase
recs are routed to ``commitment_analysis``; ``N/A`` resource ids carry no
concrete resource). Mirrors the inline ``_coh_is_renderable`` in the RDS
adapter (SR-3).
"""

from __future__ import annotations

from typing import Any


def normalize_resource_id(raw: str) -> str:
    """Canonical de-dup key for a CoH / heuristic resource id.

    Strips an ARN down to its final name segment so a CoH ``resourceArn`` and a
    heuristic ``ClusterId`` / ``DomainName`` converge on the same key::

        arn:aws:elasticache:us-east-1:1:cluster:prod    -> prod
        arn:aws:es:us-east-1:1:domain/prod              -> prod
        arn:aws:redshift:us-east-1:1:cluster:prod       -> prod
        prod                                            -> prod

    A bare id with no ARN structure is returned unchanged; falsy input -> ``""``.
    """
    if not raw:
        return ""
    text = str(raw)
    if text.startswith("arn:"):
        head = text.rsplit(":", 1)[-1]
        if "/" in head:
            head = head.rsplit("/", 1)[-1]
        return head
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text


def is_renderable_coh_rec(rec: dict[str, Any]) -> bool:
    """Filter CoH recs down to ones this service tab should render and count.

    Reserved-Instance / Savings-Plan purchase recommendations are routed to the
    ``commitment_analysis`` tab by the orchestrator and must not be re-counted
    here; ``N/A`` resource rows carry no concrete resource. Everything else
    (rightsizing, idle, storage findings) is renderable.
    """
    action = rec.get("actionType", "")
    if action in ("PurchaseReservedInstances", "PurchaseSavingsPlans"):
        return False
    if rec.get("resourceId") == "N/A":
        return False
    return True


def coh_savings(rec: dict[str, Any]) -> float:
    """Monthly $ a CoH rec contributes (``estimatedMonthlySavings`` top-level)."""
    try:
        return float(rec.get("estimatedMonthlySavings", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def coh_key(rec: dict[str, Any]) -> str:
    """Normalized dedup key for a CoH recommendation (resourceArn preferred)."""
    return normalize_resource_id(rec.get("resourceArn") or rec.get("resourceId") or "")
