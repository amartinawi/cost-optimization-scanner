# Output Audit — afs-prod / eu-west-1 / 2026-08-08

- **Report**: `afs-prod_eu-west-1.html` (account 370525687312), scan JSON
  `cost_optimization_scan_eu-west-1_20260808_020157.json`
- **Headline**: $13,625.31/mo counted, 694 recommendations, 34 services
- **Account context**: no active SPs/RIs in window (C6 commitment sweeps moot);
  role has CE access; me-south-1 S3 endpoint unreachable from scan host.
- **Method**: OUTPUT_AUDIT_PROTOCOL.md Layers 1-3. Layer 1 harness + scan_doctor
  + audit_upper_bounds; Layer 2 re-derived flagged dollars against the live AWS
  Pricing API and cross-checked every double-count surface; Layer 3 refuted
  candidates before listing.

## Verified correct (audit passes)

| Check | Result |
|-------|--------|
| S1 headline reconciliation | $13,625.31 = sum of 10 tab totals, to the cent |
| S13 per-tab reconciliation | every tab total = sum of its counted recs (EBS via CO nested field) |
| Double-count surfaces (A3/A1) | CoH∩unattached, CoH∩gp2, CoH∩CO, CoH∩enhanced-checks volume/instance overlaps ALL empty; no shared snapshots across counted AMI recs |
| EC2 rightsizing dollars | `$680.36/mo` (r6i.4xlarge→2xlarge Windows) verified against live Pricing API: $1.864/hr → $0.932/hr exactly; three DISTINCT instances (not dupes) |
| EBS Compute Optimizer | $22.00 × 3 vols (gp3 6000→3000 IOPS) present in tab total AND rendered (reporter_phase_b.py:526) |
| C8 upper bounds | audit_upper_bounds: no counted saving rests on an uncorroborated bound |
| C11 pool-share | AMI tab $1,484.79 built from actual stored snapshot blocks (FullSnapshotSize), not the billed pool |
| Count semantics (D3/D4) | summary counts recompute exactly; all rendered services have tab+panel |
| Stale CoH drops | 47 in-use volume delete recs correctly dropped (E-series guard working) |

## Live Pricing API (MCP) verification

Every dollar-bearing figure in the findings was re-derived from the AWS Pricing
API (pricing MCP, price list version 2026-08-06):

| Figure | Scanner | Live API | Verdict |
|--------|---------|----------|---------|
| EC2 rightsizing $680.36/mo | $1.864→$0.932/hr Windows r6i.4xl→2xl | $1.8640 / $0.9320 exact | EXACT |
| gp2→gp3 storage delta | $0.022/GB-mo | gp2 $0.11 − gp3 $0.088 | EXACT |
| gp3 IOPS-parity rate | $0.0055/IOPS-mo | EU-EBS:VolumeP-IOPS.gp3 $0.0055 | EXACT |
| EBS CO $22.00/vol | AWS CO figure | re-derived: 3000 IOPS × 0.0055 + 125 MiBps × (45.056/1024) = $22.00 | EXACT |
| Aurora backup rate | $0.021/GB-mo | EU-Aurora:BackupUsage $0.021 (both engines) | EXACT |

RDS-01 is a `Counted` flag defect (structural, verified in JSON + source);
pricing does not apply. Account-side CE cross-checks were not required: no
surcharge-type recs in this report and audit_upper_bounds passed.

## Findings

### EC2-01 — counted rightsizing dollars exist only as strings (B3) — LOW

3 `enhanced_checks` recs (`i-062c18039a98439dc`, `i-045e487f2617aaafe`,
`i-02a99c26f74ef638f`) carry `EstimatedSavings: "$680.36/month if rightsized"`
with **no numeric field**. The dollars ARE in the tab total (adapter sums
internally — verified) and the amount is correct to the cent, but any JSON
consumer reading numeric fields sees $0 — the exact EIP trap from B2/B3.
- Suspect: `services/adapters/ec2.py` enhanced_checks rightsizing emit.
- Fix: emit `EstimatedMonthlySavings: 680.36` alongside the string.
- Predicted delta on re-scan: **$0.00** (display/consumer hygiene only).

