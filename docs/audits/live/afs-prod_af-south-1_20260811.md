# Output Audit — afs-prod / af-south-1 / 2026-08-11

- **Report**: `afs-prod_af-south-1.html` (account 370525687312), log
  `afs-prod_af-south-1.log`
- **Headline**: $6,818.16/mo counted, 149 counted recs + 93 advisories,
  14 services with findings of 34 scanned
- **Account context**: **no active SPs/RIs** in the window (log confirms
  `ce:GetSavingsPlansUtilization: no data`), so the C6 commitment-coverage
  sweeps are moot. Role has CE access (RI/SP purchase recommendations returned).
  Prior audit of the same account: `afs-prod_eu-west-1_20260808.md`.
- **Method**: OUTPUT_AUDIT_PROTOCOL Layers 1–3. Layer 1 harness; Layer 2a rate
  verification against the live AWS Pricing API; Layer 3 adversarial refute
  before listing — which killed one candidate CRITICAL (see "Refuted").

## Verified correct (audit passes)

| Check | Result |
|-------|--------|
| Layer 1 harness | **0 FAIL**, 3 WARN, 4 INFO |
| S1 headline reconciliation | $6,818.16 = sum of 6 tab totals, to the cent |
| S13 per-tab reconciliation | every tab = sum of its counted recs (EC2 via camelCase CoH field, EBS via `EstimatedMonthlyCost` on unattached — F1/F2 traps navigated) |
| Double-count surfaces | EC2 CoH ∩ enhanced_checks instance ids: **empty**. EBS CoH-Delete ∩ unattached volume ids: **empty**. AMI: 186 snapshots referenced, **0** counted by more than one AMI (A3 clean) |
| EC2 rate verification | all 5 Windows rates EXACT vs live Pricing API (below) |
| EC2 arithmetic | $404.7412 → $404.74 etc., exact to the cent at full precision |
| C11 pool reconciliation | AMI counted $302.24 = 6.8% of the $4,420.51 actual billed snapshot pool (`ReconciliationFactor` 0.1485 applied to a $2,035.46 naive upper bound) — not a 100%-of-pool tell |
| Stale CoH drops | 15 in-use volume delete recs correctly dropped (E-series guard working) |
| **LS-7 (Database SP)** | **First live validation.** `DATABASE_SP` card produced at exactly 1yr / No Upfront — the only combo AWS sells — worth $778.26 |
| **LS-7 non-overlap rule** | **Prevented a real double count**: DATABASE_SP $778.26 lost `max()` to database RIs $3,056.85. Naive sum would have projected $19,431.84 instead of $18,653.58 |
| Projection arithmetic | 15,596.73 (EC2 Instance SP) + 3,056.85 (database RIs) = $18,653.58 exactly; basis string matches |
| **LS-6 (advisory count)** | live — `total_advisory_recommendations: 93` present and rendered |
| EC2-01 / EBS-01 (prior ledger) | **RECONCILED on live evidence** — numeric `EstimatedMonthlySavings` now present on rightsizing and gp2 emits |
| RI card duplication | none — repeated instance types are distinct (region, platform) pairs |

## Live Pricing API verification (Layer 2a)

af-south-1, Windows, Shared/Used/NA/No-License-required:

| Instance type | Scanner | Live API | Verdict |
|---------------|---------|----------|---------|
| r6i.8xlarge | $4.1600/hr | $4.1600 | EXACT |
| r6i.4xlarge | $2.0800/hr | $2.0800 | EXACT |
| r6i.2xlarge | $1.0400/hr | $1.0400 | EXACT |
| r8i.2xlarge | $1.1089/hr | $1.10888 | EXACT |
| r8i.xlarge | $0.5544/hr | $0.55444 | EXACT |
| gp3 storage | — | $0.1047/GB-Mo | (used below) |
| gp3 IOPS | — | $0.0065/IOPS-Mo | (used below) |
| gp3 throughput | — | $53.6576/GiBps-mo | (used below) |

## Findings

### AFS-1 — memory-optimized instances downsized on a CPU-only signal (C10) — HIGH

All three counted EC2 rightsizing recs — **$2,682.34/mo, 39% of the entire
headline** — are **r-family (memory-optimized)** instances whose recommendation
**halves their RAM**, gated on CPU alone:

| Instance | Change | RAM | Gate | Counted |
|----------|--------|-----|------|---------|
| `i-0b2e25be910fac823` | r6i.8xlarge → r6i.4xlarge | 256 → 128 GiB | avg CPU 1.8%, max 12.6% | $1,518.40 |
| `i-0bad855b6ad4b7288` | r6i.4xlarge → r6i.2xlarge | 128 → 64 GiB | avg CPU 1.2%, max 19.3% | $759.20 |
| `i-0447467b36b8732d7` | r8i.2xlarge → r8i.xlarge | 64 → 32 GiB | avg CPU 4.0%, max 24.6% | $404.74 |

