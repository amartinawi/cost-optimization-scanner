# Output Audit — bnc / ap-southeast-1 / 2026-08-12

- **Report**: `bnc_ap-southeast-1.html` (account 784852663902), log
  `bnc_ap-southeast-1.log`, baseline preserved as
  `bnc_ap-southeast-1_baseline.html`
- **Headline**: $2,829.03/mo counted, 320 recs + 133 advisories, 16 services
  with findings of 34 scanned
- **Account context**: a genuinely MIXED commitment account — both Savings
  Plans and Reserved Instances are active, which is why it was chosen:
  - **14 EC2-Instance Savings Plans**, $2.9066/hr = **$2,163/mo**, all No
    Upfront, expiring Nov/Dec 2026 (reconciles exactly against CE "Savings
    Plans for AWS Compute usage" $2,162.51 in July).
  - **3 Aurora PostgreSQL Reserved DB Instances**, **$3,299.64/mo** —
    `db.r5.4xlarge` ×2 at $2.162/hr and `db.t3.medium` ×1 at $0.111/hr, both No
    Upfront, started 2025-11-28. These reconcile to the cent against CE
    `APS1-HeavyUsage:db.r5.4xl` $3,217.06 (= 2 × 2.162 × 744) and
    `APS1-HeavyUsage:db.t3.medium` $82.58 (= 0.111 × 744).
  - None on EC2, ElastiCache, Redshift or OpenSearch.

  So the **RI book is larger than the SP book**, and both C6 layers are live.
  The role (`MS-Expert`) DOES have CE access on this run — a change from the
  earlier finding that bnc's MS-Expert role was SCP-denied `ce:*`, and the
  reason the commitment layers work at all here.

  > **Correction (2026-08-12, after operator challenge).** The first pass of
  > this ledger stated "**No RIs of any kind**". That was wrong, and wrong by
  > method rather than by luck: I ran `describe_reserved_instances` on
  > **OpenSearch only**, saw `NONE`, and generalised it to every service — then
  > read the absence of RI recommendations as confirmation, when that absence
  > was itself the symptom of BNC-5 below. The disconfirming evidence was
  > already in my own Layer-2a output: `APS1-HeavyUsage:db.r5.4xl $3,217.06`
  > sat in the RDS usage-type table I had pulled, and `HeavyUsage` **is** the
  > RI recurring-fee usage type. A negative claim about an account needs the
  > enumeration that covers it, and a $3,217/mo line item is not something to
  > skim past. Logged as lesson **F6**.
- **Method**: OUTPUT_AUDIT_PROTOCOL Layers 1–3. Layer 1 harness; Layer 2a rate
  verification against the live AWS Pricing API **and against Cost Explorer
  usage types**, which proved decisive twice; Layer 3 refute before listing —
  which killed 4 of 7 candidates.

## Verified correct (audit passes)

| Check | Result |
|-------|--------|
| Layer 1 harness | **0 FAIL**, 3 WARN, 2 INFO (pre-fix, before S16 existed) |
| S1 headline reconciliation | $2,829.03 = sum of 12 non-zero tab totals, to the cent |
| `audit_upper_bounds.py` | no counted saving rests on an uncorroborated upper bound |
| `scan_doctor.py` | no silent-failure service; 17 zero-finding services all genuinely empty or advisory-only |
| **M360-3 (OpenSearch JVM gate)** | **FIRST LIVE EXERCISE, and it fired.** Both domains withheld: `production-bnc` peak JVMMemoryPressure 83% and `staging-bnc-2` 70%, against the 37.5% a halved heap allows. Without it this report would have counted two heap-bound downsizes |
| **M360-1 (Aurora memory floor)** | fired on `ibis-staging` — "CPU suggests a downsize but the memory floor does not permit one" |
| **C6 commitment demotion** | **15 of 17 CoH EC2 recs correctly demoted**, each naming the covering plan and family. The 2 survivors are legitimate (below) |
| RDS backup reconciliation | anchored to CE **to the cent**: `APS1-RDS:ChargedBackupUsage` $430.03 and `APS1-Aurora:BackupUsage` $133.98 match `actual_billed_backup_pool` exactly; counted $405.80 + $81.49 sits under both pools. APS3 (ap-south-1) rows correctly excluded |
| OpenSearch Graviton rates | EXACT vs live Pricing API (table below) |
| ElastiCache memory gating | correct — `PeakMemoryUsagePct` 1.03%/1.04%, `Evictions` 0, `MemoryHeadroomOk` True on both counted downsizes |
| EIP pricing | 5 stopped-instance + 4 unassociated EIPs at $3.65 = $18.25 + $14.60, exact |
| Route53 | $0.50/zone flat, `region_multiplier` 1.0 — correctly NOT region-scaled (C1) |
| AMI dedup | 0 snapshots counted under more than one AMI (A3 clean); 0 AWS Backup AMIs on this account (AFS-2 inapplicable) |
| Display fidelity | headline appears once; every one of 16 tabs has its panel; no card titled `: Unknown` |

## Live rate verification (Layer 2a)

ap-southeast-1. **Cost Explorer usage types were used as the primary oracle**
where the Pricing API exposes no retail SKU — CE is stronger evidence anyway,
being what AWS actually billed this account.

| Claim | Scanner | Live source | Verdict |
|-------|---------|-------------|---------|
| EKS control plane | $0.10/cluster-hr | CE `APS1-AmazonEKS-Hours:perCluster` $223.20 / 2,232 hr | EXACT |
| EKS extended support | $0.50/cluster-hr | CE `APS1-AmazonEKS-Hours:extendedSupport` $261.00 / 522 hr | EXACT |
| m5.xlarge.search | $0.354/hr | Pricing API $0.3540; CE $1,580.26 / 4,464 hr | EXACT |
| m6g.xlarge.search | $0.319/hr | Pricing API $0.3190 | EXACT |
| → Graviton delta | $25.55/node/mo | (0.354 − 0.319) × 730 | EXACT |
| r5.large.search | $0.224/hr | Pricing API $0.2240 | EXACT |
| r6g.large.search | $0.202/hr | Pricing API $0.2020 | EXACT |
| → Graviton delta | $16.06/node/mo | (0.224 − 0.202) × 730 | EXACT |
| RDS backup storage | $0.095/GB-mo | CE $430.03 / 4,526.66 GB | EXACT |
| Aurora backup storage | $0.023/GB-mo | CE $133.98 / 5,825.41 GB | EXACT |

The AWS Pricing API exposes **no** standard `AmazonEKS-Hours:perCluster` or
`:extendedSupport` retail SKU in ap-southeast-1 (only Outposts and the 4XL
provisioned tier), which is why `PricingEngine` carries
`FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY = 0.50`. CE confirms that fallback is
correct — as a prior audit also found.

## Findings

### BNC-1 — EKS extended support counted on a cluster AWS does not bill (C21 / C18) — CRITICAL

$365.00/mo — **12.9% of the headline** — is counted against
`IBIS_Prod_EKS_Cluster` for an extended-support surcharge AWS is not charging.

Three clusters run Kubernetes 1.33, which entered extended support on
2026-07-29. The adapter counts $365/mo for each. **Cost Explorer bills the
surcharge for exactly two of them:**

| Window | `*-Hours:extendedSupport` | `*-Hours:perCluster` | Ratio |
|--------|---------------------------|----------------------|-------|
| Jul (from the 29th) | 144 hr = 2 × 72 | 2,232 hr = 3 × 744 | 2 vs 3 |
| Aug 1–12 | 522 hr | 783 hr | 522/783 = **exactly 2/3** |

The two windows agree independently, and the August ratio is exact to seven
decimal places (783/792 = 522/528 = 0.98863636 — the same fractional
posting-lag applied to both rows, so the underlying counts are precisely 3
clusters at the base rate and 2 at the surcharge).

The unbilled cluster is precisely the one whose
`upgradePolicy.supportType` is `STANDARD`:

| Cluster | K8s | `upgradePolicy` | Billed surcharge? | Counted |
|---------|-----|-----------------|-------------------|---------|
| `IBIS_Prod_EKS_Cluster` | 1.33 | **STANDARD** | **no** | $365.00 ✗ |
| `irisserver-cluster-prod` | 1.33 | EXTENDED | yes | $365.00 ✓ |
| `mta-prod-cluster` | 1.33 | EXTENDED | yes | $365.00 ✓ |

AWS documents the semantics unambiguously:

> `STANDARD` — Your EKS cluster is eligible for automatic upgrade at the end of
> standard support. **You will not incur extended support charges with this
> setting** […]
> `EXTENDED` — Your EKS cluster will enter into extended support once the
> Kubernetes version reaches end of standard support. **You will incur extended
> support charges with this setting.**

**This is the same lever failing a second time, in the opposite direction.**
`services/adapters/eks.py:250-264` carries a docstring stating
"`cluster.upgradePolicy.supportType` **must NOT be used**", written when the
2026-07-09 audit of this same account found $730/mo counted on policy alone
while 1.33 was still in standard support. That diagnosis was right; the repair
over-corrected. The truth is the **conjunction** — the version must be past
end-of-standard-support AND the cluster must be allowed to enter extended
support. Neither term alone is sufficient, and each admits a phantom in its own
direction. The adapter even READ `policy_extended` (line 323) but used it only
in the `elif` that emits the *pending* advisory — a corroborating signal
present in the function and never consulted by the counted branch.

- Suspect: `services/adapters/eks.py:314-350` (`_check_cluster_cost`).
- **FIXED** on `fix/eks-extended-support-policy-gate`. The counted branch now
  requires both conjuncts and records both in `AuditBasis.evidence`. A STANDARD
  policy emits **no card at all** — the recommendation is affirmatively wrong
  rather than merely unproven, which is the M360-1 precedent ("the Aurora tab
  now renders nothing, which is correct") — and warns instead, so the withheld
  lever is disclosed rather than silent. An unreadable policy also withholds
  and warns rather than assuming AWS's documented `EXTENDED` default, since the
  project resolves ambiguity toward under-counting.
- **The $73.00 idle-control-plane rec on the same cluster is CORRECT and stays.**
  `IBIS_Prod_EKS_Cluster` has 0 node groups, 0 Fargate profiles and 0 owned EC2
  nodes, and bills the plain $0.10/hr. Once the phantom surcharge is gone,
  deleting it saves exactly $73.00 — see the refutation below for why the
  pre-fix pairing was nonetheless not a double count.
- Predicted delta: **−$365.00** → eks_cost **$1,168.00 → $803.00**.

### BNC-2 — unused Savings Plan commitment counted as a saving (C22, NEW) — CRITICAL

$390.32/mo — **13.8% of the headline**, and the entire
`commitment_analysis` tab — is the measured unused commitment on 4 of the
account's 14 Savings Plans, counted as realizable monthly savings.

| Plan | Family | Commitment | Utilization | Counted |
|------|--------|-----------|-------------|---------|
| `9ab4c14e…` | r6a | $0.1810/hr | **0.0%** | $125.98 |
| `bcef3bb3…` | r5 | $0.3830/hr | 44.6% | $147.64 |
| `9802e4d2…` | t3 | $0.2656/hr | 63.6% | $67.26 |
| `655525ed…` | r5 | $0.0960/hr | 26.0% | $49.44 |

The figures are correct — each is CE's `UnusedCommitment` for the window, and
reproduces as commitment × (1 − utilization) × 696 hr to the cent. The waste is
real. **It is also unrecoverable.** A Savings Plan bills its hourly commitment
for the whole 1- or 3-year term whether or not usage consumes it; unused hourly
benefit is explicitly not carried into the next hour; and a plan can be returned
only within 7 days of purchase, in the same calendar month, at ≤$100/hr. These
were bought 2025-11-28 and 2025-12-08. Nothing the operator does this month
reduces the bill by $390.32.

The tell is in the rec itself: `recommended_value` is `"95%+"` — a target
*ratio*, not an action on a resource. There is no delete, resize or migrate, so
there is no bill delta.

**The charitable reading fails on this account's own numbers.** One could argue
the saving is real via "shift on-demand workloads onto the idle commitment".
That saving is bounded by matching uncovered on-demand spend, which the check
never measures — and here there is none. CE's uncovered on-demand, by exact
instance type over the trailing 7 days:

| Type | 30-day run rate |
|------|-----------------|
| r4.large | $336.86 |
| m6i.large | $83.81 |

The flagged plans are **r5, t3 and r6a**. Not one dollar of uncovered on-demand
exists in any of those families. The realizable saving is provably **$0.00**.

- Suspect: `services/adapters/commitment_analysis.py:270-285` (SP) and
  `:427-453` (RI). The adapter's own design comment asserted "Existing-commitment
  waste (under-utilization, expiring) stays counted".
- **FIXED** on `fix/sp-utilization-sunk-cost`. Both levers emit a `$0`
  `Counted=False` advisory carrying the measured waste in `AdvisoryEstimate`
  and naming the lever that does exist (right-size at renewal, or move matching
  on-demand usage onto the commitment) — the same treatment as FSx SSD→HDD, the
  CloudWatch log-class migration, and the AWS Backup AMI retarget.
- **The identical RI lever is fixed with it.** Reserved Instances are equally
  non-cancellable. It did not fire here (no RIs held), so it carries a $0 delta
  on this account — the M360-3 situation: fixed, unexercised, waiting for an
  account that holds RIs.
- Predicted delta: **−$390.32** → commitment_analysis **$390.32 → $0.00**.

### BNC-3 — OpenSearch Graviton lever ignores the dedicated-master tier (NEW, OS-7 class) — HIGH

The Graviton migration for `production-bnc` counts 3 nodes when the domain runs
**6 billable m5.xlarge.search nodes**: 3 data + 3 dedicated master.

```
production-bnc: data m5.xlarge.search × 3, DedicatedMasterEnabled=True,
                master m5.xlarge.search × 3
CE July:        APS1-ESInstance:m5.xlarge  $1,580.26 / 4,464 hr = 6 × 744
```

CE confirms all six bill. `OS-7` already taught the **idle-domain** lever that
master and UltraWarm nodes bill on top of the data nodes
(`services/adapters/opensearch.py:451-473`), but the **Graviton** branch prices
`rec["InstanceCount"]` alone — the data-node count — and the shim never attached
the master fields to that rec in the first place. Half the migration is missing.

This is an **under-count**, the safe direction, which is why no sweep caught it;
it is listed because the protocol's severity table counts missed real dollars as
HIGH, and because the fix is the same one OS-7 already made next door.

- Suspect: `services/opensearch.py:166-176` (shim, rec construction) and
  `services/adapters/opensearch.py:526-540` (adapter, Graviton branch).
- **FIXED** on `fix/opensearch-graviton-master-nodes`. The shim attaches the
  master/warm type and count; the adapter prices **each tier against its own
  Graviton counterpart** and omits any tier that does not price. So an
  already-Graviton master contributes nothing, and UltraWarm — which has no
  Graviton counterpart — is omitted rather than guessed. Per-tier types, counts
  and deltas ride on the `AuditBasis`.
- Predicted delta: **+$76.65** (3 master nodes × $25.55) → opensearch
  **$344.63 → $421.28**.

### BNC-5 — RI utilization and coverage are EC2-only unless filtered per service (NEW) — MEDIUM

The two RI stat readers call Cost Explorer with **no `SERVICE` filter**. Per
the `GetReservationUtilization` API reference:

> "If not specified, the `SERVICE` filter **defaults to Amazon Elastic Compute
> Cloud - Compute**. Supported values for `SERVICE` are [EC2-Compute, RDS,
> ElastiCache, Redshift, Elasticsearch]. The value for the `SERVICE` filter
> should not exceed '1'."

So an unfiltered read is an **EC2-only read wearing an account-wide label**.
On bnc, whose entire RI book is Aurora, that is the difference between a
correct answer and no answer at all — verified live:

| Call | `PurchasedHours` | Utilization | Reserved hrs | On-demand hrs |
|------|------------------|-------------|--------------|---------------|
| unfiltered | **0** | 0% | **0** | 2,862.08 |
| `SERVICE=RDS` | **2,088** | **100%** | **2,149** | 726.23 |

The report therefore rendered **"RI Utilization: n/a"** and **"RI Coverage:
0.0"** against $3,299.64/mo of reservations at 100% utilization. The coverage
figure is the more harmful of the two: `0.0` reads as *"you own no
reservations, buy some"* on an account already 74.7% covered on its largest
service — and it sits on the same tab as a Cost Optimization Hub card
recommending an OpenSearch RI purchase.

A **second, independent** defect surfaced in the same function: the overall
rate was read from `Total.PurchasedHours`, but **`Total` comes back empty
whenever `GroupBy` is set** (confirmed live — filtered+grouped returns
populated `Groups` and `Total: {}`). So even with the filter fixed, the rate
would still have collapsed to `n/a`. Both had to be repaired for either to work.

- Suspect: `services/adapters/commitment_analysis.py` `_check_ri_utilization`
  and `_check_ri_coverage`.
- **FIXED** on `fix/ri-stats-service-scope` (stacked on
  `fix/sp-utilization-sunk-cost`, since it edits the same function). Both
  readers loop the five reservable services, one call each. Utilization
  aggregates `PurchasedHours`/`TotalActualHours` from the **groups**; coverage
  sums `ReservedHours`/`TotalRunningHours` rather than averaging per-service
  percentages, which would weight a service with 3 running hours the same as
  one with 3,000. Zero purchased hours across every service still yields `None`
  (n/a), never a fabricated 0%. Uses the modern `Amazon OpenSearch Service`
  dimension — the reference lists the legacy Elasticsearch name, but only the
  modern one carries data (verified live: 5,019 hours vs 0).
- **The C6 demotion layer is unaffected.** `commitment_coverage.py:690` reads
  `rds:describe_reserved_db_instances` directly, not CE, so the Aurora RIs were
  always visible to the layer that demotes counted dollars — which is why this
  is a MEDIUM stat defect and not a dollar phantom. `_fetch_dynamodb_reserved`
  already passed its SERVICE filter correctly; only the two stat readers were
  unfiltered.
- CE cost rises from ~61 to ~69 calls/scan (~$0.61 → ~$0.69).
- Predicted delta: **$0.00 counted** — RDS RI utilization is 100%, so no
  under-utilization rec fires either way. Stats change: RI Utilization
  `n/a → 100%`, RI Coverage `0.0 → ~0.158` account-wide (2,149 reserved of
  13,624 total running hours across the five services).

## Refuted during Layer 3 (recorded so they are not re-raised)

**1. EKS `extended_support` + `idle_cluster` on the same cluster looked like a
double count.** `IBIS_Prod_EKS_Cluster` carried both a $365 surcharge rec and a
$73 idle rec, and "upgrade the version" and "delete the cluster" are not
independent actions. **Refuted as a double count**: the two are a coherent
decomposition of the cluster's full cost — deleting an extended-support cluster
saves $438/mo, and $365 + $73 = $438 exactly. The pre-fix total for that cluster
was therefore right even though one of its two components was wrong. The real
defect was BNC-1, and after it the $73 stands alone and is exactly correct. Had
the audit stopped at the double-count hypothesis it would have "fixed" the
wrong rec and left the phantom.

**2. m6i.large CoH Graviton rec counted despite an active m6i Savings Plan.**
15 of 17 CoH recs were demoted for commitment coverage; `i-0ad0b935c4cf2e3f3`
(m6i.large → m6g.large, $17.52) was not, although SP `d3168b27…` covers m6i at
$0.0794/hr. **Refuted**: the C6 **CE headroom cap** legitimately permitted it —
uncovered on-demand for exact type m6i.large runs at **$83.81/mo**, well above
the $17.52 counted. The r4.large survivor ($85.85 against $336.86 of headroom)
is likewise correct, and no r4 SP exists at all. The two-layer C6 design is
working exactly as documented.

**3. OpenSearch Graviton's $76.65 was missing from the HTML.** `grep 76.65`
returned 0 hits despite the rec being counted. **Refuted**: by-design grouping
(F3) — the panel renders "Graviton Migration (2 domains) … $92.71/month" with
both domains listed inside. Display fidelity is intact.

**4. Route53 "Unused Hosted Zones" rec had an empty `resource_id`.** Looked like
a D5 nameless-card violation. **Refuted**: the rec carries `HostedZoneId`, which
is in the renderer's or-chain, and the card renders
`Z08690371MG1YXQBLUBH2` correctly. My dump script's key list was incomplete, not
the rec.

**5. RDS snapshot savings are 94.4% of the entire billed backup pool (C11).**
$405.80 counted against a $430.03 billed pool is exactly the shape C11 warns
about. **Refuted, and the check is better than required**: the share is earned —
11,790 of 12,490 GB of backup footprint is flagged — and the whole thing is
anchored to CE's actual billed dollars rather than to a provisioned-size upper
bound (the per-rec `AuditBasis` carries `reconciliation_factor` 0.3624 knocking
$13.30 down to $4.82 on the largest). If anything it **under**-counts: deleting
11,790 GB would drop total backup below the free allowance (100% of provisioned
DB storage), taking the billed pool to ~$0 and saving the full $430.03.

**6. OpenSearch Extended Support $251.92 is under CE's $273/mo run rate.**
Derived from a trailing-7d CE read scaled ×30/7, against July's $278.55 and
August's $97.72-over-11-days. **Not a defect** — the shortfall is CE's posting
lag inside the trailing window, and it errs low. Worth recording that the figure
is genuinely attributable even though the card says "(unattributed — enable Cost
Explorer resource-level granularity)": the surcharge bills 48 normalized units
per hour, and `production-bnc`'s 6 m5.xlarge nodes are exactly 48 (xlarge = 8
units), while `staging-bnc-2` runs OpenSearch 3.3 and is current. Filed as a
LOW attribution improvement below, not a dollar finding.

