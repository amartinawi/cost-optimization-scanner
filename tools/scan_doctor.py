"""Scan doctor — surface the failure classes the scanner normally swallows.

Reads a cost_optimization_scan_*.json file and reports:
  1. SILENT FAILURES   — scan_warnings + permission_issues (missing IAM, throttling,
                          pricing fallbacks) that otherwise sit unread in the JSON.
  2. INACCURATE $       — findings whose EstimatedSavings is $0.00 / missing, which
                          usually means a pricing lookup failed and fell back to 0.
  3. MISSING SCANS      — services that returned zero recommendations AND zero
                          warnings (possible silent skip vs genuinely clean).

Usage:
    python3 tools/scan_doctor.py cost_optimization_scan_us-east-1_20260622_xxxx.json
    python3 tools/scan_doctor.py <file.json> --service ec2     # drill into one tab
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

_DOLLAR = re.compile(r"\$(\d+[\d,]*\.?\d*)")


def _savings_text(rec: dict[str, Any]) -> str:
    for k in ("EstimatedSavings", "estimatedMonthlySavings", "estimated_savings"):
        if k in rec:
            return str(rec[k])
    return ""


def _rec_dollars(rec: dict[str, Any]) -> float:
    val = rec.get("estimatedMonthlySavings")
    if isinstance(val, (int, float)):
        return float(val)
    m = _DOLLAR.search(_savings_text(rec))
    return float(m.group(1).replace(",", "")) if m else 0.0


def _iter_recs(service_data: dict[str, Any]):
    for source_name, src in service_data.get("sources", {}).items():
        recs = src.get("recommendations", []) if isinstance(src, dict) else (src or [])
        for rec in recs:
            if isinstance(rec, dict):
                yield source_name, rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_file", help="Path to a cost_optimization_scan_*.json file")
    ap.add_argument("--service", help="Drill into a single service tab (e.g. ec2)")
    args = ap.parse_args()

    with open(args.json_file) as f:
        data = json.load(f)

    services: dict[str, Any] = data.get("services", {})
    warnings = data.get("scan_warnings", [])
    perms = data.get("permission_issues", [])

    # ---- 1. SILENT FAILURES -------------------------------------------------
    print("=" * 72)
    print("1. SILENT FAILURES (warnings + missing-permission gaps)")
    print("=" * 72)
    if perms:
        print(f"\n  PERMISSION ISSUES ({len(perms)}) — these BLOCK whole checks => missed savings:")
        for p in perms:
            print(f"    [{p.get('service','?')}] {p.get('action','?')}: {p.get('message','')}")
    else:
        print("\n  permission issues: none")
    if warnings:
        by_svc: dict[str, int] = {}
        for w in warnings:
            by_svc[w.get("service", "?")] = by_svc.get(w.get("service", "?"), 0) + 1
        print(f"\n  WARNINGS ({len(warnings)}) by service:")
        for svc, n in sorted(by_svc.items(), key=lambda x: -x[1]):
            print(f"    {svc:<22} {n}")
        print("    (run with --service <name> to see the messages)")
    else:
        print("  warnings: none")

    # ---- 2. INACCURATE $ ----------------------------------------------------
    print("\n" + "=" * 72)
    print("2. INACCURATE $  (findings counted but with $0 / no savings)")
    print("=" * 72)
    zero_by_svc: dict[str, int] = {}
    total_recs = 0
    for skey, sdata in services.items():
        for _, rec in _iter_recs(sdata):
            total_recs += 1
            if _rec_dollars(rec) <= 0.0:
                zero_by_svc[skey] = zero_by_svc.get(skey, 0) + 1
    if zero_by_svc:
        print("\n  Services emitting $0-savings findings (pricing fallback / nudge leakage):")
        for svc, n in sorted(zero_by_svc.items(), key=lambda x: -x[1]):
            print(f"    {svc:<22} {n} zero-$ of {services[svc].get('total_recommendations', '?')} recs")
    else:
        print("\n  No $0-savings findings detected. Good.")

    # ---- 3. MISSING SCANS ---------------------------------------------------
    print("\n" + "=" * 72)
    print("3. POSSIBLE MISSING / SILENT-SKIP SERVICES")
    print("=" * 72)
    warned = {w.get("service") for w in warnings} | {p.get("service") for p in perms}
    quiet = [
        k for k, s in services.items()
        if s.get("total_recommendations", 0) == 0 and s.get("total_count", 0) == 0 and k not in warned
    ]
    print(f"\n  {len(quiet)} service(s) returned 0 findings, 0 resources, 0 warnings.")
    print("  (Either genuinely nothing to optimize, or the check never ran. Spot-check a few.)")
    print("    " + ", ".join(sorted(quiet)) if quiet else "    none")

    # ---- Optional per-service drilldown ------------------------------------
    if args.service:
        skey = args.service
        sdata = services.get(skey, {})
        print("\n" + "=" * 72)
        print(f"DRILLDOWN: {skey}  "
              f"(savings=${sdata.get('total_monthly_savings', 0):,.2f}, "
              f"recs={sdata.get('total_recommendations', 0)})")
        print("=" * 72)
        for src, rec in _iter_recs(sdata):
            rid = rec.get("InstanceId") or rec.get("resourceId") or rec.get("ResourceId") or "?"
            itype = rec.get("InstanceType", "")
            cat = rec.get("CheckCategory") or rec.get("actionType") or src
            print(f"  [{src}] {rid} {itype} | {cat} | ${_rec_dollars(rec):,.2f}  ({_savings_text(rec)})")
        svc_warnings = [w for w in warnings if w.get("service") == skey]
        if svc_warnings:
            print(f"\n  {skey} warnings:")
            for w in svc_warnings:
                print(f"    - {w.get('message','')}")

    print(f"\nScanned region {data.get('region','?')} | "
          f"{len(services)} services | {total_recs} total findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