The rates and arithmetic are EXACT (table above) — the defect is **evidential**.
`services/ec2.py:225` computes
`memory_bound = mem_pct is not None and mem_pct > 80.0`, and `mem_pct` comes
from `CWAgent mem_used_percent`, which requires the CloudWatch agent. The
docstring is explicit that memory "only suppresses; absent memory data leaves
prior behaviour unchanged" — so on the overwhelmingly common no-agent account,
unknown memory resolves toward **counting**. That inverts the project's tie-break
rule, which resolves ambiguity toward under-counting.

Low CPU on an r-family instance is the *expected* signature of a memory-bound
workload — 256 GiB is the reason r6i.8xlarge was chosen. These are Windows hosts
moving 7.8 GB/hr and 6.2 GB/hr of network respectively, so they are not idle.

Recurring, not incidental: the prior eu-west-1 audit of this same account
verified `$680.36/mo` of r6i.4xlarge→2xlarge Windows rightsizing as arithmetically
EXACT and did not question the memory evidence either.

- Suspect: `services/ec2.py:208-230` (`_classify_utilization`), emit at `:594-643`.
- **FIXED 2026-08-11.** Option (a): a new `_rightsize_evidence_ok(instance_type,
  mem_pct)` gates whether a verdict may be COUNTED, leaving `_classify_utilization`
  untouched so the card still renders. Memory-optimized families (r/x/z/u) with
  no `mem_pct` are demoted via `_demote_for_missing_memory`, which mirrors the
  existing C8 ASG fail-closed convention: `Counted=False`, figure preserved in
  `AdvisoryEstimate`, and BOTH the numeric and the string zeroed (the EC2 tab
  totals by parsing `EstimatedSavings`, so zeroing only the numeric would have
  left the dollar in the headline).
- Applied to the **idle** verdict as well as rightsize: the idle dollar is the
  whole instance rather than a one-size delta, so leaving it counted would have
  left a bigger hole than the one being closed. It did not fire on this account
  only because `max_cpu` 12.6% exceeded the idle threshold.
- Storage-optimized (i/d) and accelerated (g/p/inf/trn) families have the same
  non-CPU binding dimension. Deliberately NOT included — no live evidence yet,
  and widening would suppress real savings on estates this audit never saw.
  Filed as a follow-up rather than silently bundled.
- Predicted delta on re-scan: **−$2,682.34** → headline **$4,135.82**, EC2 tab
  $1,043.22 (CoH only). Simulated against this scan's JSON: all three recs carry
  no `AvgMemory` field, confirming `mem_pct` was None, and the arithmetic lands
  on $4,135.82 exactly.

### AFS-2 — 91% of AMI recs are AWS Backup recovery points (NEW) — HIGH

51 of 56 AMI recs (**$281.26 of $302.24**, and 31,834 of 34,209 snapshot GB) are
AWS Backup-created images named `AwsBackup_<instance-id>_<uuid>`, spanning 17
source instances and 40–131 days old.

Two structural problems:

1. **The gate can never be false for this resource class.** The test is "not
   referenced by any running instance" — which is true of *every* backup image
   by construction. A backup exists precisely so nothing references it until it
   is needed. This will flag 100% of AWS Backup AMIs on every account that uses
   AWS Backup, forever.
2. **The recommended action is wrong and unsafe.** These are recovery points
   owned by an AWS Backup plan with its own retention lifecycle. Deregistering
   the AMI directly circumvents the plan, destroys recovery capability, and the
   scanner has no visibility of the plan's retention setting — a 40-day-old
   image under a 90-day policy is doing exactly its job. The legitimate cost
   lever is **shortening the backup plan's retention**, which is a different
   recommendation against a different resource.

`grep` for `AwsBackup` / `aws:backup` across `services/` returns **nothing** —
there is no AWS Backup awareness anywhere in the codebase.

- Suspect: `services/adapters/ami.py` / `services/ami.py` unused/old AMI gates.
- Fix: detect AWS Backup-managed images (tag `aws:backup:source-resource`, more
  robust than the name prefix) and either exclude them or re-target the
  recommendation at the backup plan's retention. Note the C11 reconciliation is
  working correctly and is not implicated.
- Predicted delta on re-scan if excluded: **−$281.26** → AMI tab $20.98.

### AFS-3 — a region-scoped report carries an account-wide projection (NEW) — MEDIUM

The executive summary renders "Projected commitment up to **$18,653.58/mo**" in
a report titled af-south-1, but **65% of the RI recommendations behind it
($11,912.98 of $18,283.73) are for eu-west-1**:

| Region | RI purchase recommendations |
|--------|------------------------------|
| eu-west-1 | $11,912.98 |
| af-south-1 | $6,370.75 |

The dominant term, the EC2 Instance SP card ($15,596.73), carries `region: None`
and families `c5, c5a, c6a, c6i, m5, r5, r6i, r8i, t3, t3a` — plainly
account-wide. CE's purchase-recommendation APIs are account-scoped and take no
region filter, so this is inherent to the source, not a computation error.

