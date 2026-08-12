# Live-Audit Lessons — recurring cost-fidelity bug classes

Bug classes **confirmed in live deep audits** across multiple accounts/regions
(eu-central-1, eu-west-1, ap-south-1, ap-southeast-1; accounts level-Shoes-prod,
bnc, tadweer-prod — 2026-06-29 → 2026-06-30), extended by the 31-adapter blind
sweep and its six remediation tranches (2026-08-09 → 2026-08-10, see
`docs/audits/SWEEP-FALSE-NEGATIVES-2026-08-09.md`). Each was a real finding that
shipped a fix. Paste this file **alongside** any per-service `*_AUDIT_PROMPT.md`: the
per-service prompt tells you what the adapter does; this file tells you the
mistakes that actually recur and exactly how to catch them.

Every counted dollar must be account-specific and defensible; a non-defensible
lever is a `$0` `Counted=False` advisory (rendered, never summed); `counted ==
rendered` at the per-rec field level; **double-counting is the cardinal sin**.

---

## A. Double-counting & de-duplication (the cardinal sin)

Whenever **two levers or two sources can target the same physical resource**, they
will eventually both count it. This is the single most common real finding.

- **A1 — Dedup at RESOURCE-ID granularity, not a coarser scope.** A NAT
  Cost-Optimization-Hub idle finding was first de-duplicated by *demoting every
  local NAT lever in the whole VPC* — which suppressed the **independent**
  consolidation savings of *other* NATs in that VPC and attached a false
  "superseded by CoH" note. Fix: dedup by the specific NAT id (CoH covers nat-X →
  exclude only nat-X from the local topology math; the other NATs still
  consolidate). *Real: network NAT CoH, ap-southeast-1.*
- **A2 — Prefer EXCLUSION over blanket DEMOTION.** Excluding the CoH-owned
  resource from the heuristic's input (so the heuristic recomputes on what
  remains) is safer than demoting the heuristic's output. Demotion over-suppresses
  independent resources and is prone to advisory-leaks (see B1).
- **A3 — Cross-adapter shared resources.** A single EBS snapshot referenced by
  **two unused AMIs** was counted under both (each AMI attributed the snapshot's
  full storage). A snapshot is billed once and is freed only when *every*
  referencing AMI is deregistered, so the second attribution is unrecoverable.
  Fix: a `counted_snapshot_ids` set; count each snapshot once; the second AMI
  becomes a `$0` advisory ("shared — counted under the other AMI"). *Real: AMI,
  tadweer-prod, $43.47/mo overstated.* Same risk for: a volume in both EBS and a
  snapshot tab; an EIP on a stopped instance counted again under multiple-EIPs
  (NET-03); ALB consolidation levers scoring the same ALB twice (NET-01).
- **A4 — Dedup state-mutation / claim-order bugs.** When you build a "seen" set
  while iterating, (a) claim a resource **only after** all skip checks pass (a
  skipped item must not steal the claim from a genuine candidate that shares it),
  and (b) claim **only once it actually contributes a counted dollar** — claiming
  an *unsizable* snapshot (describe failed, no fallback size) made a later AMI that
  *could* size it look "shared" and zeroed a real saving. Distinguish
  "all-shared" (→ `$0` advisory) from "unsizable / no data" (→ skip, no rec) —
  both can yield `incremental == 0`. *Real: AMI claim-order, caught in adversarial
  re-verification.*
- **A5 — A dedup fix's failure mode is UNDER-count, not over-count.** After
  writing any dedup, adversarially check: did it suppress an *independent*
  resource's saving? did it leave a demoted rec with a stale numeric (B1)? does it
  reconcile to the cent on a live re-scan? Two of three dedup fixes this cycle had
  a defect caught only by an independent skeptic pass.

- **A6 — A dedup guard is only as good as its least-guarded PRODUCER.** When a
  suppression decision keys off a shared set, grep every writer of that set, not
  just the consumer you are editing. Routing CoH storage types into the `rds`
  bucket needed those recs excluded from suppression in `rds_logic` *and* in the
  aurora adapter — but a third path, `RdsModule` publishing its counted ids to
  `ctx.rds_covered_instance_ids`, put the cluster id back into Aurora's
  suppression set through the back door and defeated the guard added two lines
  earlier. *Real: tranche 4, caught in self-review after the first two paths were
  fixed.* **Sweep:** for each shared set (`coh_keys`, `covered`,
  `rds_covered_instance_ids`, `multi_az_ids`, `counted_snapshot_ids`), list every
  assignment site and confirm each applies the same filter.
## B. Advisory hygiene & string ↔ numeric agreement

