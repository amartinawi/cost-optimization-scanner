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

**Operational notes (learned the hard way, 2026-08-11):**

- Run scans with `.venv/bin/python cli.py …` — the system `python3` has no boto3.
- **Preserve the baseline before re-scanning.** A re-scan overwrites
  `<profile>_<region>.html` in place; copy it aside first or the reconciliation
  compares the new report against itself.
- **Wait for `Report generated` in the log, not for the log to be non-empty.**
  Python buffers stdout to a file, so an early flushed line looks like
  completion. A reconciliation run against a half-finished scan reported $0.00
  deltas across every tab and looked like a total fix failure.
- Assume-role profiles expire; `InvalidClientTokenId` means stale credentials,
  not a permanent block. Build the session WITHOUT a region and pass
  `region_name` to `.client()`.
- A scan takes tens of minutes. Run it in the background and poll the report's
  mtime.

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
S12 permission gaps - S13 per-tab reconciliation - S14 projected-commitment
recompute - **S15 binding-dimension evidence (C18)** - **S16 conjunct evidence
(C21)**. Exit 1 on any FAIL. Tests:
`tests/test_output_audit.py` (one seeded violation per class).

S15 is the machine-checkable half of lesson C18 and keys off
`_BINDING_DIMENSION_LEVERS`: a COUNTED downsize must carry evidence for the
dimension that actually binds it — EC2 `AvgMemory` (memory-optimized families
only; CPU is fair evidence for a general-purpose instance), Aurora
`PeakMemoryUsedGiB`, OpenSearch `PeakJVMMemoryPressure`. **Add a row whenever a
new downsize lever ships.** It was verified against two real pre-fix reports,
where it flagged exactly the 3 EC2 and 2 Aurora recs the audits had found by
hand.

S16 is the machine-checkable half of lesson C21 and keys off
`_REQUIRED_EVIDENCE_CONJUNCTS`: a lever billed only when TWO signals hold must
name both in its `AuditBasis.evidence`, so a later "fix" that swaps one signal
for the other trips the harness instead of shipping the mirror-image phantom.
**Add a row whenever a counted lever's billing condition is a conjunction.** It
was verified against the real pre-fix bnc report, where it flagged all 3 EKS
extended-support recs.

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

Completed ledgers (each carries its own reconciliation section):

| Scan | Headline before → after | Outcome |
|------|------------------------|---------|
| `afs-prod_eu-west-1_20260808.md` | $13,625.31 | 3 fixed, 1 by-design |
| `afs-prod_af-south-1_20260811.md` | $6,818.16 → **$3,854.58** | AFS-1/2/3 fixed, AFS-4 withdrawn, gp3 CRITICAL refuted |
| `M360_ap-south-1_20260811.md` | $4,841.06 → **$2,468.62** | M360-1/3 fixed, M360-2 withdrawn |
| `bnc_ap-southeast-1_20260812.md` | $2,829.03 → **$2,163.19** | BNC-1/2/3/5 fixed + RECONCILED, 5 refuted; lessons C21/C22/C23/F6, sweep S16 |
| `level-Shoes-prod_eu-west-1_20260812.md` | $2,106.29 → **$2,440.91** | LS-1/2/3/4 fixed + RECONCILED, 6 refuted; lesson C24. First headline to RISE |

**Record withdrawals and refutations in the ledger, not just fixes.** Three of
nine candidates across the two 2026-08-11 audits did not survive Layer 3, and
two of those were killed only by checking before implementing. A ledger that
lists only what was fixed teaches the next auditor nothing about what looked
like a bug and wasn't.

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
2. Fix; predict the exact headline delta the fix will cause — **using the
   formula the CODE uses, not the one used to size the finding**. A lever
   measured over a trailing CE window scaled to 30 days does not equal the same
   charge at a 730-hour run rate, and CE keeps posting inside that window; the
   level-Shoes ElastiCache prediction missed by $22.72 for exactly this reason
   while the implementation was correct. Where a lever reads a live CE pool,
   state that the figure MOVES between scans and predict the mechanism, not just
   the number.
3. Operator re-scans the same profile/region; the headline must move by
   EXACTLY the predicted amount. Anything else reopens the finding.
4. Regression gate: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
5. If the bug class is new or recurring, append it to `_LIVE_AUDIT_LESSONS.md`
   and, when machine-checkable, add a sweep to `tools/output_audit.py`.
