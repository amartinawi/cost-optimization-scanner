# Backlog Reconciliation — Validation

- **Date:** 2026-08-10
- **Subject:** Independent validation of the "Reconciled backlog" section of
  `docs/audits/SWEEP-FALSE-NEGATIVES-2026-08-09.md` (the `153 / 69 / 84`
  reconciliation and the eight still-open severe findings).
- **Scope of the recon under test:** `docs/audits/SWEEP-FALSE-NEGATIVES-2026-08-09.md`,
  lines 75-155 (Reconciled backlog) and the tranche fix-status prose at
  lines 161-233, cross-read against the three findings sections (CRITICAL
  L18-26, HIGH L28-49, MEDIUM/LOW L51-73).
- **Methodology:** five parallel verifiers across two waves, each re-derived
  from the current code and botocore service models rather than the recon's
  own "Verified: ..." text. None of the verifiers trusted the recon's
  verified-claims as evidence; each treated them as the hypothesis under test.
  The five inputs were:
  1. `w1a-severe.md` — ATH-1, RS-A, QS-1 (all OPEN-CONFIRMED at HEAD).
  2. `w1b-severe.md` — QS-2/QS-3 (LATENT-CONFIRMED), MS-1/MS-2
     (OPEN-CONFIRMED).
  3. `w1c-fs2.md` — FS-2 (OPEN-CONFIRMED) plus the FS-4 adjacency proof.
  4. `w2-arithmetic.md` — independent re-derivation of 153 / 69 / 84.
  5. `w2-severity.md` — original-vs-reconciled severity carry-over spot-check.
- **Read of working tree:** this validation was performed read-only. The
  only artifact written is this file.

## VERDICT

**SOUND-WITH-CAVEATS.** Every load-bearing load claim of the recon reproduces
under independent re-derivation:

- **8/8 severe findings** independently OPEN-CONFIRMED or LATENT-CONFIRMED
  (no refutation, no partial).
- **153 / 69 / 84 arithmetic** reproduced two independent ways
  (top-down per-tranche parse and bottom-up `153 − remaining`), partition
  exactly, and tie out to `5 + 34 + 114`, `12+11+12+6+6+9+6+0+3+4`, and
  `8 severe + 76 M/L` respectively.
- **Severity carry-over is faithful** — zero re-rating; the four CRITICALs
  that left the remaining list (SM-1, BR-1, BR-2, NC-1) dropped out because
  they landed in tranche 1, not because they were downgraded.

The verdict is SOUND-WITH-CAVEATS rather than plain SOUND because two
methodology-level catches surfaced during the re-derivation, and one
provenance-level correction to the validation task itself must be surfaced
for the recon to be acted on safely:

1. **The CF-5 tension is a real doc-level inconsistency**, not a transcription
   quibble. Tranche-2 prose (L163) claims "CF-1/CF-2/CF-5 (advisory hygiene +
   dead branch)" landed; the recon's own final remaining list (L133) enumerates
   CF-5 as still open under `cloudfront (3): CF-3, CF-5, CF-6`. The recon's own
   `153 − 84 = 69` is only self-consistent if CF-5 is **open**; the post-T10
   remaining list therefore wins and CF-5 is correctly excluded from the 69.
   But the tranche-2 prose contradicts that conclusion and the user should
   clarify CF-5's status in the recon doc itself.
2. **The 69 is correct but four of its ids hide behind plumbing labels**
   (BR-5 = "report #33" in T1; EC2-8 = "reporter" in T3; AUR-A and DDB-D =
   "rank 7 other half" in T4). A naive id-grep over the tranche prose
   under-counts the landed set to 65, not 69. This is a counting-method
   caveat for any future re-derivation, not a defect in the recon's number.

These are surfaced in the body, not buried. They do not change the SOUND
verdict on the recon's conclusions; they qualify how confidently the reader
can re-derive those conclusions from the recon's prose alone.