## Open / not fixed

- **BNC-4 (LOW, not fixed)** — the OpenSearch Extended Support card could name
  `production-bnc` by reconciling billed normalized units against each domain's
  node count and engine version, instead of rendering
  "(unattributed …)". No dollar changes; deferred as an enhancement rather than
  bundled into a correctness fix.
- **`services/rds.py` `RDS_OPTIMIZATION_DESCRIPTIONS`** (carried over from
  M360-2) — still not swept. Confirmed inert again here: RDS's live sources on
  this account are `enhanced_checks` and `cost_optimization_hub` only, and the
  reporter keys descriptions by source name. Left alone deliberately; removing
  one dead key without auditing the whole dict would be arbitrary.
- **me-south-1 / me-central-1 outage** — not applicable to this report. No
  ME-region resource appears in it, and no S3 region was retired during the
  scan.

## Coverage gaps (savings ABSENT from the $2,829.03)

- **S3 `core-document-central-784852663902-ap-southeast-1-an`** —
  `s3:GetBucketLocation` denied (twice; the duplicate suggests the call is made
  twice). That bucket's size, storage class mix and lifecycle findings are
  missing entirely.
- **QuickSight** — `ListNamespaces` is disabled for STANDARD Edition, so no
  QuickSight capacity/SPICE finding is possible on this account. Correctly
  raised as a warning rather than swallowed.
