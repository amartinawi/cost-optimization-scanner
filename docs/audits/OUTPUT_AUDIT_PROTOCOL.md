# Output Audit Protocol

Audit the scanner by auditing what it PRODUCES — the console log, the HTML
report, and the embedded scan JSON from real accounts — instead of (or before)
reading adapter code. Complements the per-service code audits in
`docs/audits/prompts/`; the recurring bug classes live in
`docs/audits/prompts/_LIVE_AUDIT_LESSONS.md` and every layer below keys off them.

## What the operator provides per run

1. **Console log** — capture stderr too:

   ```bash
   python3 cli.py --profile <profile> <region> 2>&1 | tee <profile>_<region>.log
   ```

2. **HTML report** — `<profile>_<region>.html` (self-contained; the scan JSON is
   embedded as a base64 download link, so no separate JSON file is needed).
3. **Context** — anything that changes what "correct" means:
   - Does the role have `ce:GetCostAndUsage`? (CE access is load-bearing: the
     counted total must never RISE when CE evidence is missing — lesson C8.)
   - Does the account hold active Savings Plans / RIs? (Triggers the C6
     commitment-coverage sweeps.)
   - Known estate facts worth cross-checking (e.g. "we have ~3,000 snapshots").

Outputs contain account and resource identifiers — keep them local.

## Layer 1 — deterministic sweeps (machine)

Run the harness; it encodes lessons A/B/C11/D/E as hard invariants:

```bash
python3 tools/output_audit.py <profile>_<region>.html --log <profile>_<region>.log
python3 tools/scan_doctor.py <scan>.json            # silent failures, $0 recs, missing scans
python3 tools/audit_upper_bounds.py <report.html>   # uncorroborated upper bounds (C8)
```

`output_audit.py` sweeps: S1 headline reconciliation - S2 advisory leak (B1) -
S3 counted-but-$0 (B3/C5) - S4 shared snapshot counted twice (A3) - S5
string/numeric agreement (B2/B3) - S6 pool-cap-at-100% tell (C11) - S7
counted-only / rendered-aware count semantics (D3/D4) - S8 negative/NaN -
S9 tab/panel render gate (D2) - S10 log triage - S11 dropped CoH buckets (E2) -
S12 permission gaps. Exit 1 on any FAIL. Tests:
`tests/test_output_audit.py` (one seeded violation per class).

## Layer 2a — rate verification worklist (harness extracts, MCP verifies)

```bash
python3 tools/output_audit.py <report> --rates          # human table
python3 tools/output_audit.py <report> --rates --json   # for the MCP loop
```

This walks every rec's `AuditBasis`/`PricingBasis` and emits the DEDUPED list
of pricing-rate claims the report rests on (typically 10-30 rows, not
hundreds). Claude then resolves each row against the live AWS Pricing API
(pricing MCP `get_pricing`, exact SKU filters: instance type, OS, license,
tenancy, volumeApiName, usagetype…) and records an EXACT / MISMATCH matrix in
the ledger. A MISMATCH row taints every rec sharing that claim. Composite
figures (e.g. a CO $22) are re-derived from their component SKU rates, not
trusted as published.

## Layer 2 — evidence audit (Claude, read-only)

For each service tab, ordered by counted dollars (biggest first):

1. **Re-derive the top N counted recs from first principles.** Start from the
   Layer-2a matrix; pull any missing rate via the pricing MCP for the exact
   SKU, region, and dimension; recompute the dollar; compare to the cent.
   Flat-global rates (EIP $3.65, Route53 $0.50/zone) must NOT be
   region-scaled (C1).
2. **Sweep the lesson classes that need judgment, not regex:**
   - C4/C9 — any saving that is a suspiciously round % of a cost figure is a
     fabricated dollar until it is two live prices.
   - C6 — if the account holds SPs/RIs: no counted rightsizing rec may target a
     type that leaves its coverage; per service, counted rightsizing <= CE
     uncovered-on-demand for that exact instance type.
   - C7 — every "remove a surcharge" rec must match a billed CE usage type.
   - C10 — every rightsizing lever must gate on the BINDING dimension (memory
     for caches/DBs, IOPS for volumes), not just CPU.
   - C11 — every reconciled tab where counted ~= a billed pool: demand the
     flagged subset's share.
3. **Coverage-gap accounting from the log.** Every "skipping/unreachable/no
   data" line is savings ABSENT from the report — list what the report cannot
   see, so a low headline is not mistaken for a clean estate.
4. **Display fidelity.** Headline figure, per-tab chips, priority filters, and
   card counts in the HTML must match the embedded JSON (the JSON is the truth;
   the HTML is a projection of it). Grouped CoH cards are by design (F3).

Live AWS cross-checks (CE usage types, DescribeClusterVersions, coverage reads)
use the same profile READ-ONLY. Any subagent dispatched for this layer gets
Write/Edit restricted — audit agents must never apply fixes.

Before reporting, re-read section F of `_LIVE_AUDIT_LESSONS.md` (audit-method
traps): camelCase CoH fields (F1), `EstimatedMonthlyCost` on unattached volumes
(F2), grouped rendering (F3), AWS-supplied annotations (F4).

## Layer 3 — adversarial verification

Every candidate finding gets an independent refute pass before it is reported:
try to prove the rec is actually correct (different field spelling? legitimate
projection? by-design grouping? fixture-only artifact?). Only findings that
survive land in the ledger. Historically two of three dedup "fixes" had defects
caught only by this pass.

## Findings ledger

One file per audited scan: `docs/audits/live/<profile>_<region>_<YYYYMMDD>.md`.

| Field | Meaning |
|-------|---------|
| ID | `<SVC>-<n>`, stable across re-scans |
| Class | Lesson ref (A3, B1, C6, D2, ...) or NEW |
| Severity | CRITICAL = counted-$ overstatement / double count; HIGH = phantom or missed real dollars; MEDIUM = count/display desync; LOW = hygiene |
| Claim | One sentence, with the exact dollar delta |
| Evidence | The rec JSON fields + the independent number that contradicts them |
| Suspect | `services/adapters/<file>.py` path or renderer location |
| Status | CANDIDATE → VERIFIED → FIXED → RECONCILED |

## Fix loop (per finding)

1. Unit-test the exact failing scenario (dollar AND `Counted` state asserted).
2. Fix; predict the exact headline delta the fix will cause.
3. Operator re-scans the same profile/region; the headline must move by
   EXACTLY the predicted amount. Anything else reopens the finding.
4. Regression gate: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
5. If the bug class is new or recurring, append it to `_LIVE_AUDIT_LESSONS.md`
   and, when machine-checkable, add a sweep to `tools/output_audit.py`.