> **Provenance correction (read this before acting on the recon).** The
> validation brief stated HEAD was `7253507` and the working tree was clean.
> At the moment this report was written, the repository was at HEAD
> `4e838fff` — one commit ahead of `7253507` (the single intervening commit
> `4e838ff` is `fix(mediastore): delete the counted path that never fired
> (MS-1, MS-3, MS-4)`) — and the working tree carried two uncommitted
> modifications (`core/pricing_engine.py`, `services/athena.py`). The
> `services/athena.py` modification is unambiguously an **in-progress ATH-1
> remediation**: it removes the `"Up to 75% scan-cost reduction"` seed string
> and replaces the seeded rec with `Counted=False, EstimatedMonthlySavings=0.0`,
> with a docstring that explicitly names ATH-1. None of the five input
> notepads observed this working-tree state (they all recorded HEAD `7253507`
> and a clean tree). The severe-finding verdicts below were re-derived at
> `7253507` and are valid for that commit; at the current `4e838fff` HEAD
> with the uncommitted athena.py change applied, **ATH-1's OPEN-CONFIRMED
> status would no longer hold** (the counted 0.75 path has been removed).
> See the Provenance section for the full accounting and the recommendation
> this triggers.

## Provenance

- **HEAD recorded by the five notepads:** `72535074b1572e39eefb2d5c133a01519f93fd5e`
  (short `7253507`), confirmed via `git rev-parse --short HEAD` at the time
  of each notepad. ATH-1, RS-A, QS-1, QS-2, QS-3, MS-1, MS-2, FS-2 were all
  re-derived against this commit.
- **HEAD observed at the moment this report was written:** `4e838fff49e61a0a2755dac808e1bdbb7e792f78`.
  Relationship: `7253507` is an ancestor of `4e838ff` (`git merge-base
  --is-ancestor` confirms); the only commit in the `7253507..4e838ff` range
  is:
  ```
  4e838ff fix(mediastore): delete the counted path that never fired (MS-1, MS-3, MS-4)
  ```
  This is itself a (landed) remediation of the MS-1 finding confirmed open
  in the notepads. At the notepads' HEAD `7253507` the MS-1/MS-2 findings
  were genuinely open; at `4e838ff` the mediastore counted path no longer
  exists. The OPEN-CONFIRMED verdicts in the table below describe the state
  the recon was written against (`7253507`).
- **Working-tree state (read-only discipline):** this validation did not edit
  product code, prompts, or existing audit docs, and did not run `git add`.
  However, the working tree was **not** clean when this report was written.
  `git status --short` showed:
  ```
   M core/pricing_engine.py
   M services/athena.py
  ```
  plus four untracked root files (`findings.md`, `progress.md`, `task_plan.md`,
  and `.zcode/`). The brief's "git diff empty" assertion was not honoured by
  the environment between notepad-time and report-time. The two modifications
  are external to this validation (not made by it) and are described in the
  Provenance correction above; they are flagged because they change whether
  ATH-1's verdict still holds at the current HEAD.
- **botocore model source:** `/usr/local/aws-cli/v2/2.35.22/dist/awscli/botocore/data/`,
  used for the RS-A `ResourceType` enum probe and the QS-1
  `DescribeSpiceCapacity` / `ListDataSets` / `DescribeDataSet` operation
  checks. Probed via direct JSON read of `service-2.json` and independently
  via a live `boto3.client(...).hasattr` check (botocore 1.43.67 in the
  notepad cohort that performed the live probe, 1.43.47 in the cohort that
  performed the SPICE price-list probe).

## Severe findings table (8 rows — the heart of this doc)