- **Athena workgroup `primary`** — `PublishCloudWatchMetricsEnabled` off, so
  scan spend is unmeasurable (ATH-6 behaving correctly: one warning, no
  fabricated card).
- **Bedrock** — `ListCustomModels` "Unknown Operation" in ap-southeast-1. Given
  CE shows **$6,658/mo of Bedrock model spend in July** across Claude Sonnet
  4.5/4.6 and Haiku 4.5 — the account's second-largest cost after RDS — the
  Bedrock tab reporting $0 is a coverage gap worth naming loudly, not a clean
  bill of health.
- **13 stale Cost Hub volume-delete recs dropped** — all for `in-use` volumes.
  The E-series guard working; no savings lost, but CoH's view of this account is
  ~13 recs stale.

## Status

| ID | Class | Severity | Claim | Status |
|----|-------|----------|-------|--------|
| BNC-1 | C21 / C18 | CRITICAL | $365.00 counted for an EKS extended-support surcharge AWS does not bill (STANDARD upgrade policy); CE proves 2 of 3 clusters | **FIXED** — `fix/eks-extended-support-policy-gate`, predicted −$365.00 |
| BNC-2 | C22 (NEW) | CRITICAL | $390.32 of unused SP commitment counted as savings; sunk and unrecoverable, with $0 of in-family on-demand to absorb it | **FIXED** — `fix/sp-utilization-sunk-cost`, predicted −$390.32 |
| BNC-3 | NEW (OS-7 class) | HIGH | OpenSearch Graviton prices data nodes only, missing 3 dedicated master nodes | **FIXED** — `fix/opensearch-graviton-master-nodes`, predicted +$76.65 |
| BNC-4 | — | LOW | OpenSearch extended-support surcharge rendered unattributed though determinable | OPEN — enhancement, $0 |
| BNC-5 | NEW | MEDIUM | RI utilization/coverage read CE unfiltered = EC2-only; "n/a" and "0.0" against $3,299.64/mo of Aurora RIs at 100% utilization | **FIXED** — `fix/ri-stats-service-scope`, predicted $0.00 counted, stats corrected |
| — | — | — | EKS surcharge + idle rec on one cluster | REFUTED — coherent decomposition, $365 + $73 = full cluster cost |
| — | — | — | m6i CoH rec escaping C6 demotion | REFUTED — CE headroom $83.81 > $17.52 counted |
| — | — | — | OpenSearch $76.65 missing from HTML | REFUTED — F3 grouping |
| — | — | — | Route53 nameless counted card | REFUTED — renders `HostedZoneId` |
| — | — | — | RDS snapshots at 94.4% of the billed pool (C11) | REFUTED — CE-anchored and conservative |

