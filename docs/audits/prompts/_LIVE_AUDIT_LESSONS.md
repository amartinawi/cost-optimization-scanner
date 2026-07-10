# Live-Audit Lessons — recurring cost-fidelity bug classes

Bug classes **confirmed in live deep audits** across multiple accounts/regions
(eu-central-1, eu-west-1, ap-south-1, ap-southeast-1; accounts level-Shoes-prod,
bnc, tadweer-prod — 2026-06-29 → 2026-06-30). Each was a real finding that shipped
a fix. Paste this file **alongside** any per-service `*_AUDIT_PROMPT.md`: the
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
#        SP/RI purchase or coverage-gap rec in commitment_analysis projects "you'd save $X IF you
#        buy", it is NOT a counted resource saving the headline excludes by accident. Exclude those
#        projection sources; everything else with a non-zero numeric is a real leak.
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

# 5. scanned == rendered service tabs (D3); CoH dropped-type warnings present? (E2)
print("scanned:", d["summary"]["total_services_scanned"])
print("dropped-type / fallback warnings:",
      [w["message"] for w in d.get("scan_warnings", []) if "dropped" in w["message"] or "fallback" in w["message"]])
print("permission_issues:", len(d.get("permission_issues", [])))
```

Then **regenerate the HTML** (`generate_html_report_from_json`) and confirm: every
service with rendered cards has an `id="tab-<key>"` + `id="panel-<key>"` (D2);
advisory-only services included; the corrected headline figure appears.

## How to verify a fix (especially a dedup)

1. Unit-test the exact failing scenario (shared / partial-overlap / unsizable /
   self-duplicate, etc.), asserting the dollar AND the `Counted` state.
2. Run an **independent adversarial pass** that tries to REFUTE the fix — for a
   dedup, specifically: independent-resource under-count (A1), advisory-leak (B1),
   claim-order (A4). This caught real defects in two of three dedup fixes.
3. Live re-scan and reconcile to the cent: the headline should move by **exactly**
   the predicted amount (the NAT recovery was +$43.07; the AMI dedup was −$43.47).
