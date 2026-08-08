"""Output audit harness — deterministic invariant sweeps over a generated report.

Layer 1 of the output-audit protocol (docs/audits/OUTPUT_AUDIT_PROTOCOL.md).
Encodes the recurring bug classes from docs/audits/prompts/_LIVE_AUDIT_LESSONS.md
as machine-checkable sweeps against a scan JSON (or the base64 payload embedded
in a generated HTML report), plus an optional console-log triage.

Sweeps (lesson class in parentheses):
    S1  headline           summary total == sum of per-service totals, to the cent
    S2  advisory-leak      Counted=False rec with a non-zero numeric (B1)
    S3  counted-zero       counted rec whose every savings field is $0 (B3 / C5)
    S4  shared-snapshot    one snapshot counted under >1 rec or >1 tab (A3)
    S5  string-numeric     EstimatedSavings string vs numeric disagree (B2/B3)
    S6  pool-cap-tell      counted tab total ~= a billed pool at 100% (C11)
    S7  summary-semantics  counted-only rec counts + rendered-aware scanned (D3/D4)
    S8  negative-nan       negative or NaN savings anywhere
    S9  html-render        every rendered service has tab-<key> + panel-<key> (D2)
    S10 log triage         console warnings classified into coverage gaps
    S11 dropped-coh        CoH "no service bucket" warnings (E2)
    S12 permission-gaps    permission_issues, ce:GetCostAndUsage called out (C8)

Usage:
    python3 tools/output_audit.py <report.html | scan.json> [--log console.log] [--json]

Exit status: 0 = no FAIL findings, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

Finding = dict[str, str]

# What-if buy/coverage projections legitimately carry a non-zero numeric (B1-ii).
PROJECTION_SERVICES = {"commitment_analysis"}
# Source-level projections: born-advisory what-ifs whose counted dollar lives in
# another tab (eks node groups are EC2 instances — the EC2 tab counts them).
PROJECTION_SOURCES = {("eks_cost", "node_group_optimization")}

_DOLLAR = re.compile(r"\$([0-9,]+\.?[0-9]*)")
_SNAP_ID = re.compile(r"snap-[0-9a-f]{8,17}")
_NUMERIC_FIELDS = ("EstimatedMonthlySavings", "estimatedMonthlySavings", "monthly_savings")
_ALL_SAVINGS_FIELDS = _NUMERIC_FIELDS + ("EstimatedMonthlyCost", "PotentialMonthlySavings")
_BILLED_KEY = re.compile(r"billed|actual_billed|ActualBilledPool", re.I)


def _finding(sweep: str, severity: str, service: str, message: str) -> Finding:
    return {"sweep": sweep, "severity": severity, "service": service, "message": message}


def parse_dollar(text: Any) -> float:
    """Extract the first $-figure from a savings string; 0.0 when absent."""
    m = _DOLLAR.search(str(text or ""))
    return float(m.group(1).replace(",", "")) if m else 0.0


def rec_dollar(rec: dict[str, Any]) -> float:
    """Read a rec's dollar from every savings-bearing field shape (F1/F2/F5)."""
    for key in ("EstimatedMonthlySavings", "estimatedMonthlySavings", "EstimatedMonthlyCost", "monthly_savings"):
        val = rec.get(key)
        if isinstance(val, (int, float)) and val:
            return float(val)
    # Compute Optimizer AWS shape (F5): dollars nest in the rank-1 option.
    for opt_key in ("volumeRecommendationOptions", "recommendationOptions"):
        opts = rec.get(opt_key)
        if isinstance(opts, list) and opts and isinstance(opts[0], dict):
            val = (opts[0].get("savingsOpportunity") or {}).get(
                "estimatedMonthlySavings", {}).get("value")
            if isinstance(val, (int, float)) and val:
                return float(val)
    return parse_dollar(rec.get("EstimatedSavings", ""))


def load_report(path: Path | str) -> dict[str, Any]:
    """Load scan JSON directly or from the base64 payload inside a report HTML."""
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    m = re.search(r"data:application/json;base64,([A-Za-z0-9+/=]+)", text)
    if not m:
        raise SystemExit(f"no embedded JSON payload found in {path}")
    return json.loads(base64.b64decode(m.group(1)))