**Predicted headline: $2,829.03 → $2,150.36** (−$365.00 − $390.32 + $76.65).
Two of the three moves remove dollars that were not defensible; the third
restores dollars that were real and unclaimed.

## Harness and lessons added

- **Lesson C21** — *When a fix replaces signal A with signal B, first ask
  whether the truth is A AND B.* Written from BNC-1, where the same lever on the
  same account produced a phantom in each direction a month apart.
- **Lesson C22** — *Measured waste is not automatically a realizable saving.*
  Written from BNC-2. The tell: a `recommended_value` that is a target ratio
  rather than an action on a resource.
- **Lesson C23** — *A CE API that silently defaults a dimension answers a
  narrower question than you asked.* Written from BNC-5: `GetReservationUtilization`
  and `GetReservationCoverage` default `SERVICE` to EC2 and cap it at one value
  per call, so an unfiltered read is an EC2-only read wearing an account-wide
  label. Pairs with the `Total`-is-empty-under-`GroupBy` trap in the same call.
- **Lesson F6** (audit-method) — *A negative claim about an account needs the
  enumeration that covers it.* Written from this ledger's own error: "no RIs of
  any kind" was generalised from a single OpenSearch `describe`, and the
  disconfirming `HeavyUsage` line was already in my Layer-2a output.
