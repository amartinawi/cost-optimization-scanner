"""Shared savings-parsing utilities for ServiceModule adapters."""

from __future__ import annotations

import re


def parse_dollar_savings(savings_str: str) -> float:
    """Extract dollar amount from savings strings like '$12.50/month' or 'Up to $12.50/month'.

    Returns 0.0 if no dollar amount found. For percentage-only strings
    like '~50% of instance cost', returns a $50 fallback estimate.
    """
    if not savings_str:
        return 0.0
    match = re.search(r"\$(\d+[\d,]*\.?\d*)", savings_str)
    if match:
        return float(match.group(1).replace(",", ""))
    if "%" in savings_str:
        return 50.0
    return 0.0