- **B1 — Advisory-leak: a DEMOTED-resource `Counted=False` rec with a non-zero
  numeric.** A rec demoted from counted to advisory MUST carry its numeric at
  `0.0`; the recoverable figure goes in `PotentialMonthlySavings`. RDS snapshot
  reconciliation and the first NAT demotion both left `Counted=False` recs with a
  non-zero `EstimatedMonthlySavings` — invisible to the headline (which filters on
  `Counted`) but a trap for any consumer that sums the numeric. **Two subtleties:**
  (i) the numeric field name varies — `EstimatedMonthlySavings`,
  `estimatedMonthlySavings` (CoH camelCase), or snake_case `monthly_savings`
  (network_cost, commitment_analysis, sagemaker); a sweep that checks only one key
  silently passes a leak in the others. (ii) a **PROJECTION / what-if advisory is
  a legitimate exception** — an SP/RI purchase or coverage-gap rec in
  `commitment_analysis` carries a non-zero `monthly_savings` ("you'd save $X *if
  you buy*"), which is a projection, not a counted-resource saving the headline
  dropped by accident; exclude those projection sources before asserting. **Detect:**
  the sweep in the appendix (all numeric fields, projection sources excluded).
- **B1-iii — The COMMITMENT-DEMOTION shape is a sanctioned exception** (user-
  ratified 2026-08-09, Jarir-M2 live audit). A rec demoted by
  `split_by_commitment`/`_demote` keeps its indicative numeric
  (`estimatedMonthlySavings`/`EstimatedMonthlySavings`) so the card can render
  "$X — covered by your SP/RI"; it self-identifies via `CommitmentCoverageNote`
  + `AdvisoryEstimate`. Nothing may sum the numeric (S1/S13 enforce). A
  `Counted=False` rec with a non-zero numeric and NO demotion marker remains a
  genuine B1 leak. The `network/auto_scaling_groups` source is born-advisory
  with an indicative numeric by design (like `eks_cost/node_group_optimization`)
  — exempt at source level, not rec level.
- **B2 — The `EstimatedSavings` STRING and `EstimatedMonthlySavings` NUMERIC must
  agree to the cent in EVERY branch.** A reconciliation capped the string
  (`$4.84/month`) but left the numeric uncapped (`13.30`) — a +$719.60 field
  overstatement that the headline (string-based) hid. Athena set the numeric but
  left a generic "Up to 75%" string. EIP carried a string but no numeric. When you
  cap, reconcile, or fall back, update **string, numeric, AND AuditBasis** in
  lockstep. *Real: RDS, athena, EIP.*
- **B3 — A counted rec needs both representations populated.** A counted lever
  with only a string (no numeric) or only a numeric (empty/placeholder string)
  breaks `counted == rendered`. Note the legitimate exceptions (C5) before flagging.

## C. Pricing-rate traps

- **C1 — A globally-FLAT rate must NOT be region-scaled.** Public IPv4 / EIP is a
  flat $3.65/mo ($0.005/hr) in every commercial region; Route53 hosted zones are
  $0.50/zone flat. The `pricing_engine=None` fallback multiplied EIP by
  `pricing_multiplier`, fabricating a region-specific rate for a flat charge. Know
  which dimensions are flat-global vs region-varying (NAT, VPC-endpoint, instance
  hours ARE region-varying — keep the multiplier there). *Real: EIP, both
  pricing_engine and elastic_ip.* This is the mirror of the catalogue's
  "region-scaling missing / double-applied" item.
- **C2 — Size on ACTUAL stored bytes, not provisioned.** Snapshots bill on stored
  blocks: prefer `FullSnapshotSizeInBytes` over `VolumeSize` (~2× overstatement).
  When the actual size can't be read, fall back to the provisioned upper bound
  *and flag the estimate*; if even that is missing, **skip — never fabricate a
  size**. *Real: AMI + EBS snapshot sizing.*
- **C3 — Cap an upper-bound estimate against an actual when available.** RDS
  snapshot upper bounds are capped at Cost-Explorer-actual backup spend per engine
  pool; a CoH/CO dollar supersedes a heuristic for the same resource. A capped
  saving is defensible and stays counted; an un-cappable upper bound is a `$0`
  advisory.
- **C4 — Flat-%-of-spend with no per-resource signal = fabricated.** A Savings-
  Plans coverage gap for service `"Unknown"` (Cost Explorer aggregates *all*
  on-demand spend under "Unknown" when no SPs are active) produced a flat-30%
  estimate of **$2,873.68 — larger than the entire counted headline**. Correctly
  `Counted=False`, but non-actionable noise: suppress it (the concrete buy
  scenarios come from `purchase_recommendations`). *Real: commitment_analysis,
  tadweer-prod.* Same class: old NAT/data-transfer 0.30/0.50/0.40 factors, the
  athena $50 and bedrock $5 placeholders.
- **C5 — De-minimis / round-to-zero.** A 0.1 GB snapshot's recoverable rounds to
  `$0.00` yet a `size_gb > 0` guard still emits a "$0.00/mo recoverable" card.
  Gate on the **rounded potential** (`if potential <= 0: continue`), not just raw
  size. *Real: EBS snapshot, tadweer-prod.*
- **C6 — Rightsizing under an existing Savings Plan / RI is on-demand-basis, not
  realizable.** CoH/Compute-Optimizer `estimatedMonthlySavings` is computed
  "before discounts" (`estimatedMonthlyCost` == on-demand monthly). When the
  account already holds a commitment covering the resource, that figure is a
  phantom: an **EC2-Instance SP is family-locked**, so a Graviton migration
  (m4→r6g) moves the instance OUT of coverage — the new instance bills full
  on-demand while the family-locked commitment **strands to its end date**
  (net effect zero or **cost-NEGATIVE**); a same-family downsize only saves if
  the freed commitment is reabsorbed. The scanner prefetches
  `ctx.commitment_coverage` (`services/commitment_coverage.py`) covering **every
  commitment matrix**: EC2-Instance / Compute / SageMaker SPs
  (`savingsplans:DescribeSavingsPlans`), classic EC2 RIs
  (`ec2:describe-reserved-instances`, regional-family vs zonal-exact), RDS /
  ElastiCache RIs (family, size-flexible), Redshift / OpenSearch RIs (**exact
  type** — not size-flexible), DynamoDB reserved capacity (CE). It **demotes
  commitment-covered rightsizing recs to advisory** (`Counted=False`) in
  ec2/**aurora**/rds/elasticache/redshift/opensearch/lambda/**sagemaker/dynamodb/
  containers(Fargate; ECR storage stays counted)**. Two aggregate-safe layers:
  (1) membership demotion (never overstates); (2) a **CE headroom cap** — the
  *uncovered on-demand $* per `(service, exact instance type)`, with candidates
  counted greedily up to that ceiling so realizable on-demand overflow survives
  while total counted never exceeds real uncovered on-demand; CE-read failure
  falls back to demote-all (safe). Four traps, each cost real money on Jarir-M2:
  * **The gate must cover locally-derived recs, not just CoH.** Adapters that only
    called `demote_coh_by_commitment(coh_recs, …)` let their `enhanced_checks`
    levers through ungated — elasticache **$565.02** + opensearch **$689.12** of
    pure phantom on nodes at 100% RI coverage. Use `demote_covered_in_place`.
  * **Key the ceiling by EXACT instance type, never family.** Overflow concentrates
    in one size; a family ceiling lets a rec on a fully-covered sibling size spend
    it. (Only `db.r7i.4xlarge` carried on-demand; the other 8 r7i were covered.)
  * **Read on-demand over a trailing 7 days, not 30.** A 30d window spanning a
    mid-window RI purchase reports on-demand the now-active RI already absorbs.
    (OpenSearch: $288/mo over 30d, **$0** over the last 7d.)
  * **Source it from `GetCostAndUsage`** (`PURCHASE_TYPE="On Demand Instances"`,
    `GroupBy=INSTANCE_TYPE`, skip the `NoInstanceType` group).
    `GetReservationCoverage` **cannot** serve: it rejects an `INSTANCE_TYPE_FAMILY`
    groupBy, rejects `Granularity` alongside `GroupBy`, and its
    `Coverage.CoverageCost.OnDemandCost` is **`null`** for RDS/ElastiCache/OpenSearch.
    `GetSavingsPlansCoverage` needs `INSTANCE_TYPE_FAMILY` (not `INSTANCE_FAMILY`)
    plus SERVICE+REGION filters, else it sweeps in other regions and a
    `NoInstanceTypeFamily` (Lambda/Fargate) bucket.

  RDS/Aurora RIs are **engine-scoped** (an `aurora-mysql` reservation never covers
  a `mysql` instance) and Aurora draws on the *same* Reserved DB Instance pool as
  RDS — gate both. **Do not infer coverage from normalized-unit arithmetic:** a
  "22 x db.r7i.large = 88 NU == 88 NU fleet, so all covered" inference was flatly
  contradicted by CE's per-instance-type on-demand spend. Actual on-demand $ wins.
  *Real: alyasra, eu-central-1 — 8 EC2-Instance SPs {m4,m5,r5}, 92% util, 90%
  coverage collapsed a reported **$1,057→$13.87/mo** counted (membership layer);
  the flagship m4.2xlarge→r6g.large "$324.70 saving" is actually ~**−$26/mo**
  during the SP term.* **Sweep:** any account with active SPs/RIs — assert (a) no
  counted rightsizing rec targets a family/type that leaves its SP/RI coverage,
  and (b) per service, counted rightsizing savings ≤ CE uncovered-on-demand for
  that family. **Note:** Compute SP covers EC2/Lambda/Fargate but NOT
  RDS/ElastiCache/…/SageMaker; SageMaker SP covers only SageMaker.

- **C7 — A recurring surcharge is only real if AWS is billing it. Verify against a
  Cost-Explorer usage type, never against a config field.** `eks.py` counted a
  `$365/mo` Extended-Support surcharge per cluster whenever
  `cluster.upgradePolicy.supportType == "EXTENDED"` — with the comment
  *"evidence-based … not guessing from the version number"*. But that field is a
  **policy** ("when standard support ends, enter extended support rather than
  auto-upgrade"), **not a billing state**. *Real: bnc, ap-southeast-1 — two
  clusters on Kubernetes 1.33 produced **$730/mo phantom** (31% of the headline)
  while CE showed exactly one usage type, `APS1-AmazonEKS-Hours:perCluster` at
  `$0.098/cluster-hour` — the standard `$0.10` rate, no surcharge line at all.
  `eks:DescribeClusterVersions` confirmed 1.33 = `STANDARD_SUPPORT` until
  2026-07-29.* Authoritative signal: `DescribeClusterVersions[v].versionStatus ==
  "EXTENDED_SUPPORT"` (fail closed — an unreadable lookup counts nothing). A
  cluster with `supportType=EXTENDED` on a still-standard version is a **$0
  advisory naming the date**, not a counted saving. **Converse, same account:** the
  scanner had *no* OpenSearch extended-support check while `APS1-OpenSearchExtendedSupport`
  billed **$264.75/mo** — it invented a surcharge that did not exist and missed one
  that did. Measure surcharges from the billed usage type (trailing 7d x 30/7).
  **Sweep:** for every counted rec whose saving is "remove a surcharge", grep CE
  usage types for a matching line; absent it, the rec is phantom. Note EKS bills
  under CE service `"Amazon Elastic Container Service for Kubernetes"`, not
  `"Amazon Elastic Kubernetes Service"`.

- **C8 — When the evidence read fails, fail CLOSED. An early `return` that skips a
  cap is a silent overstatement.** `reconcile_snapshot_savings` opened with
  `if not backup_actuals: return snaps`, documented as *"a CE gap never silently
  zeroes real savings"* — optimising for the wrong direction. Snapshot savings are a
  **provisioned-size upper bound** (`AllocatedStorage x rate`); actual backup bytes
  sit well below it, so the bound is only counted when Cost Explorer corroborates
  it. The function's own per-group branch already demoted an uncorroborated bound
  (F5) — the early return jumped over it. *Real: bnc, ap-southeast-1 — `ce:GetCostAndUsage`
  is denied by an org SCP for some roles; under such a role the RDS snapshot tab
  counts **$1,131.45** (11,910 GB x $0.095) where billed backup supports only
  **$411.87** — a **$719.58/mo silent overstatement** that appears only when a
  permission is missing.* **Sweep:** for every `except`/empty-result path feeding a
  counted number, ask *"does this skip a ceiling?"* Compare the same account scanned
  with and without `ce:GetCostAndUsage`; the counted total must never rise when
  evidence is removed. **Corollary — a fail-closed ceiling is only safe when its
  query is RIGHT.** A wrong billing query returns `$0`, which is indistinguishable
  from "nothing billed" and demotes real savings. *Real: EBS snapshot storage bills
  under CE service `"EC2 - Other"`, not `"Amazon Elastic Compute Cloud - Compute"`;
  filtering the latter zeroed **$161.60/mo** of genuine AMI savings on bnc while CE
  itself answered fine (no warning).* Scope such reads by **usage type**, not
  service, and warn when a `$0` pool contradicts priced recommendations. Same class as the EBS delete guard that failed open on
  `InvalidVolume.NotFound` (C-series) and the EKS surcharge counted from a config
  field (**C7**).

- **C9 — A flat percentage is a fabricated dollar, and a pricing *fallback* turns a
  "real price delta" back into one.** `dms.py` counted `35% of instance monthly
  (one-size-down)` — `_DMS_SAVINGS_FACTORS = {"Instance Optimization": 0.35}` — in an
  adapter whose docstring claimed *"No flat fallbacks."* *Real: bnc, ap-southeast-1 —
  **$74.09/mo** (the whole DMS tab) credited against `replication-instance-staging`, a
  `dms.r5.large`, which is the **smallest size in the r5 family**: there was no
  one-size-down target for the 35% to represent.* Same class as ElastiCache **H3**
  (flat 0.30) and OpenSearch **H4** (flat 0.25). Replace with the concrete
  `current -> one-size-down` price delta; when no priceable target exists, emit a **$0
  advisory**. **The trap when you fix it:** `PricingEngine.get_*_monthly_price` returns
  a documented *fallback constant* for an unknown SKU, so pricing the non-existent
  `dms.r5.medium` yields a number and `current - fallback` is fabrication wearing a
  real-price costume. Probe hypothetical classes with `allow_fallback=False` (added to
  `get_dms_instance_monthly_price`), and give strict lookups their **own cache
  namespace** — a cached fallback must never satisfy a strict read. **Sweep:** grep for
  `factor`/`* 0.` multipliers against a price; each is a fabricated dollar until it is
  two live prices. Then check every "real delta" fix actually probes both legs strictly.

- **C10 — Idle is not the same as resizable. A recommendation you would have to
  revert is not a saving.** The ElastiCache downsize lever was gated on
  `CPUUtilization` alone (14-day avg < 20%). But AWS's node ladder leaves the next
  size down with only **~36-48% of the current maxmemory** (`cache.t4g.micro` is 36%
  of `cache.t4g.small`; `cache.r5.large` is 50% of `cache.r5.xlarge`), so a node can
  be CPU-idle and still not fit. Executing such a rec evicts the working set and gets
  rolled back — the "saving" was never realizable. Now gated on peak
  `DatabaseMemoryUsagePercentage` <= 35% **and** zero `Evictions`; an unreadable metric
  withholds the dollar (absence of evidence is not evidence of headroom — **C8**), and
  the delta survives as `PotentialMonthlySavings`. *Real: bnc, ap-southeast-1 — the
  four counted nodes did fit (peaks 34.6% / 1.0%), so the $103.66 was right by luck,
  not by check; `ibis-prod` sits 0.4pp under the bound.* **Sweep:** every rightsizing
  lever must gate on the dimension that BINDS the resource, not just the one that
  looks idle — memory for caches/DBs, IOPS/throughput for volumes, connections for
  proxies. CPU is rarely the binding constraint on the thing you are shrinking.
  Cousin of the EBS guard that refuses to delete an in-use volume.

- **C11 — A billed pool covers EVERY resource of its kind, not just the ones you
  flagged. Cap at the subset's SHARE of the pool, never at the pool.** The C8
  reconciler capped an upper bound at the whole billed pool, which silently asserts
  the flagged resources *are* the pool. *Real: afs-prod, eu-west-1 — 317 unused AMIs
  are backed by 744 of the region's 3,003 snapshots (**23.7%** of the 576,495 GiB
  estate), yet the un-shared cap credited **100% of the $5,124.78/mo**
  `EBS:SnapshotUsage` bill. **$3,911.50/mo** of that survives deleting every flagged
  AMI, because 2,259 other snapshots keep billing.* The tell is a counted figure that
  matches the billed pool almost exactly — that is not corroboration, it is the cap
  binding at 100%. Ceiling = `billed x (flagged_footprint / total_footprint)`, with
  numerator and denominator measured on the same basis; an unmeasurable share demotes
  (**C8**), because an unknown fraction of a pool is not a saving. **Sweep:** every
  reconciled tab — if `counted ≈ ActualBilledPool`, the share is missing.
  `services/rds_logic.reconcile_snapshot_savings` has the same shape (bnc: capped to
  $411.80 = 100% of billed backup storage, ignoring automated backups).

- **C12 — A strict (no-fallback) price mode on a CACHED lookup needs its own cache
  namespace.** When a method gains `allow_fallback=False` so a caller can probe
  whether a *hypothetical* class exists, sharing one cache key lets any earlier
  fallback-permitting caller poison it: the strict call returns the cached
  fabricated rate and the guard silently does nothing. Two namespaces (a real-SKU
  hit writes BOTH; a strict miss caches `0.0` so the probe is not repeated per
  resource). *Real: `get_rds_instance_monthly_price` gained the flag for the Aurora
  Graviton probe — and the RDS adapter, which prices with the default, primes the
  shared key first, so the probe would have computed exactly the fabricated delta
  the flag was added to prevent. Caught in self-review; `get_dms_instance_monthly_price`
  already had the right shape.* **Sweep:** every `allow_fallback` parameter — does
  its miss path write a distinct key?
- **C13 — Know a metric's REPORTING CRITERIA before writing an idle gate; the two
  polarities are opposite.** (a) Metrics emitted *only during activity*
  (`AWS/Transfer` `BytesIn`/`BytesOut`: "emitted every 5 minutes **while a
  connection is established**... if no files or bytes are transferred in the
  period, `0` is emitted") — an EMPTY series proves idleness, a series of ZEROS
  means the resource is in use. (b) Metrics emitted *continuously once the
  resource exists* (`AWS/Kafka` `ConnectionCount`, published from the moment a
  cluster is ACTIVE) — an empty series means the read found nothing and must
  ABSTAIN; only present-and-zero proves idleness. A `sum == 0` gate conflates both
  and recommends deleting resources people are actively using. Also check the
  DIMENSIONS: a read whose dimension set does not exactly match what AWS publishes
  returns nothing and reads as idle (MSK needs `Cluster Name` **and** `Broker ID`;
  API Gateway stage traffic needs `(ApiName, Stage)`; SageMaker needs
  `(EndpointName, VariantName)`). *Real: tranche 5 shipped both polarities in one
  run (TR-1 on Transfer, MSK-1 on Kafka); SM-1 was the dimension-set form.*

- **C14 — An AWS coverage/eligibility matrix is a DATED fact. Verify it against a
  live enum or offering query before gating on it, and never widen a gate from
  memory.** Which services a commitment product covers, which regions have a SKU,
  which instance families a reservation is flexible across — all of these change
  under you, and a stale matrix used as an ALLOWLIST fails in the silent
  direction: the check simply stops firing, and an allowlist key that never
  matches is indistinguishable from "nothing found" forever. Cheap live probes
  beat recall: send a deliberately invalid enum value and read the valid set back
  out of the `ValidationException` (`ce:GetSavingsPlansPurchaseRecommendation`
  with a bogus `SavingsPlansType` returns `[DATABASE_SP, SAGEMAKER_SP,
  COMPUTE_SP, EC2_INSTANCE_SP]`), or count live offering rates per `serviceCode`.
  Before adding a filter at all, check whether **AWS already filters for you** —
  `ce:GetSavingsPlansCoverage` returns only SP-eligible services (23 SERVICE
  values in the account, 3 returned), so an allowlist there is pure recall loss
  with no precision gain. *Real: LS-1 (2026-08-10) was raised as a HIGH on the
  premise "Savings Plans cover EC2/Fargate/Lambda/SageMaker only" and was
  WITHDRAWN — Database Savings Plans went GA 2025-12. The proposed allowlist
  would have silently disabled the check for RDS and Aurora, the largest
  uncovered spend in most accounts. The finding, the fix, and one investigating
  agent all carried the same stale matrix; only the live enum caught it.*

- **C15 — Before promoting a measured number onto a card, check what the API says
  it MEASURES, and whether the same tab already shows a different number for the
  same-sounding thing.** `Coverage.OnDemandCost` from `ce:GetSavingsPlansCoverage`
  is documented as usage priced at the **public On-Demand rate** — a rate
  equivalent, not billed spend — and that query takes no region filter. Live it
  read $13.37 for EC2 while actual unblended on-demand was ~$0.0000009 (free
  tier). The commitment tab *already* carries an "Uncovered On-Demand ($/mo)"
  stat built from `UnblendedCost` + REGION + PURCHASE_TYPE, so rendering the
  first beside the second puts two contradictory dollars for the same label on
  one screen. Replacing a hidden fabrication with a visible contradiction is not
  a fix. *Real: the rejected half of the LS-2 repair.*

- **C16 — Being in the API's enum is not the same as being for sale. Check the
  OFFERING, not the parameter.** A request enum tells you what the API will
  *accept*, not what AWS will *sell* you, and the two drift apart the moment a
  product launches with a restricted catalogue. Worse, the API may not tell you:
  `ce:GetSavingsPlansPurchaseRecommendation` accepts `DATABASE_SP` with
  `THREE_YEARS`/`ALL_UPFRONT` and answers **empty** rather than raising — so a
  blind fan-out looks like it works, quietly burns a $0.01 call per impossible
  cell, and stands ready to render a purchase card for a plan nobody can buy the
  day CE starts answering. `savingsplans:DescribeSavingsPlansOfferings` is free
  and authoritative: per plan type it returns the real `(durationSeconds,
  paymentOption)` set — 6 combos for Compute/EC2Instance/SageMaker, exactly 1
  (1yr, No Upfront) for Database. Derive the matrix from that probe rather than
  hardcoding today's answer (C14), and fail **open** — an unresolvable probe
  should cost calls, never drop the recommendation. *Real: LS-7 (2026-08-10).*

- **C17 — After fixing a check, verify the GATE that decides whether it runs.
  A correct fix behind a wrong gate is inert, and every test still passes.**
  Prefetches and reads are usually gated on the set of selected services, and
  that gate was written for whatever the feature originally covered. Extend the
  check to a new service family and the gate silently excludes it: the new code
  is unreachable on exactly the accounts it targets, the flag keeps its safe
  default, and unit tests that call the inner function directly never notice.
  Always add a test that drives the PUBLIC entry point with a `selected` set
  containing only the newly-covered services, and assert the underlying read
  actually happened. *Real: LS-8 (2026-08-10) — `want_sp` was
  `selected & {"ec2","lambda","containers","sagemaker"}`, but a Database SP
  matters to rds/aurora/elasticache/opensearch/dynamodb, none of them in that
  set. Caught only because a mock-driven infinite loop crashed an unrelated test;
  the same widening then exposed an unterminated pager that had been latent
  behind the narrow gate — widening a gate makes latent bugs on that path real.*

- **C18 — A corroborating signal that only ever SUPPRESSES is a fail-open.
  Absent evidence must resolve toward NOT counting, not toward counting.**
  The pattern reads as safe — "network/memory is only used to suppress a false
  positive, never to invent one; when the data is unavailable the prior verdict
  stands" — and it is exactly backwards for a COUNTED dollar. On the accounts
  where the signal is missing, which is most of them, the check silently reverts
  to the un-corroborated behaviour it was written to fix, and the docstring
  reassures the next reader that this is deliberate. Ask of every corroborator:
  *what happens on the account that does not emit it?* If the answer is "we
  count anyway", the fix never shipped. Demote rather than suppress, so the card
  still renders with its figure and the operator can go get the evidence.
  *Real: AFS-1 (2026-08-11, afs-prod/af-south-1) — `memory_bound = mem_pct is not
  None and mem_pct > 80`, where `mem_pct` needs the CloudWatch agent. All three
  counted EC2 rightsizing recs, $2,682.34 = 39% of the report headline, were
  memory-optimized r-family instances told to HALVE their RAM on CPU alone. Rates
  and arithmetic verified EXACT — the defect was purely evidential. Low CPU on an
  r6i is the EXPECTED signature of a memory-bound workload; 256 GiB is why that
  instance was chosen. The prior audit of the same account had verified the same
  family's arithmetic and not questioned the evidence either.*

- **C19 — A gate that can never be FALSE for a resource class is not a check,
  it is a generator. And a managed resource's lifecycle belongs to its
  manager.** Before trusting an idleness/usage test, find the resource class for
  which it is true by construction. "Unused AMI = not referenced by a running
  instance" is true of *every* backup image, because a backup exists precisely so
  that nothing references it until it is needed — so the check fires on 100% of
  them, on every account, forever, and its output looks like a huge finding. The
  second half matters as much: when a resource is created and owned by a managed
  service (AWS Backup, ASG, EKS, an operator), acting on the artifact directly
  circumvents the manager and is usually destructive. The cost lever lives on the
  MANAGER — the plan's retention, the group's desired capacity — which is a
  different action against a different resource, and often one this scan cannot
  even read. Retarget the recommendation instead of deleting it: `$0` advisory,
  measured figure retained, pointed at the thing the operator can actually
  change. *Real: AFS-2 (2026-08-11) — 51 of 56 AMI recs, $281.26 of $302.24 and
  31,834 of 34,209 snapshot GB, were `AwsBackup_*` recovery points. Detect by the
  `aws:backup:` TAG NAMESPACE, not one exact key and not the name, so a new tag
  cannot silently re-open it.*

- **C20 — Demoting a rec must also withdraw its contribution to any SHARE or
  CEILING computed across recs — otherwise the fix inflates its neighbours.**
  Reconciliation caps counted savings at the flagged resources' share of a billed
  pool, and that share is summed over the rec list. Demote some recs without
  removing their size from the numerator and the ceiling holds while the upper
  bound collapses, so the cap stops binding and the SURVIVING recs rise to their
  full un-capped bound. The repair reads as pure removal and quietly hands the
  remainder a raise. Whenever a change demotes recs, re-derive every aggregate
  computed over that list and predict the survivors' new total explicitly — a
  demoted rec frees nothing, so its size must not buy headroom for the ones that
  count. *Real: AFS-2 (2026-08-11) — `flagged_gib` summed every rec's
  `SnapshotSizeGB`. Demoting the 51 backup AMIs collapsed the bound
  $2,035.46 → $141.32 while the ceiling stayed at $302.36, so the factor went to
  1.0 and the five survivors would have jumped $20.99 → $141.32: a $120.33
  phantom created BY the fix. Latent until then only because the pre-existing
  shared-snapshot advisories carry `SnapshotSizeGB` 0.0.*

- **C24 — A billed surcharge is a first-class cost lever, and the ONLY way to
  know whether AWS is charging one is to read the usage type.** Surcharges
  (extended support, licensing, dedicated-IP SSL) are removable, recurring, and
  attributable — everything a counted saving should be — but they are invisible
  to resource describes, so the two failure modes are symmetrical and both are
  live: **inferring** one from a version number invents charges AWS is not
  billing, while **not looking** misses charges it is. Whenever a service
  publishes an end-of-support policy, ask BOTH "does an adapter read the usage
  type?" and "does any adapter infer it instead?". `grep -rn extended_support
  services/adapters/` is the whole audit. *Real: level-Shoes-prod/eu-west-1
  (2026-08-12) did both in the same report, ~$1,090 apart and in opposite
  directions — EKS counted a $365/mo surcharge that has no eu-west-1
  `:extendedSupport` line at all, while $725.62/mo of billed
  `EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.*` (34% of the headline) went
  unreported because no ElastiCache check existed. Bonus: the ElastiCache usage
  type embeds the NODE TYPE, so unlike OpenSearch's it is attributable without
  CE resource-level granularity — read the usage-type STRING before concluding
  a charge cannot be attributed.*

## D. Render / tab / count semantics (`counted == rendered`, both directions)

- **D1 — Counted-but-invisible (render desync).** Savings summed into the headline
  but no visible card. Filter at the SOURCE, not at render. (EC2 CoH renders 22
  recs as 2 *grouped action cards* — that is fine, all instances list inside; the
  past network render-desync was a real bug.)
- **D2 — Advisory-but-no-tab.** A service whose cards are ALL `$0` advisories
  (S3's 138 bucket cards; commitment_analysis RI/SP; redshift/athena/network_cost)
  must still render a tab. The tab gate must key off **rendered** cards (counted +
  advisory), while the headline COUNT keys off **counted-only**. A regression made
  the gate use the counted-only count and hid every advisory-only service's tab.
  *Real: html_report_generator tab gate.*
- **D3 — `total_services_scanned` must match the rendered service tabs.** Use a
  rendered-aware count (counted + advisory, excluding `OPTIMIZED`), not the
  adapter-supplied `total_recommendations`. **Synthetic Snapshots/AMIs tabs are
  intentionally EXTRA** (cross-cuts, not scanned services) — do not flag the
  "9 services / 10 panels" gap they create.
- **D4 — `total_recommendations` counts COUNTED only.** `$0` advisories render as
  cards but never inflate the count or the dollar total; a `count` placeholder
  with no materialised recs is trusted.
- **D5 — A NEW counted lever needs a render check, not just an adapter check.**
  "Counted" and "actionable" are different properties, and in four consecutive
  remediation tranches the surviving defect was render-side, never pricing-side.
  The Phase-A detail extractors in `reporter_phase_a.py` are keyed to the rec
  shapes that existed when they were written, so any rec with a NEW identity key
  needs its extractor updated; everything outside `PHASE_A_DESCRIPTORS` falls
  through `_render_generic_other_rec`, whose or-chain must contain that key.
  Assert on rendered HTML: the resource is NAMED (no "Unknown"), the group total
  equals the counted sum, and an advisory's figure never reads as a counted
  dollar. *Real: LAM-2 (counted Provisioned-Concurrency dollar unrenderable);
  renderers ignoring `Counted`; CF-4 cards rendering "Unknown" because the
  extractor only knew `DistributionId` while the rec is keyed by certificate;
  AG-3's two stages of one API rendering as identical rows.*
- **D6 — A demoted rec's masked figure must reach the card.** Demotion zeroes the
  numerics and parks the gross in `AdvisoryEstimate` / `PotentialMonthlySavings`
  — but if the card prints only "$0.00/month — advisory", the reader has no
  indication anything was withheld. Name the figure inside the advisory framing
  ("$0.00/month — advisory ($418.20/month if realizable)"), in the grouped
  renderers as well as the per-rec ones. *Real: AUR-G hid $418.20/mo behind a
  $0.00 Aurora card; the first fix reached only the per-rec path, leaving the
  grouped network/monitoring cards — where the tranche-6 demotions park their
  exposure — still bare.*

## E. Silent failures & Cost-Hub plumbing

- **E1 — Classify enumeration/metric failures; keep normal fallbacks silent.** A
  bare `except: pass` / `return []` on a `list_*` / `get_metric_statistics` turns
  an `AccessDenied`/throttle into a false "no resources" — hiding both the savings
  and the permission gap. Route genuine failures through `record_aws_error`
  (AccessDenied/Unauthorized/OptInRequired → `permission_issue`, else `warn`). Do
  NOT record the *normal* fallback paths: paginator-unavailable, the
  `NoSuchWebsiteConfiguration` "not a website" answer, an empty-datapoints metric.
  *Real: bedrock (PT/KB/agent enum), sagemaker (endpoint/notebook/training enum),
  step_functions (per-machine CW), s3 (`GetBucketWebsite`, BOTH call sites — the
  second was missed on the first pass).* Thread `ctx` into helpers that lacked it.
- **E2 — CoH "dropped type" is a 3-layer wire-up.** A `currentResourceType` lands
  nowhere unless it is in `_HUB_SERVICES` **and** in the orchestrator `type_map`
  **and** has a consuming adapter that reads `ctx.cost_hub_splits[<bucket>]`. A
  self-reported "N recommendation type(s) had no service bucket and were dropped"
  warning is the tell — AWS-computed savings are being discarded. The bucket name
  MUST equal the consuming module's `key` (the EKS `eks` vs `eks_cost` bug). *Real:
  NatGateway → network bucket recovered exactly the dropped $43.07.* Known
  still-orphaned buckets: none for NAT now; verify elasticache/opensearch/redshift
  are consumed before flagging.

## F. Audit-METHOD traps (avoid false findings)

These caused *false positives* in our own sweeps — check them before reporting.

- **F1 — CoH recs carry savings in camelCase, not PascalCase.** A CoH rec's
  `EstimatedSavings` string is empty and `EstimatedMonthlySavings` (PascalCase) is
  absent; the real dollar is `estimatedMonthlySavings` (camelCase, AWS shape). EC2
  and EBS CoH recs both use this. A "$0 EstimatedSavings string" sweep flags all of
  them falsely — always also read camelCase `estimatedMonthlySavings`.
- **F2 — Unattached EBS volumes carry savings in `EstimatedMonthlyCost`.** Not
  `EstimatedSavings`/`EstimatedMonthlySavings`. The adapter sums it (full cost is
  recovered on delete) and the renderer shows it. Read all savings-bearing fields:
  `EstimatedMonthlySavings`, `estimatedMonthlySavings` (CoH), `EstimatedMonthlyCost`
  (unattached), `PotentialMonthlySavings` (advisory only — must NOT count),
  parsed `EstimatedSavings`.
- **F3 — Grouped rendering is not a render desync.** EC2/EBS CoH recs render as a
  few action-grouped cards with every resource listed inside `<li>` items; a low
  "rec-item card count" vs a high rec count is by design.
- **F5 — Compute Optimizer recs nest their dollars in the rank-1 option.** A CO
  rec (EC2 `recommendationOptions`, EBS `volumeRecommendationOptions`) carries
  its saving at `options[0].savingsOpportunity.estimatedMonthlySavings.value` —
  no flat savings field at all. A flat-field sweep false-flags every CO rec as
  "counted-but-$0" AND a per-tab reconciliation shows a phantom delta equal to
  the CO dollars (afs-prod: $66 = 3 x $22 EBS IOPS rightsizing, correctly summed
  by the adapter and rendered by reporter_phase_b). Read the nested shape before
  flagging. Same family as F1/F2. Also projection-style advisories exist outside
  commitment_analysis: `eks_cost/node_group_optimization` Spot/Graviton what-ifs
  are born-advisory with a non-zero `monthly_savings` (the counted dollar lives
  in the EC2 tab) — exempt them like B1-ii before asserting a leak.
- **F4 — AWS-supplied annotations are not our bug.** A CoH `estimatedSavingsPercentage`
  that disagrees ~1pp with `savings/cost` is AWS's rounding; we display the actual
  `$`. Synthetic Snapshots/AMIs tabs (D3) and the RDS-snapshots-stay-counted (cap
  makes them defensible) vs EBS-snapshots-advisory distinction are by design.

---

## Ready-to-run invariant sweeps

Run these against the scan JSON (and regenerate the HTML to check render). They
catch the classes above deterministically.

```python
import json, re
d = json.load(open(SCAN_JSON)); svcs = d["services"]
def parse(s):
    m = re.search(r"\$([0-9,]+\.?[0-9]*)", str(s)); return float(m.group(1).replace(",", "")) if m else 0.0
def rec_dollar(r):  # every savings-bearing field (F1/F2) — adapters are inconsistent
    return (float(r.get("EstimatedMonthlySavings") or 0)
            or float(r.get("estimatedMonthlySavings") or 0)   # CoH camelCase (F1)
            or float(r.get("EstimatedMonthlyCost") or 0)      # unattached EBS (F2)
            or float(r.get("monthly_savings") or 0)           # snake_case adapters (network_cost, commitment, sagemaker, …)
            or parse(r.get("EstimatedSavings", "")))

# 1. Headline reconciles to the cent.
tot = sum(v.get("total_monthly_savings", 0) for v in svcs.values())
assert abs(tot - d["summary"]["total_monthly_savings"]) < 0.5, (tot, d["summary"])

# 2. Advisory-leak: a DEMOTED-RESOURCE advisory (Counted=False) must carry a 0 numeric (B1).
#    Two subtleties:
#    (a) the numeric field name varies — EstimatedMonthlySavings, estimatedMonthlySavings (CoH),
#        or snake_case monthly_savings (network_cost, commitment_analysis, …); check ALL.
#    (b) EXCEPTION — a PROJECTION/what-if advisory legitimately carries a non-zero numeric: an
#        SP/RI PURCHASE rec in commitment_analysis projects "you'd save $X IF you buy", it is NOT
#        a counted resource saving the headline excludes by accident. Exclude those projection
#        sources; everything else with a non-zero numeric is a real leak.
#        NARROWED 2026-08-10 (LS-2): "coverage-gap" is no longer in this exception — that source
#        emits nothing now. Its projection was a flat 0.30 x spend with no account-specific input,
#        i.e. a C-class fabrication wearing a projection's clothes. A projection earns this
#        exception only when its dollar comes from AWS (CE/CoH) or from live offering rates —
#        never from a hardcoded average. A non-zero numeric under sp_coverage_gaps is now a leak.
def _num(r):
    return (float(r.get("EstimatedMonthlySavings") or 0)
            or float(r.get("estimatedMonthlySavings") or 0)
            or float(r.get("monthly_savings") or 0))
PROJECTION_SERVICES = {"commitment_analysis"}  # what-if buy/coverage projections, not resource savings
leaks = [(k, sn, _num(r)) for k, v in svcs.items() if k not in PROJECTION_SERVICES
         for sn, s in v.get("sources", {}).items() for r in s.get("recommendations", [])
         if isinstance(r, dict) and r.get("Counted") is False and abs(_num(r)) > 1e-4]
assert leaks == [], leaks

# 3. Counted-but-$0 inflation — counts ALL savings fields so CoH/unattached are not false-flagged (F1/F2).
infl = [(k, sn) for k, v in svcs.items() for sn, s in v.get("sources", {}).items()
        for r in s.get("recommendations", []) if isinstance(r, dict)
        and r.get("Counted") is not False and rec_dollar(r) == 0]
# investigate each: a true $0 counted rec inflates the count; a placeholder/count-source may be legitimate.

# 4. Same physical resource counted twice (A3) — e.g. a snapshot under >1 AMI rec.
from collections import defaultdict
snap_amis = defaultdict(set)
for s in svcs.get("ami", {}).get("sources", {}).values():
    for r in s.get("recommendations", []):
        if r.get("Counted") is not False:
            for sid in re.findall(r"snap-[0-9a-f]+", json.dumps(r)):
                snap_amis[sid].add(r.get("ImageId"))
assert not {s: a for s, a in snap_amis.items() if len(a) > 1}, "shared snapshot counted twice"

# 5. Every COUNTED rec must name a resource on its card (D5). A rec whose identity
#    key is absent from the renderer's or-chain renders as "Unknown" — counted but
#    not actionable. This is the JSON-side pre-check; confirm in the HTML below.
ID_KEYS = ("resource_id", "InstanceId", "LoadBalancerName", "ClusterName", "ServerId",
           "ApiId", "DistributionId", "CertificateId", "LogGroupName", "FunctionName",
           "DBInstanceIdentifier", "VpcEndpointId", "NatGatewayId", "AllocationId",
           "EndpointName", "FileSystemId", "resourceArn", "resourceId")
nameless = [(k, sn, r.get("CheckCategory")) for k, v in svcs.items()
            for sn, s in v.get("sources", {}).items() for r in s.get("recommendations", [])
            if isinstance(r, dict) and r.get("Counted") is not False and rec_dollar(r) > 0
            and not any(r.get(key) for key in ID_KEYS)]
assert nameless == [], nameless

# 6. scanned == rendered service tabs (D3); CoH dropped-type warnings present? (E2)
print("scanned:", d["summary"]["total_services_scanned"])
print("dropped-type / fallback warnings:",
      [w["message"] for w in d.get("scan_warnings", []) if "dropped" in w["message"] or "fallback" in w["message"]])
print("permission_issues:", len(d.get("permission_issues", [])))
```

Then **regenerate the HTML** (`generate_html_report_from_json`) and confirm: every
service with rendered cards has an `id="tab-<key>"` + `id="panel-<key>"` (D2);
advisory-only services included; the corrected headline figure appears; no card
title ends in `: Unknown` (D5); and every `$0.00/month — advisory` card that has
an `AdvisoryEstimate`/`PotentialMonthlySavings` also names that figure (D6).

## How to verify a fix (especially a dedup)

1. Unit-test the exact failing scenario (shared / partial-overlap / unsizable /
   self-duplicate, etc.), asserting the dollar AND the `Counted` state.
2. Run an **independent adversarial pass** that tries to REFUTE the fix — for a
   dedup, specifically: independent-resource under-count (A1), advisory-leak (B1),
   claim-order (A4), and every other WRITER of the shared key set (A6). This
   caught real defects in two of three dedup fixes.
2b. For a **new counted lever**, additionally: render it (D5), check the metric's
   reporting criteria and dimension set (C13), and confirm any strict pricing
   probe has its own cache namespace (C12). Across six remediation tranches,
   every self-review blocker fell into one of these three.
3. Live re-scan and reconcile to the cent: the headline should move by **exactly**
   the predicted amount (the NAT recovery was +$43.07; the AMI dedup was −$43.47).
