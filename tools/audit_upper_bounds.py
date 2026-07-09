#!/usr/bin/env python3
"""Flag counted savings derived from an UNCORROBORATED upper bound (lesson C8).

An upper-bound saving (provisioned size, full snapshot size) must be capped at
what Cost Explorer says is actually billed, and demoted to a $0 advisory when that
evidence cannot be read. Counting one unconditionally overstates the report — and
does so *more* when a permission is missing, which is the worst possible direction.

This walks a generated report and reports, per service/source, how much counted
saving rests on an upper bound with no billing corroboration in its audit trail.

Usage:
    python3 tools/audit_upper_bounds.py <report.html | report.json>

Exit status:
    0  no uncorroborated counted upper bounds
    1  at least one found (the amount is printed)
"""

from __future__ import annotations

import base64
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any

# Wording an adapter uses when it knows its figure is an upper bound.
_UPPER = re.compile(r"upper[ _-]?bound|max estimate|typically lower|overstat", re.I)
# Wording that proves the bound was checked against real billing.
_CORROBORATED = re.compile(
    r"actual_billed|ActualBilledPool|Reconciled|reconcil|Cost Explorer|billed backup|"
    r"measured from actual billing",
    re.I,
)

_SAVINGS_KEYS = ("EstimatedMonthlySavings", "monthly_savings", "_savings")


def load_report(path: Path) -> dict[str, Any]:
    """Load a report from its JSON, or from the base64 payload inside the HTML."""
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    match = re.search(r"data:application/json;base64,([A-Za-z0-9+/=]+)", text)
    if not match:
        raise SystemExit(f"no embedded JSON payload found in {path}")
    return json.loads(base64.b64decode(match.group(1)))


def rec_savings(rec: dict[str, Any]) -> float:
    for key in _SAVINGS_KEYS:
        try:
            value = float(rec.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0.0


def audit(report: dict[str, Any]) -> tuple[dict[tuple[str, str], list[float]], float]:
    """Return {(service, source): [uncorroborated $...]} and the headline total."""
    findings: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for service, block in (report.get("services") or {}).items():
        for source, src in (block.get("sources") or {}).items():
            if not isinstance(src, dict):
                continue
            for rec in src.get("recommendations") or []:
                if not isinstance(rec, dict) or rec.get("Counted") is False:
                    continue
                saving = rec_savings(rec)
                if saving <= 0:
                    continue
                blob = json.dumps(rec, default=str)
                if _UPPER.search(blob) and not _CORROBORATED.search(blob):
                    findings[(service, source)].append(saving)
    headline = float((report.get("summary") or {}).get("total_monthly_savings") or 0)
    return findings, headline


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = load_report(Path(argv[1]))
    findings, headline = audit(report)

    if not findings:
        print(f"OK — no counted savings rest on an uncorroborated upper bound (headline ${headline:,.2f}/mo)")
        return 0

    total = sum(sum(v) for v in findings.values())
    print(f"{'service/source':40s} {'recs':>5s} {'uncorroborated $':>18s}")
    for (service, source), amounts in sorted(findings.items(), key=lambda kv: -sum(kv[1])):
        print(f"{service + '/' + source:40s} {len(amounts):>5d} {sum(amounts):>18,.2f}")
    pct = (total / headline * 100) if headline else 0.0
    print(f"\nC8 RISK: ${total:,.2f}/mo counted from uncorroborated upper bounds "
          f"({pct:.1f}% of the ${headline:,.2f}/mo headline)")
    print("Each must be capped at billed spend, or demoted to a $0 advisory when "
          "Cost Explorer cannot be read. See docs/audits/prompts/_LIVE_AUDIT_LESSONS.md (C8).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