def _iter_recs(data: dict[str, Any]):
    """Yield (service_key, source_name, rec) for every dict rec in the report."""
    for key, svc in data.get("services", {}).items():
        for src_name, src in (svc.get("sources") or {}).items():
            recs = src.get("recommendations", []) if isinstance(src, dict) else (src or [])
            for rec in recs:
                if isinstance(rec, dict):
                    yield key, src_name, rec


def _is_counted(rec: dict[str, Any]) -> bool:
    return rec.get("Counted") is not False


# ------------------------------------------------------------------ sweeps


def sweep_headline(data: dict[str, Any]) -> list[Finding]:
    """S1 — the summary headline must reconcile to the per-service totals."""
    svcs = data.get("services", {})
    total = sum(v.get("total_monthly_savings", 0) or 0 for v in svcs.values())
    reported = data.get("summary", {}).get("total_monthly_savings", 0)
    if abs(total - reported) >= 0.5:
        return [_finding("S1-headline", "FAIL", "summary",
                         f"headline ${reported:,.2f} != sum of services ${total:,.2f} "
                         f"(delta ${reported - total:+,.2f})")]
    return []


def sweep_advisory_leak(data: dict[str, Any]) -> list[Finding]:
    """S2 — a demoted (Counted=False) rec must carry a zero numeric (B1)."""
    out: list[Finding] = []
    for key, src, rec in _iter_recs(data):
        if key in PROJECTION_SERVICES or (key, src) in PROJECTION_SOURCES or _is_counted(rec):
            continue
        for field in _NUMERIC_FIELDS:
            val = rec.get(field)
            if isinstance(val, (int, float)) and abs(val) > 1e-4:
                out.append(_finding("S2-advisory-leak", "FAIL", key,
                                    f"{src}: Counted=False rec carries {field}={val} "
                                    f"(recoverable belongs in PotentialMonthlySavings)"))
    return out


def sweep_counted_zero(data: dict[str, Any]) -> list[Finding]:
    """S3 — a counted rec with $0 in every savings field inflates the count."""
    out: list[Finding] = []
    for key, src, rec in _iter_recs(data):
        if _is_counted(rec) and rec_dollar(rec) == 0.0:
            out.append(_finding("S3-counted-zero", "WARN", key,
                                f"{src}: counted rec with $0 in every savings field "
                                f"— pricing fallback or de-minimis leak (C5)?"))
    return out


def sweep_shared_snapshots(data: dict[str, Any]) -> list[Finding]:
    """S4 — one snapshot must contribute a counted dollar at most once (A3)."""
    owners: dict[str, set[str]] = {}
    for key, src, rec in _iter_recs(data):
        if not _is_counted(rec) or rec_dollar(rec) == 0.0:
            continue
        label = f"{key}/{rec.get('ImageId') or rec.get('SnapshotId') or src}"
        for snap in set(_SNAP_ID.findall(json.dumps(rec))):
            owners.setdefault(snap, set()).add(label)
    return [
        _finding("S4-shared-snapshot", "FAIL", "cross-tab",
                 f"{snap} counted under {len(labels)} recs: {sorted(labels)}")
        for snap, labels in owners.items() if len(labels) > 1
    ]


def sweep_string_numeric(data: dict[str, Any]) -> list[Finding]:
    """S5 — on a counted rec the string and numeric must agree to the cent (B2/B3)."""
    out: list[Finding] = []
    for key, src, rec in _iter_recs(data):
        if not _is_counted(rec) or "EstimatedSavings" not in rec:
            continue
        text = str(rec.get("EstimatedSavings") or "")
        string_val = parse_dollar(text)
        numeric = next((float(rec[f]) for f in _NUMERIC_FIELDS
                        if isinstance(rec.get(f), (int, float))), 0.0)
        if string_val == 0.0:
            continue  # percent-only / empty strings are a different class (S3)
        if "/year" in text or "annual" in text.lower():
            string_val /= 12.0
        if abs(string_val - numeric) > max(0.011, 0.005 * string_val):
            out.append(_finding("S5-string-numeric", "FAIL", key,
                                f"{src}: string '{text}' (${string_val:,.2f}/mo) vs "
                                f"numeric {numeric} — B2/B3 disagreement"))
    return out