- **S16 conjunct-evidence sweep** (`tools/output_audit.py`), keyed off
  `_REQUIRED_EVIDENCE_CONJUNCTS`, the machine-checkable half of C21: a counted
  rec on a conjunction-gated lever must name both halves in
  `AuditBasis.evidence`. **Verified against the real pre-fix report** — it flags
  all 3 EKS recs on `bnc_ap-southeast-1_baseline.html` and is silent after the
  fix. Seeded-violation tests cover both directions of the EKS regression plus
  the passing and advisory cases.
- **S15 needs no new row.** No new downsize lever shipped in this audit: the EKS
  surcharge is a version charge, SP utilization is a commitment, and the
  OpenSearch Graviton lever is a same-size family migration with no binding
  dimension to gate on.

## Reconciliation — re-scan 2026-08-12 (protocol fix-loop step 3)

| | Baseline | Re-scan | Delta |
|---|---|---|---|
| **Headline** | $2,829.03 | **$2,163.19** | −$665.84 |
| eks_cost | 1,168.00 | 803.00 | **−365.00** (BNC-1, exact) |
| commitment_analysis | 390.32 | 0.00 | **−390.32** (BNC-2, exact) |
| opensearch | 344.63 | 434.11 | **+89.48** (BNC-3 +76.65 exact, +12.83 CE drift — below) |
| All 9 other tabs | — | — | **$0.00 each** |

