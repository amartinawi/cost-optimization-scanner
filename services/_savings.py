"""Shared savings-parsing utilities for ServiceModule adapters."""

from __future__ import annotations

import re
from typing import Any


def parse_dollar_savings(savings_str: str) -> float:
    """Extract dollar amount from savings strings like '$12.50/month' or 'Up to $12.50/month'.

    Returns 0.0 when no explicit dollar amount is present. Percentage-only
    strings (e.g. '30-50% cost reduction') intentionally return 0.0 — callers
    that want a real number must compute it from live pricing rather than rely
    on an arbitrary constant fallback.
    """
    if not savings_str:
        return 0.0
    match = re.search(r"\$(\d+[\d,]*\.?\d*)", savings_str)
    if match:
        return float(match.group(1).replace(",", ""))
    return 0.0


_CO_OPTION_KEYS: tuple[str, ...] = (
    "recommendationOptions",          # EC2 instance recommendations
    "volumeRecommendationOptions",    # EBS volume recommendations
    "instanceRecommendationOptions",  # RDS instance recommendations
    "storageRecommendationOptions",   # RDS storage recommendations
)


def compute_optimizer_savings(rec: dict[str, Any]) -> float:
    """Extract estimated monthly savings from an AWS Compute Optimizer recommendation.

    AWS places this value at ``<resource>RecommendationOptions[N].savingsOpportunity.estimatedMonthlySavings``
    as a ``{currency, value}`` object — not at the top level. The option-array key
    varies by resource type (EC2 uses ``recommendationOptions``, EBS uses
    ``volumeRecommendationOptions``, RDS uses both ``instanceRecommendationOptions``
    and ``storageRecommendationOptions``). This helper sums the rank-1 (or first)
    option's savings across every option-array present on the record, and returns
    0.0 on any structural surprise so callers stay defensive.
    """
    total = 0.0
    for key in _CO_OPTION_KEYS:
        options = rec.get(key) or []
        if not options:
            continue
        best = next((o for o in options if o.get("rank") == 1), options[0])
        ems = best.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {})
        value = ems.get("value", 0.0) if isinstance(ems, dict) else ems
        try:
            total += float(value or 0.0)
        except (TypeError, ValueError):
            continue
    return total