def sweep_pool_cap(data: dict[str, Any]) -> list[Finding]:
    """S6 — counted total ~= a billed pool means the cap bound at 100% (C11)."""
    out: list[Finding] = []
    for key, svc in data.get("services", {}).items():
        counted = svc.get("total_monthly_savings", 0) or 0
        if counted < 100:
            continue
        pools: set[float] = set()
        for _, _, rec in _iter_recs({"services": {key: svc}}):
            for k, v in rec.items():
                if isinstance(v, (int, float)) and v > 0 and _BILLED_KEY.search(str(k)):
                    pools.add(float(v))
        for pool in pools:
            if pool >= 100 and abs(counted - pool) / pool < 0.02:
                out.append(_finding("S6-pool-cap-tell", "WARN", key,
                                    f"counted ${counted:,.2f} ~= billed pool ${pool:,.2f} "
                                    f"— is the flagged subset's SHARE applied (C11)?"))
    return out


def _counted_recs(svc: dict[str, Any]) -> int:
    """Mirror ScanResultBuilder._counted_recommendations on serialized JSON."""
    total = 0
    for src in (svc.get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        recs = src.get("recommendations", [])
        if recs:
            total += sum(1 for r in recs if isinstance(r, dict) and _is_counted(r))
        elif src.get("count", 0) > 0:
            total += src["count"]
    return total


def _rendered_recs(svc: dict[str, Any]) -> int:
    """Mirror ScanResultBuilder._rendered_recommendations on serialized JSON."""
    total = 0
    for src in (svc.get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        recs = src.get("recommendations", [])
        if recs:
            total += sum(1 for r in recs if isinstance(r, dict) and r.get("finding") != "OPTIMIZED")
        elif src.get("count", 0) > 0:
            total += src["count"]
    return total


def sweep_summary_semantics(data: dict[str, Any]) -> list[Finding]:
    """S7 — counted-only rec counts and rendered-aware scanned count (D3/D4)."""
    out: list[Finding] = []
    svcs = data.get("services", {})
    summary = data.get("summary", {})
    for key, svc in svcs.items():
        counted = _counted_recs(svc)
        declared = svc.get("total_recommendations", counted)
        if declared != counted:
            out.append(_finding("S7-summary-semantics", "FAIL", key,
                                f"total_recommendations={declared} but counted-only recs={counted} "
                                f"— advisories must not inflate the count (D4)"))
    expected_total = sum(_counted_recs(s) for s in svcs.values())
    if summary.get("total_recommendations") not in (None, expected_total):
        out.append(_finding("S7-summary-semantics", "FAIL", "summary",
                            f"summary.total_recommendations={summary.get('total_recommendations')} "
                            f"!= recomputed counted-only {expected_total}"))
    scanned = sum(1 for s in svcs.values() if _rendered_recs(s) > 0 or s.get("total_count", 0) > 0)
    if summary.get("total_services_scanned") not in (None, scanned):
        out.append(_finding("S7-summary-semantics", "FAIL", "summary",
                            f"summary.total_services_scanned={summary.get('total_services_scanned')} "
                            f"!= recomputed rendered-aware {scanned} (D3)"))
    return out


def sweep_tab_reconcile(data: dict[str, Any]) -> list[Finding]:
    """S13 — each tab's total must equal the sum of its own counted rec dollars.

    S1 checks summary vs tab totals; this checks each tab against its recs, which
    is what caught a $66 delta hiding in Compute Optimizer's nested field shape.
    Skips services with count-placeholder sources (recs not materialised).
    """
    out: list[Finding] = []
    for key, svc in data.get("services", {}).items():
        sources = svc.get("sources") or {}
        if any(isinstance(s, dict) and not s.get("recommendations") and s.get("count", 0) > 0
               for s in sources.values()):
            continue
        total = 0.0
        for _, _, rec in _iter_recs({"services": {key: svc}}):
            if _is_counted(rec):
                total += rec_dollar(rec)
        tab = svc.get("total_monthly_savings", 0) or 0
        if abs(tab - total) > 0.5:
            out.append(_finding("S13-tab-reconcile", "FAIL", key,
                                f"tab total ${tab:,.2f} != sum of counted recs ${total:,.2f} "
                                f"(delta ${tab - total:+,.2f}) — hidden field or phantom sum"))
    return out


def sweep_negative(data: dict[str, Any]) -> list[Finding]:
    """S8 — no savings field may be negative or NaN."""
    out: list[Finding] = []
    for key, src, rec in _iter_recs(data):
        for field in _ALL_SAVINGS_FIELDS:
            val = rec.get(field)
            if isinstance(val, (int, float)) and (val < 0 or math.isnan(val)):
                out.append(_finding("S8-negative-nan", "FAIL", key,
                                    f"{src}: {field}={val}"))
    return out


def sweep_html_render(data: dict[str, Any], html_text: str) -> list[Finding]:
    """S9 — every service that renders cards must have its tab + panel (D2)."""
    out: list[Finding] = []
    for key, svc in data.get("services", {}).items():
        if _rendered_recs(svc) <= 0 and svc.get("total_count", 0) <= 0:
            continue
        for element in (f'id="tab-{key}"', f'id="panel-{key}"'):
            if element not in html_text:
                out.append(_finding("S9-html-render", "FAIL", key,
                                    f"renders cards but {element} missing from HTML (D2)"))
    total = data.get("summary", {}).get("total_monthly_savings", 0) or 0
    if total > 0 and f"${total:,.2f}" not in html_text and f"${total:,.0f}" not in html_text:
        out.append(_finding("S9-html-render", "WARN", "summary",
                            f"headline ${total:,.2f} not found verbatim in HTML — "
                            f"verify the displayed figure manually"))
    return out


def sweep_warnings(data: dict[str, Any]) -> list[Finding]:
    """S11/S12 — dropped CoH buckets (E2) and permission gaps (C8)."""
    out: list[Finding] = []
    for w in data.get("scan_warnings", []):
        msg = w.get("message", "")
        if "no service bucket" in msg or "dropped" in msg:
            out.append(_finding("S11-dropped-coh", "FAIL", w.get("service", "?"),
                                f"AWS-computed savings discarded: {msg}"))
    perms = data.get("permission_issues", [])
    for p in perms:
        action = p.get("action", "?")
        severity = "WARN"
        note = ""
        if action.startswith("ce:"):
            note = " — CE access is load-bearing: fail-closed caps demote real savings without it"
        out.append(_finding("S12-permission-gap", severity, p.get("service", "?"),
                            f"{action}: {p.get('message', '')}{note}"))
    return out


def triage_log(log_text: str) -> list[Finding]:
    """S10 — classify console warnings into coverage gaps vs benign drops."""
    out: list[Finding] = []
    counters = {"stale-coh-drop": 0, "metric-gap": 0, "no-commitment-data": 0}
    for line in log_text.splitlines():
        if "Warning:" not in line and "Found" not in line:
            continue
        if "dropping the stale recommendation" in line:
            counters["stale-coh-drop"] += 1
        elif "no CloudWatch" in line or "no data in the" in line:
            counters["metric-gap"] += 1
        elif "unreachable" in line or "skipping all remaining" in line:
            out.append(_finding("log-coverage-gap", "WARN", "log",
                                f"savings absent from report: {line.strip()}"))
        elif "no active Savings Plans" in line:
            counters["no-commitment-data"] += 1
        elif re.search(r"Found \d+ optimization opportunities", line):
            out.append(_finding("log-summary", "INFO", "log", line.strip()))
    for name, n in counters.items():
        if n:
            out.append(_finding(f"log-{name}", "INFO", "log", f"{n} occurrence(s)"))
    return out


# Keys inside AuditBasis whose numeric value is a PRICE/RATE claim worth
# verifying against the live AWS Pricing API (vs sizes, ages, counts).
_RATE_KEY = re.compile(r"rate|price|per_gb|per_iops|hourly|_month\b|multiplier", re.I)


def extract_rate_assertions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract every deduped pricing-rate claim for MCP verification (Layer 2a).

    Walks all recs and pulls (a) numeric AuditBasis leaves whose key looks like
    a rate/price and (b) whole PricingBasis strings. Each row is one claim to
    check against the live AWS Pricing API; identical claims collapse to one.
    """
    seen: set[tuple] = set()
    rows: list[dict[str, Any]] = []

    # Per-resource keys must not split identical rate claims into many rows.
    per_resource = {"formula", "metric_window"}

    def _context(basis: dict[str, Any]) -> str:
        parts = [f"{k}={v}" for k, v in sorted(basis.items())
                 if k not in per_resource and isinstance(v, str) and len(str(v)) < 120]
        return "; ".join(parts)

    for svc, src, rec in _iter_recs(data):
        basis = rec.get("AuditBasis")
        if isinstance(basis, dict):
            ctx_str = _context(basis)
            # Mask digits in the dedup key so per-resource arithmetic embedded in
            # shared context strings does not split one rate claim into N rows.
            masked = re.sub(r"[0-9][0-9,.]*", "#", ctx_str)
            for k, v in basis.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and _RATE_KEY.search(k):
                    key = (svc, k, round(float(v), 8), masked)
                    if key not in seen:
                        seen.add(key)
                        rows.append({"service": svc, "source": src, "field": k,
                                     "value": float(v), "context": ctx_str})
        pb = rec.get("PricingBasis")
        if isinstance(pb, str) and "$" in pb:
            key = (svc, "PricingBasis", pb)
            if key not in seen:
                seen.add(key)
                rows.append({"service": svc, "source": src, "field": "PricingBasis",
                             "value": parse_dollar(pb), "context": pb})
    return sorted(rows, key=lambda r: (r["service"], r["field"]))


def run_sweeps(data: dict[str, Any], html_text: str | None = None,
               log_text: str | None = None) -> list[Finding]:
    """Run every applicable sweep and return the combined findings list."""
    findings: list[Finding] = []
    findings += sweep_headline(data)
    findings += sweep_advisory_leak(data)
    findings += sweep_counted_zero(data)
    findings += sweep_shared_snapshots(data)
    findings += sweep_string_numeric(data)
    findings += sweep_pool_cap(data)
    findings += sweep_summary_semantics(data)
    findings += sweep_tab_reconcile(data)
    findings += sweep_negative(data)
    findings += sweep_warnings(data)
    if html_text is not None:
        findings += sweep_html_render(data, html_text)
    if log_text is not None:
        findings += triage_log(log_text)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="report.html (embedded JSON) or scan .json")
    ap.add_argument("--log", help="console log captured from the scan run")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--rates", action="store_true",
                    help="emit the deduped rate-assertion worklist for MCP verification")
    args = ap.parse_args()

    path = Path(args.report)
    data = load_report(path)
    if args.rates:
        rows = extract_rate_assertions(data)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{r['service']:<16} {r['field']:<32} {r['value']:<12} {r['context'][:90]}")
            print(f"\n{len(rows)} distinct rate assertions to verify against the live Pricing API")
        return 0
    html_text = path.read_text() if path.suffix in (".html", ".htm") else None
    log_text = Path(args.log).read_text() if args.log else None
    findings = run_sweeps(data, html_text=html_text, log_text=log_text)

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        order = {"FAIL": 0, "WARN": 1, "INFO": 2}
        for f in sorted(findings, key=lambda f: (order.get(f["severity"], 3), f["sweep"])):
            print(f"[{f['severity']:<4}] {f['sweep']:<22} {f['service']:<18} {f['message']}")
        fails = sum(1 for f in findings if f["severity"] == "FAIL")
        warns = sum(1 for f in findings if f["severity"] == "WARN")
        print(f"\n{fails} FAIL, {warns} WARN, {len(findings) - fails - warns} INFO "
              f"— account {data.get('account_id')} region {data.get('region')} "
              f"headline ${data.get('summary', {}).get('total_monthly_savings', 0):,.2f}/mo")
    return 1 if any(f["severity"] == "FAIL" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