Predicted **$2,150.36**, landed **$2,163.19** — **+$12.83**, and the difference
is fully attributed and is not the fixes.

- **BNC-1 exact.** eks_cost −$365.00 to the cent, and the log carries the new
  disclosure: *"EKS cluster 'IBIS_Prod_EKS_Cluster' runs Kubernetes 1.33
  (extended support), but its upgradePolicy is STANDARD — AWS auto-upgrades it
  and bills no extended-support surcharge."* The two genuinely-billed clusters
  keep their $365 each.
- **BNC-2 exact.** commitment_analysis → $0.00, with all 4 SP under-utilization
  recs `Counted=False` and the full **$390.32 preserved in `AdvisoryEstimate`** —
  the waste is still shown, just not counted.
- **BNC-3 exact.** production-bnc Graviton $76.65 → **$153.30**, and the
  `AuditBasis` now carries `master_type`, `master_target_type`,
  `master_count: 3` and `master_per_node_delta_monthly: 25.55` beside the data
  tier. staging-bnc-2 unchanged at $16.06, as it has no master tier.
- **BNC-5 verified on the stat cards.** **RI Utilization `n/a` → `1.0` (100%)**
  and **RI Coverage `0.0` → `0.1577`** — the latter matching the predicted
  ~0.158 (2,149 reserved of 13,624 running hours across the five reservable
  services). The account's 3 Aurora RIs are finally visible.
- **The +$12.83 is the OpenSearch Extended Support trailing window, not a fix.**
  That lever measures `*-OpenSearchExtendedSupport` over a trailing 7 days and
  scales ×30/7, so it moves with CE as spend posts: **$251.92 → $264.75**.
  Decomposing the tab confirms it — $264.75 + $153.30 + $16.06 = $434.11
  exactly, with the Graviton legs landing on prediction to the cent. (The new
  figure is also the $264.75 this account's surcharge was independently measured
  at in an earlier session.)
- **Layer 1: 0 FAIL**, and **S16 is silent** where it flagged all 3 EKS recs on
  the baseline.
- Counted recs 320 → 315, advisories 133 → 137: the demoted recs render rather
  than vanish.
- **ElastiCache extended support (LS-2) correctly contributes $0 here** — bnc's
  Redis clusters are on current versions and CE bills no ElastiCache surcharge,
  so the new lever stays silent. A lever that fires only where the charge exists.

**Status: RECONCILED.** BNC-1, BNC-2, BNC-3 and BNC-5 all verified on live
output; the single deviation is an independently-quantified CE window movement.