The figure is correctly kept OUT of the counted headline and labelled "requires
purchase — not in the counted total", and each RI card does display its own
region (28 `eu-west-1` occurrences in the HTML). But the headline figure carries
no scope qualifier, and the report contains **zero** instances of "account-wide"
or equivalent. Two concrete harms: the reader attributes the opportunity to
af-south-1, and **a second scan of eu-west-1 for this account would report the
same account-wide recommendations again** — this operator has already run one
(`afs-prod_eu-west-1_20260808.md`), so adding the two projections would double
count.

- Suspect: `reporter_phase_b.py` commitment card/summary copy.
- Fix: label the projection "account-wide (all regions)" wherever the aggregate
  is shown, and optionally break out the scan region's share.
- Predicted delta: **$0.00** (counted total unaffected — disclosure only).

### AFS-4 — counted cards worth cents (LOW)

`containers` emits 4 counted "ECR Lifecycle Management" cards totalling **$0.15**
($0.01, $0.01, $0.06, $0.07). Each renders as a recommendation whose dollar
rounds to a cent. Same principle as the LS-4 materiality floor just added to the
never-expiring-log-group check (a card whose own figure rounds to $0.00 refutes
itself); these clear $0.00 but not by enough to be a recommendation.

- Predicted delta if floored: **−$0.15**.

## Coverage gaps (savings ABSENT from the $6,818.16)

- **me-south-1 S3 buckets** — endpoint unreachable from the scan host (connect
  timeout to `afs-ai-prod.s3.me-south-1.amazonaws.com`); their sizes and
  lifecycle findings are missing. **Recurring** — the same gap was recorded in
  the eu-west-1 audit three days earlier, so it is a scan-host networking issue,
  not a transient.
- **3 volumes skipped IOPS rightsizing** — no CloudWatch IOPS data in the 14-day
  window (`vol-01d216bd0afc90172`, `vol-0f2fa1c25f16c30a9`,
  `vol-0d89d6f8c4462e430` — all three also appear in the CoH stale-delete list).
- **Bedrock** — `ListProvisionedModelThroughputs` AccessDenied (correctly raised
  as a permission issue, not swallowed); `ListKnowledgeBases` 500 after 10
  retries; `ListCustomModels` "Unknown operation" (the API does not exist in
  af-south-1). Any Bedrock spend is unreported.
- **Lightsail / App Runner** — no regional endpoint in af-south-1. These surface
  as generic connection-failure warnings rather than "service not available in
  this region"; harmless, but they read as scan errors.
- **FSx file caches** — feature not enabled for the account (2 identical
  warnings; the duplicate suggests the call is made twice).

## Refuted during Layer 3 (recorded so it is not re-raised)

**Candidate CRITICAL — gp3 unattached volumes priced at ~11× the storage rate.**
All 18 unattached volumes are 25 GB gp3 costing **$28.6675/mo** each, where
25 GB × $0.1047 = $2.6175. The $26.05 excess decomposes *exactly* as
3,000 IOPS × $0.0065 = $19.50 plus 125 MB/s (0.12207 GiBps) × $53.6576 = $6.55 —
which looked precisely like charging for gp3's free baseline.

**Refuted**: `services/ebs.py:385,406` explicitly subtract both baselines
(`max(0, iops - 3000)`, `max(0, throughput - 125)`). The same $28.6675 is
reproduced exactly by volumes genuinely provisioned at **6,000 IOPS and
250 MB/s**, which is the only reading consistent with the code. Deleting such a
volume does stop the IOPS and throughput charges, so counting them is correct.

⚠️ **Open verification item** — this could not be confirmed against the account
(the `afs-prod` profile could not assume its role from this host:
`InvalidClientTokenId`). If those 18 volumes are actually at the 3,000/125
defaults, the finding flips back to CRITICAL with **$468.90** phantom counted.
One command settles it:
`aws ec2 describe-volumes --profile afs-prod --region af-south-1 --filters Name=status,Values=available --query 'Volumes[].{id:VolumeId,iops:Iops,tp:Throughput}'`

## Status

| ID | Class | Severity | Claim | Status |
|----|-------|----------|-------|--------|
| AFS-1 | C10 | HIGH | $2,682.34 counted on CPU-only evidence for memory-halving downsizes of r-family instances | **FIXED 2026-08-11** — simulated delta −$2,682.34 exactly; RECONCILED pending re-scan |
| AFS-2 | NEW | HIGH | $281.26 counted against AWS Backup recovery points; gate structurally always-true, action unsafe | CANDIDATE |
| AFS-3 | NEW | MEDIUM | $18,653.58 projection is account-wide (65% eu-west-1) in an af-south-1 report, undisclosed | CANDIDATE |
| AFS-4 | B3-adjacent | LOW | 4 counted cards totalling $0.15 | CANDIDATE |
| — | — | — | gp3 unattached pricing | REFUTED (pending the describe-volumes check above) |

If AFS-1, AFS-2 and AFS-4 are all fixed, the predicted headline is
**$3,854.41/mo** (from $6,818.16) — a 43% reduction, all of it removal of
dollars that were not defensible rather than newly-found savings.