| finding_id | service | claimed severity | independent verdict | independent evidence | note |
|---|---|---|---|---|---|
| **ATH-1** | athena | CRITICAL | OPEN-CONFIRMED | `services/adapters/athena.py:71` (at `7253507`): `rec_savings = monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75`. The only gate to reach line 71 is `monthly_tb > 0`; no check anywhere in the scan path reads workgroup storage format, table format, compression, or partition state. The `0.75` comment itself admits it "assumes" the workgroup is uncompressed CSV/JSON. | Counted-$ defect — the inflated figure is summed into `total_monthly_savings` and rendered as a concrete account saving for every workgroup, including ones already 100% Parquet/partitioned where the achievable saving is ~0%. **Provenance caveat:** at the current `4e838ff` HEAD with the uncommitted `services/athena.py` change applied, the 0.75 path has been removed — see Provenance correction. This OPEN-CONFIRMED verdict describes `7253507`. |
| **RS-A** | redshift | HIGH | OPEN-CONFIRMED | (a) botocore `cost-optimization-hub/2022-07-26/service-2.json`: `RedshiftCluster in enum: False`; the only redshift-ish `ResourceType` is `RedshiftReservedInstances`, which routes to `commitment_analysis`, not the redshift tab. (b) `services/adapters/redshift.py`: `savings` initialised to `coh_total` only (`:87`); every heuristic rec is force-set `EstimatedMonthlySavings=0.0, Counted=False` (`:93-127`). (c) `scan_orchestrator.py:56-64,132-138` carry dated 2026-08-09 code comments stating the redshift CoH bucket "can no longer RECEIVE recs" because the `type_map` keys were removed. | Structural blind spot — an entire service contributes a permanent $0 to the account total while the tool claims to scan "34 AWS services". Known-and-acknowledged in the orchestrator's own code comments; not latent. |
| **QS-1** | quicksight | HIGH | OPEN-CONFIRMED | (a) botocore `quicksight/2018-04-01/service-2.json`: `DescribeSpiceCapacity in ops: False`; only `UpdateSPICECapacityConfiguration` (write-side) and `ListUsersIndexCapacity` (Q-index, different namespace) exist. (b) live `boto3.client('quicksight').hasattr('describe_spice_capacity')` returns `False` (botocore 1.43.67). (c) `services/quicksight.py:92` `spice_supported = hasattr(quicksight, "describe_spice_capacity")` is therefore always `False`, so the entire counted SPICE path (`:104-154`) is dead on real AWS. | `ListDataSets`/`DescribeDataSet` do exist (confirmed) and expose `ConsumedSpiceCapacityInBytes`/`TotalSpiceCapacityInBytes` per dataset — the remediation primitives — but neither is called in the current code, so they do not rescue the lever today. |
| **QS-2** | quicksight | HIGH | LATENT-CONFIRMED | Latent behind QS-1: would fire at `services/adapters/quicksight.py:74` `rec_savings = round(unused_gb * rate * ctx.pricing_multiplier, 2)` once the dead `elif` is unblocked. AWS Price List API confirms SPICE per-GB-month is flat-global ($0.25 Pro / $0.38 Enterprise) across us-east-1, eu-west-1, ap-south-1, ap-southeast-2 — only the SKU prefix carries the region. Region-scaling a flat-global rate is the C1 shape (`_LIVE_AUDIT_LESSONS.md:109-116`). | C1 invisible in us-east-1 (`pricing_multiplier==1.0`); overstates by the local multiplier elsewhere. Severity correctly down-rated from CRITICAL because it under-states in the default region. |
| **QS-3** | quicksight | HIGH | LATENT-CONFIRMED | Latent behind QS-1: `services/quicksight.py:106-119` reads the account-wide pool and computes `unused_gb = round(total_capacity - used_capacity, 2)`; `adapters/quicksight.py:74` then credits the entire `(purchased_total - used)` delta with no floor for the per-Author bundled-free SPICE (10 GB/Author). C11 shape (`_LIVE_AUDIT_LESSONS.md:277-291`). | The "~4.5x" multiplier in the recon is **illustrative, not a constant** — the ratio depends on the (Authors, purchased_total, used) tuple and could not be reproduced as a fixed value. The structural defect (no Author-entitlement floor) is real. |
| **MS-1** | mediastore | HIGH | OPEN-CONFIRMED | `services/mediastore.py:88-90` queries `Namespace="AWS/MediaStore", MetricName="BucketSizeBytes"`. `BucketSizeBytes` is an AWS/S3 CloudWatch metric; the AWS/MediaStore namespace publishes request/throughput metrics only. `:97` then resolves `storage_gb == 0` for every container, and `services/adapters/mediastore.py:54` `if estimated_gb > 0:` is never true → every rec falls through to the `$0.00/month — advisory` path. `adapters/mediastore.py:61` records `AuditBasis.metric_window = "BucketSizeBytes 14-day Average (AWS/MediaStore)"` — the audit trail names a metric the namespace does not publish. | **Provenance caveat:** at the current `4e838ff` HEAD the mediastore counted path has been deleted by commit `4e838ff`; at `7253507` (the recon's HEAD) MS-1 is genuinely open as described. |
| **MS-2** | mediastore | HIGH | OPEN-CONFIRMED | `services/mediastore.py:109` `if activity_datapoints_seen > 0 and total_activity == 0:` is structurally unsatisfiable: an idle container emits no CloudWatch datapoints (so `activity_datapoints_seen == 0` → first clause false) and an active container has `total_activity > 0` (second clause false). Neither population satisfies the AND. The adapter-side `EstimatedStorageGB > 0` gate (cited by the recon) is the *second* gate and is independently fatal via MS-1. | MS-1 and MS-2 are **independently fatal and mutually reinforcing** — fixing one alone does not restore counted MediaStore savings; both must land together. |
| **FS-2** | file_systems | HIGH | OPEN-CONFIRMED | `services/file_systems_logic.py:81` (function `efs_lifecycle_net_savings`): `cold_gb = max(standard_gb - max(monthly_access_gb, 0.0), 0.0)`. `standard_gb` is stored capacity (GiB in Standard class, from `SizeInBytes`); `monthly_access_gb` is I/O throughput volume (CloudWatch `DataReadIOBytes + DataWriteIOBytes`, Sum over 30d, /1024^3) — different physical quantities. Reproduction: a 1 GB file read 100× gives `standard_gb=1`, `monthly_access_gb=100`, `cold_gb = max(1-100, 0) = 0`, `gross=0` → caller gate `efs_fsx.py:476` `if est.net_savings > 0 and est.cold_gb >= EFS_MIN_LIFECYCLE_GB` does not fire. | **FS-4 adjacency (explicitly noted):** FS-2 (line 81) and FS-4 (line 83) sit two lines apart in the same function and are distinct defects. `git show c131423` proves FS-4 only changed the access-fee line (`access_charge` → `_ = ia_access_rate`; return `EfsLifecycleEstimate(cold_gb, gross, 0.0, gross)`) and left line 81 byte-for-byte unchanged as context. FS-4 deliberately did not close FS-2. |

**Tally of the eight:**
- OPEN-CONFIRMED: 6 (ATH-1, RS-A, QS-1, MS-1, MS-2, FS-2)
- LATENT-CONFIRMED: 2 (QS-2, QS-3 — both latent behind QS-1's dead `elif`)
- OPEN-REFUTED: 0
- PARTIAL: 0

## Arithmetic reconciliation

All three numbers reproduce and are mutually consistent. Re-derived two
independent ways: top-down (per-tranche parse) and bottom-up (`153 − remaining`).

### 153 — sweep-enumerated findings

Read directly from the three findings sections and transcribed once each.
Excludes cross-references to ids owned by other sections or to external docs
(RDS-3, S3-2, SM-1 re-mentioned in M/L, MSK-1 re-mentioned in M/L, the
`[KNOWN]` restatements bullet at L73 which carries no new ids).

| section | line range | count | ids |
|---|---|--:|---|
| CRITICAL | L18-26 | 5 | SM-1, BR-1, BR-2, NC-1, ATH-1 |
| HIGH | L28-49 | 34 | EC2-2/3/4, EKS-1, AR-1, CN-1, RS-A, DDB-A, AUR-A, OS-1/2, MSK-1, FS-1/2/6, TR-1/2, LAM-1/2, CF-1/4, AG-3, MS-1/2, QS-1/2/3, MON-1/2, WS-1, H3/4/5, LS-1 |
| MEDIUM/LOW | L51-73 | 114 | (per-service; see `w2-arithmetic.md` table) |
| **total** | | **153** | (each id unique across sections) |

The load-bearing `+1` for 153 is **NET-A**, named only as a parenthetical
inside the M/L network bullet (L68). Without it the M/L total is 113 and the
grand total is 152. The number reproduces as 153 only when NET-A is counted.

**5 + 34 + 114 = 153.** REPRODUCES.

### 69 — landed across T1-T10

Parsed each tranche's fix-status paragraph and matched every landed item to a
sweep id. Counting rules that determined the 69: partials count as landed
(WS-1); deliberately-not-changed does not (GL-3); spans count each id once;
four plumbing labels map 1:1 to sweep ids and DO count (BR-5, EC2-8, AUR-A,
DDB-D); plumbing with no sweep-id mapping does NOT count (T8's CoH routing);
external-session commits do not count; ids invented during a tranche do not
count (T5's TR-3); and the recon's final remaining list is ground truth for
open-vs-landed disputes (this is what resolves CF-5 to open).

| T | merge | n | landed ids |
|--:|---|--:|---|
| 1 | `10f0b0c` | 12 | AR-1, OS-1, OS-2, NC-1, SM-1, SM-3, SM-2, BR-1, BR-2, BR-3, BR-4, BR-5 (=report #33) |
| 2 | `f6c6c2e` | 11 | LS-1, CF-1, CF-2, EC2-2, EC2-3, EC2-4, EKS-1, EKS-2, CN-1, MON-1, MON-2 |
| 3 | `e9ccd4f` | 12 | H3, H4, H5, DDB-A, DDB-C, LAM-1, LAM-2, WS-3, WS-1(partial), FS-1, FS-6, EC2-8 (=reporter) |
| 4 | `1ad837d` | 6 | TR-2, GL-1, NET-B, NET-E, AUR-A (=rank7 AuroraDbClusterStorage), DDB-D (=rank7 DynamoDbReservedCapacity) |
| 5 | `37b0fb9` | 6 | CF-4, AG-3, TR-1, MSK-1, MSK-5, MON-3 |
| 6 | `8a7f7b0` | 9 | NET-A, NET-D, NET-F, GL-4, DMS-1, DMS-4, NET-C, AUR-C, AUR-G |
| 7 | `f869e64` | 6 | LAM-3, OS-5, OS-4, OS-7, OS-9, MON-7 |
| 8 | `7cce9f5` | 0 | (systemic CoH routing — SageMakerEndpoint/WorkSpaces — no discrete sweep id) |
| 9 | `b6501e2` | 3 | FS-7, BR-6, MSK-3 |
| 10 | `f57a97f` | 4 | AG-1, WS-4, MON-8, FS-4 |
| **sum** | | **69** | |

Cross-tranche duplicate check: no id appears in two tranches. Independent
cross-check: `landed = (the 153 set) − (the explicit remaining list from
L92-153) = 69`, byte-identical to the per-tranche union. REPRODUCES.

### 84 — remaining

`153 − 69 = 84`. Confirmed both arithmetically and by counting the recon's
own "Still open" enumeration (L92-153):

- Severe open: **8** (1 CRITICAL: ATH-1; 7 HIGH: RS-A, QS-1, QS-2, QS-3,
  MS-1, MS-2, FS-2).
- MEDIUM/LOW open: **76** (per-service; each row's parenthetical count
  matches its actual id count, no mismatches).

**8 severe + 76 M/L = 84.** The 69 landed ids and the 84 remaining ids
partition the 153 set exactly: no id is both landed and open, no id is in
neither. REPRODUCES.

## Severity carry-over

Original-vs-reconciled severity, checked finding-by-finding for every id in
the original CRITICAL and HIGH sections, and via a 10-item sample for
MEDIUM/LOW. **Zero re-rating detected.**

### The 5 original CRITICALs

| id | service | original | in remaining list? | reconciled | reason |
|---|---|---|---|---|---|
| ATH-1 | athena | CRITICAL | yes (L94) | CRITICAL | faithful carry-over |
| SM-1 | sagemaker | CRITICAL | no | n/a | landed in tranche 1 (L161) |
| BR-1 | bedrock | CRITICAL | no | n/a | landed in tranche 1 (L161) |
| BR-2 | bedrock | CRITICAL | no | n/a | landed in tranche 1 (L161) |
| NC-1 | network_cost | CRITICAL | no | n/a | landed in tranche 1 (L161) |

The four CRITICALs that left the remaining list dropped out because they
landed with TDD and a green suite (per the tranche-1 fix-status paragraph),
**not** because they were downgraded. ATH-1 is correctly retained at
CRITICAL.

### Retained HIGHs

RS-A, QS-1, QS-2, QS-3, MS-1, MS-2, FS-2 — all seven were HIGH in the
original section and are HIGH in the reconciled remaining list. No
re-rating in either direction.

### 10/10 MEDIUM/LOW spot-check (clean)

Random selection across services from the reconciled M/L list (L127-153),
each checked against the original M/L section (L51-73): AM-2, AG-4, ATH-7,
AUR-F, BR-7, CF-5, DDB-B, EKS-4, GL-3, WS-6 — all ten retain their original
M/L classification. (GL-3 is the one tranche-6 explicitly considered and
deliberately left unchanged; it correctly stays in the remaining M/L list
at its original severity.)

## Caveats / honest methodology catches

These are the value-add of an independent re-derivation. They do not change
the recon's conclusions; they qualify how confidently the conclusions can be
re-derived from the recon's prose alone.

- **CF-5 tension (doc-level inconsistency).** Tranche-2 prose at L163 claims
  "CF-1/CF-2/CF-5 (advisory hygiene + dead branch)" landed. The recon's own
  final remaining list at L133 enumerates `cloudfront (3): CF-3, CF-5, CF-6`,
  i.e. CF-5 still open. The recon's own `153 − 84 = 69` is only
  self-consistent if CF-5 is **open**; the post-T10 remaining list wins, so
  CF-5 is correctly excluded from the 69. This is a real internal
  inconsistency in the recon doc — the tranche-2 prose over-claims. The
  arithmetic works because the final remaining list overrides the prose, but
  a future reader trusting the tranche-2 prose will get 70, not 69.
  **Recommendation:** the user should clarify CF-5's status in the recon doc
  (either correct the tranche-2 prose to drop CF-5, or land CF-5 and update
  the remaining list to `cloudfront (2)`).

- **4 ids hide behind plumbing labels.** Four of the 69 landed ids are not
  named by id in their tranche's fix-status prose; they hide behind plumbing
  labels that map 1:1 to sweep findings:
  - **BR-5** = "report #33" in T1 (sweep L71: "BR-5 ⊃ [KNOWN: #33]").
  - **EC2-8** = "reporter" in T3 (sweep L53: "EC2-8 demoted recs render
    full gross, CommitmentCoverageNote read by no renderer"; T3's
    `_render_ec2_advanced_checks now honors Counted` is exactly that).
  - **AUR-A** = T4 "rank 7 (other half): AuroraDbClusterStorage → rds".
  - **DDB-D** = T4 "rank 7 (other half): ... DynamoDbReservedCapacity →
    commitment_analysis".

  The mapping is provable from each plumbing label naming the exact
  resource/type the sweep id describes. But a naive id-grep over the tranche
  prose under-counts the landed set to **65**, not 69. The 69 is correct; it
  requires mapping plumbing labels to finding ids.

- **T8 contributes 0 to the 69.** Tranche 8 routes two more real CoH types
  (SageMakerEndpoint → sagemaker, WorkSpaces → workspaces) but the sweep's
  finding-id space has no id for "route these two CoH types" — the closest
  id (DDB-D) names the unconsumed-types class generally and was not landed
  by T8 (DDB-D is still open). Not a defect; a counting note. Anyone
  re-deriving the 69 by summing tranche contributions must apply the rule
  that plumbing outside the finding-id space is excluded.

- **QS-3's "~4.5x" is illustrative, not a fixed multiplier.** The structural
  C11 defect (no Author-entitlement floor; the whole `(purchased − used)`
  delta credited without netting the bundled-free 10 GB/Author) is real and
  would fire the moment QS-1 lands. But the exact overstatement ratio
  depends on the (Authors, purchased_total, used) tuple — e.g. 5 Authors /
  100 GB purchased / 5 GB used gives ~1.9x; a pool dominated by bundled-free
  capacity gives a higher ratio. The 4.5x figure should be worded as
  "scenario-dependent overstatement", not a constant.

- **QS-2's C1 region-scaling error is invisible in us-east-1.** The flat
  SPICE rate is multiplied by `ctx.pricing_multiplier`, which is 1.0 in
  us-east-1 and >1.0 elsewhere. The defect only manifests in regions where
  the global flat rate differs from the local multiplier — us-east-1-only
  accounts see no error. The HIGH severity is correct for the general case
  but should not be presented as equally severe for us-east-1-only accounts.

## Recommended next actions (prioritized)

- **P1 — fix the 8 severe findings.** These are the highest-value targets:
  1 CRITICAL (ATH-1) + 7 HIGHs, all genuinely open at `7253507` and confirmed
  independently. Recommended order:
  1. **ATH-1 first.** It is the only CRITICAL and has been escalated three
     times across the audit/verification/sweep runs. It is a counted-$
     overstatement on every Athena workgroup, including ones already
     Parquet/partitioned where the saving is ~0%. **Note:** an in-progress
     ATH-1 remediation is already in the working tree as an uncommitted
     `services/athena.py` modification (see Provenance correction) — that
     work should be reviewed and landed (or replaced) rather than re-started.
  2. **MS-1 + MS-2 together.** They are independently fatal and mutually
     reinforcing; fixing one alone does not restore counted MediaStore
     savings. **Note:** commit `4e838ff` at the current HEAD has already
     deleted the mediastore counted path (a different remediation
     strategy — retiring the dead path rather than fixing the metric/gate);
     confirm that strategy is intended before doing further MS-1/MS-2 work.
  3. **QS-1 + QS-2 + QS-3 together.** QS-2 and QS-3 are latent behind QS-1's
     dead `elif`; fixing QS-1 alone would unmask both. Land all three as a
     unit so QS-2 (region-scaling a flat-global rate) and QS-3 (no
     Author-entitlement floor) are addressed before the SPICE path goes live.
  4. **RS-A** (structural — Redshift tab is permanently $0; needs a local
     counted lever since `RedshiftCluster` is not a CoH ResourceType).
  5. **FS-2** (dimensional error in `efs_lifecycle_net_savings`; distinct
     from the already-landed FS-4, confirmed via `git show c131423`).

- **P2 — clarify the CF-5 status in the recon doc.** Resolve the
  landed-vs-open tension between the tranche-2 prose (L163, claims landed)
  and the final remaining list (L133, lists open). Either correct the prose
  or land CF-5 and update the remaining list. This is a doc-level fix; it
  does not affect any code.

- **P3 — the 76 MEDIUM/LOW tail.** Lowest priority. The tail was
  arithmetic-spot-checked (counts + a 10-item severity sample) but not
  verified finding-by-finding; treat the 76 as a count, not as 76
  individually-confirmed defects.

## Honest limitations of this validation

- **The 76 MEDIUM/LOW tail was not verified finding-by-finding.** It was
  spot-checked two ways: (a) each row's parenthetical count matches its
  actual id count in the recon's explicit enumeration, and (b) a 10-item
  random sample retained its original M/L severity. Whether each of the 76
  is genuinely open at HEAD was not re-derived. The severe 8 (1 CRITICAL +
  7 HIGHs) were verified finding-by-finding; the 76 was not.
- **No live AWS scan was run.** QS-1's `DescribeSpiceCapacity` absence and
  `ListDataSets`/`DescribeDataSet` presence were confirmed against the
  botocore service model and a live `boto3.client(...).hasattr` check, but
  whether `describe_data_set` actually exposes the capacity fields the user
  claims (`ConsumedSpiceCapacityInBytes` / `TotalSpiceCapacityInBytes`) was
  not verified against a live QuickSight account. The QS-2 SPICE price-list
  flatness WAS verified live against the AWS Price List API across four
  regions.
- **Independence was preserved by method, not by authorship.** The recon was
  authored by the same party asking for validation. Independence was
  achieved by re-deriving every load-bearing claim from current code and
  botocore models (treating the recon's "Verified: ..." text as the
  hypothesis under test), not by trusting the recon's prose. The CF-5
  tension and the 4-hidden-ids mapping are exactly the kind of catch that
  method surfaces; a prose-trust pass would have missed both.
- **Provenance drift between notepad-time and report-time.** The five input
  notepads all recorded HEAD `7253507` and a clean tree. At the moment this
  report was written, HEAD had advanced to `4e838ff` (one mediastore fix
  commit) and the working tree carried two uncommitted modifications
  including an in-progress ATH-1 remediation. The OPEN-CONFIRMED verdicts in
  the table describe `7253507`; at the current HEAD with the uncommitted
  changes, ATH-1's verdict would not hold and MS-1's counted path has been
  deleted. Anyone acting on this validation should re-verify ATH-1 and MS-1
  against the current HEAD before starting fix work — and should not assume
  the working tree is clean just because the validation brief said so.