### EBS-01 — gp2→gp3 dollars exist only as strings (B3) — LOW

Same class: 7 `gp2_migration` recs ($6.14/mo total, sizes 1-100 GB) carry the
dollar only in `EstimatedSavings` (AuditBasis present, numeric absent).
- Suspect: `services/adapters/ebs.py` gp2_migration emit.
- Fix: emit the numeric alongside the string. Predicted delta: **$0.00**.

### RDS-01 — advisory-by-string recs count as recommendations (D4) — MEDIUM

3 Aurora cluster-snapshot recs whose size the API does not report emit the
advisory string (`"advisory — snapshot size not reported…"`, numeric 0.0) but
**never set `Counted=False`** — `services/rds.py:130` `_snapshot_savings_text`
returns the advisory tuple, the caller keeps the rec counted. They inflate the
headline count (3 of the 694) and appear in priority filters as real
recommendations.
- Snapshots: `pre-april13-upgrade-dbmigrations` + 2× `tibco-prod-*` (alloc 0 GB).
- Fix: set `Counted=False` (+ keep numeric 0.0) on the size-unreported branch.
- Predicted delta on re-scan: count **694 → 691**, headline **$ unchanged**.

### EKS-01 — projection advisories carry `monthly_savings` ≠ 0 (B1-ii, by design) — INFO

7 `node_group_optimization` advisories (Spot/Graviton what-ifs, $2,087.65/mo
combined) are `Counted=False` with non-zero `monthly_savings`. Adversarial
verdict: **legitimate projection** — born advisory (never demoted), excluded
from the tab total (365.00 = counted only), the counted dollar belongs to the
EC2 tab, and the code documents mutually-exclusive levers. Same pattern as
commitment_analysis purchase scenarios. Harness now exempts this source;
optionally move the figure to `PotentialMonthlySavings` for strict B1 hygiene.

## Coverage gaps (savings ABSENT from the $13,625.31)

- **me-south-1 S3 buckets** — endpoint unreachable from scan host; sizes and
  lifecycle findings missing. Re-scan from a host that can reach me-south-1 or
  via `--scan-only s3` with connectivity.
- **6 volumes skipped IOPS rightsizing** — no CloudWatch IOPS data in 14-day
  window (includes 5 of the CoH stale-delete volumes).
- **Aurora cluster snapshots with unreported size** — real backup charges,
  unquantifiable at scan time (the RDS-01 advisories).

## Harness changes made during this audit (method traps, not scanner bugs)

- F5: Compute Optimizer recs carry dollars in nested rank-1
  `volumeRecommendationOptions/recommendationOptions → savingsOpportunity`;
  flat-field sweeps false-flagged them $0 (3 EBS false positives + phantom $66
  tab delta).
- Added S13 per-tab reconciliation as a permanent sweep.
- `eks_cost/node_group_optimization` added to projection exemptions.

## Status

| ID | Class | Severity | Status |
|----|-------|----------|--------|
| EC2-01 | B3 | LOW | FIXED (2026-08-08) — numeric mirrors added to idle/rightsizing/ASG emits (`services/ec2.py`) |
| EBS-01 | B3 | LOW | FIXED (2026-08-08) — numeric mirror added to gp2 emit (`services/adapters/ebs.py`) |
| RDS-01 | D4 | MEDIUM | FIXED (2026-08-08) — `Counted=False` on size-unreported branch, both sites (`services/rds.py`) |
| EKS-01 | B1-ii | INFO (by design) | CLOSED — projection |

Fix verification (simulated on this scan's JSON, per protocol fix-loop step 2):
count 694 → 691 (exactly the 3 RDS advisories), headline delta $+0.00 — matches
the predicted deltas. RECONCILED pending the next live re-scan of this
profile/region. Tests: `tests/test_output_audit_remediation.py` (5 tests pin
the exact failing scenarios, dollar AND `Counted` state).

Open question parked for the next commitment-account audit: `_demote`
(`services/commitment_coverage.py`) keeps the original numeric on demoted recs
(renderer displays it as the indicative figure) — this conflicts with B1's
letter and will trip sweep S2 on any account with active SPs/RIs. Decide
renderer-vs-field convention there, not here.
