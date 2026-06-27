# CRITICAL Cost-Correctness Remediation Prompt

A single, code-grounded remediation brief for the **29 CRITICAL** findings surfaced by the
all-services cost audit (`docs/audits/UNIFIED_AUDIT_FINDINGS.md`). Every CRITICAL below was
**adversarially verified — all 29 confirmed** against the real code (and, where a rate was
load-bearing, against the live AWS Pricing API). Paste the **PROMPT** section into a fresh
session.

Scope is **strictly cost**: every emitted recommendation must produce a concrete,
account-specific dollar saving. A finding that cannot be quantified from evidence becomes a
**$0 advisory** (`Counted=False`, rendered-not-counted) — never a fabricated counted dollar.

---

## PROMPT (copy from here)

You are remediating the **29 CRITICAL cost-correctness bugs** in this AWS Cost Optimization
Scanner. They are grouped below into **3 shared root-cause fixes** (each repairs several
services at once — do these FIRST) and **6 per-service clusters**. Every item lists exact
`file:lines`, the verified defect, the fix, acceptance criteria, and the test to add.

Work in this order, one cluster at a time, fully green before moving on. Do NOT batch unrelated
edits into one commit.

### Global rules (apply to every fix)

1. **Confirm before editing.** Re-read the cited `file:lines` and reproduce the defect mentally
   against the current code before changing anything. Line numbers are from the audit snapshot;
   re-anchor on the surrounding code, not the absolute line.
2. **Validate every rate live.** Use the AWS Pricing MCP (`get_pricing` with the correct
   service code + pinned `usagetype`/`productFamily`/SKU filters) and the AWS Knowledge MCP.
   Never trust a hardcoded rate or memory. Record the validated value in the finding's
   `AuditBasis`.
3. **The advisory-$0 pattern is the default remedy for any saving you cannot quantify from
   evidence.** Mirror `services/adapters/lambda_svc.py` (metric-gated $0 advisory): set
   `Counted=False`, `EstimatedMonthlySavings=0.0`, and an honest `EstimatedSavings` string
   ("$0.00/month — advisory: …"). The rec still renders; it just does not feed the headline.
4. **Classify AWS errors, never swallow them.** Replace `except: pass` / `except: continue` /
   `logger`-only paths with `services/_aws_errors.record_aws_error(ctx, e, service=…, …)`:
   `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` → `ctx.permission_issue`, everything
   else → `ctx.warn`. Reference uses: `services/adapters/network.py`, `services/rds.py`.
5. **Fail safe on safety-critical recs.** For any rec that recommends **deletion/termination**
   (AMI deregister, MediaStore container delete, DMS "unused"), a *failed* evidence read must
   **abstain** (do not emit), never assert "unused/delete".
6. **Immutability.** Build new rec dicts; do not mutate shared inputs in place.
7. **Counted == rendered.** The number summed into `total_monthly_savings` must equal the
   dollar shown on the card. Kill every string-vs-number desync.
8. **Each counted finding carries a structured `AuditBasis`** (rate / region / metric-window /
   formula) so the dollar is defensible from the report alone.
9. **Tests + regression gate.** For each fix, add/extend a `tests/test_<svc>_audit_fixes.py`
   mirroring `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive the
   pure logic and the `scan()` path with a `SimpleNamespace` ctx + fake boto3 paginators.
   Keep the gate green: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
   The two SHARED fixes (SR-1, SR-2) and SR-3 **will** shift the golden/reporter snapshots —
   refresh with `SNAPSHOT_UPDATE=1` **only** after you have eyeballed the diff and confirmed it
   is the intended dollar change, and say so in the commit.
10. **Stage only the files you changed.** One cluster → one focused commit.

### Sibling reference patterns (study before fixing)

- **CoH consumption + CoH>heuristic dedup by normalized id:** `services/rds_logic.py`,
  `services/adapters/rds.py` (`ctx.cost_hub_splits["rds"]`).
- **Metric-gated $0 advisory + arch-aware constant:** `services/adapters/lambda_svc.py`.
- **Exact current→target price delta (not a reduction factor):** `services/adapters/ec2.py`,
  `services/ebs.py`.
- **Deterministic, usagetype-filtered Pricing lookup:** `core/pricing_engine.py`
  `_select_efs_storage_rate` (≈1346-1374) and the hourly unit-guarded path
  `_call_pricing_api_hourly`.
- **AWS error classification:** `services/_aws_errors.py` `record_aws_error`.

---

## SHARED ROOT-CAUSE FIXES (do these first — each repairs multiple services)

### SR-1 — `core/pricing_engine.py`: non-deterministic `MaxResults=1` SKU selection corrupts every generic instance price
**Repairs:** ElastiCache **C2**, Redshift **C2** (and materially improves DMS H1, MSK pricing).
**Files/lines:** `core/pricing_engine.py:1290-1298` (`_fetch_generic_instance_price`) and
`1590-1622` (`_call_pricing_api`).

**Defect (verified live).** `_fetch_generic_instance_price` filters only on `instanceType +
location`; `_call_pricing_api` requests `MaxResults=1` and returns `_extract_usd(PriceList[0])`
with no `productFamily` / `usagetype` / price-unit guard. A single `instanceType` matches many
OnDemand SKUs and boto3 ordering is **not guaranteed**, so the returned rate is frequently the
wrong pricing dimension:
- **Redshift `ra3.4xlarge`** matches 4 SKUs — Concurrency Scaling ($0.0009/**sec**), Managed
  Storage ($0.024/GB-Mo), CSFreeUsage ($0.00), and the correct Compute Instance ($3.26/**Hr**).
  Compute Instance was returned **last**; `MaxResults=1` yields $0.0009 → $0.66/mo vs the true
  $2,379.80/mo (~3,600× low), or $0 (silently dropped by the `if monthly>0` guard).
- **ElastiCache `cache.r6g.large`** matches 6 SKUs across engines — Redis ExtendedSupport
  ($0.33), Redis/Memcached NodeUsage ($0.206), Valkey NodeUsage ($0.1648), Valkey SyncDurability
  surcharge ($0.0297). `MaxResults=1` grabbed the $0.33 Extended-Support SKU (+60%).

**Fix.** Make the generic instance-price path **deterministic and unit-correct**:
1. Add an optional, service-aware filter set to `_fetch_generic_instance_price`: pin
   `productFamily` and/or `usagetype`, and require the price **unit** to be `Hrs`.
   - Redshift: `productFamily="Compute Instance"` (and/or `usagetype LIKE 'Node:'+type`); reject
     Concurrency Scaling / Managed Storage / Free-Usage rows.
   - ElastiCache: `usagetype CONTAINS 'NodeUsage'` AND **not** ExtendedSupport/SyncDurability/
     SnapshotStorage, AND `cacheEngine == <cluster engine>` (Redis/Memcached/Valkey).
2. Fetch `MaxResults=100` and **select the row whose price unit is `Hrs`** and whose `usagetype`
   matches the canonical `…NodeUsage:<type>` / `Node:<type>` pattern — mirror
   `_select_efs_storage_rate`'s usagetype-filter loop. Reject `$0` rows.
3. Plumb the discriminator down from the shims: ElastiCache must pass the **cluster engine** to
   the price call (see ElastiCache C1/C2 cluster); Redshift passes node type.

**Acceptance criteria.** For a fixed `(instanceType, region, engine)` the function returns the
**same** node-hour rate on repeated calls, equal to the live Pricing-API Compute-Instance/
NodeUsage `Hrs` SKU (validate both Redshift `ra3.4xlarge` and ElastiCache Redis
`cache.r6g.large`). No per-second / per-GB / Extended-Support / SyncDurability SKU can ever be
returned as a node-hour price.

**Test.** `tests/test_pricing_engine.py`: feed a fake multi-SKU `PriceList` (in adversarial
order — correct SKU last) and assert the `Hrs`/`NodeUsage` row is chosen deterministically and
the per-second/per-GB rows are rejected.

### SR-2 — `html_report_generator.py`: flat **$50-per-rec** fabrication when a service total is $0
**Repairs:** API Gateway **C1**, Step Functions **C2** (note: `opensearch` is the third
`_FLAT_SAVINGS_SERVICES` member — its CRITICALs are different, but remove it here too).
**Files/lines:** `html_report_generator.py:82` (`_FLAT_SAVINGS_SERVICES`), `3076-3094`
(`_calculate_service_savings`), consumed at `3113-3119` (`_get_service_content`), `2831`
(exec-summary chart), `2281-2284` (reconciliation footnote).

**Defect (verified).** `_FLAT_SAVINGS_SERVICES = {"opensearch","api_gateway","step_functions"}`.
`_calculate_service_savings` returns the real total only when `total_monthly_savings > 0`;
otherwise it adds **$50 per rec** for these services with no rate/metric/`Counted` check. Because
the per-service total is **$0 in the default case** (fast_mode, no per-stage CloudWatch metrics,
throttled reads — and for Step Functions C1 makes it *always* $0), every emitted REST API /
state machine becomes a fabricated $50 in the tab headline, executive summary, and
reconciliation footnote. It is bimodal: one real metric suppresses the override for all recs.

**Fix.**
1. Delete `_FLAT_SAVINGS_SERVICES` and the `+= 50` branch in `_calculate_service_savings`
   (lines ~3082-3094). The reporter must never invent a dollar the adapter did not count.
2. In each affected adapter, when there is no quantifiable saving (e.g.
   `monthly_requests == 0` for API Gateway; the Standard→Express lever for Step Functions),
   emit the rec with `Counted=False` and `EstimatedMonthlySavings=0.0` so it renders as a $0
   advisory.

**Acceptance criteria.** With a zero-metric account, API Gateway and Step Functions tabs show
**$0 counted** (advisory cards rendered), not `$50 × N`. The executive-summary total and the
reconciliation footnote equal the sum of genuinely counted dollars.

**Test.** `tests/test_reporter_snapshots.py` (or a focused reporter unit test): a service_data
with `total_monthly_savings=0` and N recs renders `$0.00`, not `$50·N`. Refresh the reporter
snapshot intentionally.

### SR-3 — Orphaned **Cost Optimization Hub** buckets silently dropped
**Repairs:** ElastiCache **C1**, OpenSearch **C1**, Redshift **C1** (matches the existing
"Orphaned Cost Hub buckets" memory note).
**Files/lines:** `core/scan_orchestrator.py` (`_HUB_SERVICES` ≈57-59, `type_map`
`ElastiCacheCluster`/`OpenSearchDomain`/`RedshiftCluster` ≈92-94, splits stored ≈124-144);
adapters `services/adapters/elasticache.py`, `opensearch.py`, `redshift.py` (`scan()` never reads
`ctx.cost_hub_splits`).

**Defect (verified).** The orchestrator buckets `ElastiCacheCluster`/`OpenSearchDomain`/
`RedshiftCluster` CoH recommendations into `ctx.cost_hub_splits[<svc>]`, but **no adapter
consumes them** (the only consumer, `cost_optimization_hub.py`, is retired from `ALL_MODULES`).
The unbucketed-type warning fires only for *unmapped* types, so these populated-but-unconsumed
buckets drop with **zero** warning — losing the highest-authority, account-specific AWS savings.

**Fix (per adapter, mirror `services/rds_logic.py`).**
1. In `scan()`, read `recs = getattr(ctx, "cost_hub_splits", {}).get("<svc>", [])`.
2. Emit a `cost_optimization_hub` SourceBlock; sum `estimatedMonthlySavings` via the existing
   CoH/`compute_optimizer_savings` helper.
3. Add **authority dedup CoH > heuristic** by **normalized cluster/domain id** (strip ARN;
   mind cluster-vs-node): when CoH covers a resource, demote the overlapping heuristic lever
   (Graviton/Underutilized/Valkey, etc.) to `Counted=False`.
4. Demote CoH **RI/commitment** rows to advisory (they belong to `commitment_analysis`, which
   already consumes its own split — do not double-count).

**Acceptance criteria.** A scan with CoH recommendations for an ElastiCache cluster / OpenSearch
domain / Redshift cluster shows those dollars in the respective tab, counted once, with the
overlapping heuristic demoted. No silent drop.

**Test.** Per adapter: a fake `ctx.cost_hub_splits["<svc>"]` with one cluster rec + a heuristic
rec for the same normalized id ⇒ CoH counted, heuristic `Counted=False`, total not double-counted.

---

## PER-SERVICE CLUSTERS

### Cluster A — Dead / no-op savings levers (structurally $0; re-wire or honestly demote)

**apprunner C1** — `services/apprunner.py:24-60` (+ adapter `apprunner.py:81-99`). The shim
initializes three buckets and **never appends** to any (the only loop body binds `_ =
instance_config`); `get_enhanced_apprunner_checks` always returns `{"recommendations": []}`, so
the adapter's dual-billing pricing loop is dead code and App Runner produces **$0/0 recs for
every account** (confirmed by the golden fixture). **Fix:** implement at least one defensible
counted check — e.g. an idle/zero-request or `PAUSED`-but-provisioned service flagged
pause/delete, priced at the real provisioned+active (vCPU $0.064/hr + memory $0.007/GB/hr) cost,
appended to a bucket — **or** remove the module from `ALL_MODULES` and document it inert. Then
fix its latent H-items (App Runner H1 invalid CloudWatch dimension, H2 error classification).

**athena C1** — `services/athena.py:24-53` (+ adapter `athena.py:39-81`). The `checks` dict is
initialized and **never appended to** (loop body only does `_ = (output_location, config)`), so
`recommendations` is always `[]` and the `$5/TB` ProcessedBytes pricing loop never runs — the
Athena tab is permanently empty. **Fix:** emit one rec per workgroup carrying `WorkGroup` (and
`ProcessedBytesTB`) so the pricing loop can price it; fix at the same time H2 (bill per **TB =
10¹²**, not TiB 2⁴⁰ — current code understates 9.05%), H1 (CloudWatch failure → classify, not
silent $0), and H3 (paginate).

**bedrock C1** — `services/adapters/bedrock.py:138,142,148,180,183,189` (CW dims 71,98,113).
The adapter reads `pt.get("modelId","")`, but the botocore `ProvisionedModelSummary` shape has
**no `modelId`** (members are `foundationModelArn`, `modelArn`, `provisionedModelArn`,
`modelUnits`, …). So `model_id` is always `""` ⇒ (a) `PT_HOURLY_PRICE.get("",1.0)` → fabricated
$1/hr default; (b) CloudWatch queries `Dimensions=[{ModelId:""}]` → no datapoints → both counted
checks (`idle_provisioned_throughput`, `pt_breakeven_analysis`) short-circuit. PT analysis is
**structurally dead**. **Fix:** derive the rate key from the real foundation model id parsed from
`pt["foundationModelArn"]` (or `modelArn`); use `pt["provisionedModelArn"]` as the CloudWatch
`ModelId` dimension value (H4) and rec id; then fix H1 (don't count an unknown-rate PT at the $1
default — demote to advisory) and H2/H3 (PT price-key truncation; per-token breakeven model).
Add a test driving `_check_idle_pt` with a realistic `ProvisionedModelSummary`.

**step_functions C1** — `services/adapters/step_functions.py:73-92`. `eligible_for_migration =
state_count > 25 and avg_duration_sec < 60`, but `StateCount`/`AvgDurationSec` are **never set by
any producer** (defaults 5 and 0), so the predicate is **always False** and the
`savings += … *0.025*0.60*…` block never executes — `total_monthly_savings` is structurally **$0**
(this is the root cause of the SR-2 flat-$50 for Step Functions). **Fix:** either (a) demote
`standard_vs_express` to advisory (`Counted=False`, return $0 honestly), or (b) compute a real
delta — fetch the ASL via `describe_state_machine` to count states, read actual avg
duration/memory, model Express ($1/M requests + $0.00001667/GB-s) vs Standard ($0.025/1K
transitions) as current−target. Remove the dead `eligible_for_migration` gate or feed it real
data. **Do C1 before SR-2's Step Functions advisory flagging.**

### Cluster B — Fabricated $ from a config dimension with no usage metric (gate on evidence or demote)

These all compute a counted dollar from capacity/size/instance-name alone, with **no usage
signal**. The remedy is the same shape: gate the counted dollar on a real metric (CloudWatch /
job-runs / consumed-capacity), declare `requires_cloudwatch`/`reads_fast_mode` if you read CW,
and when there is no evidence emit a **$0 advisory** (mirror `lambda_svc.py`). Record an
`AuditBasis`.

- **aurora C1** — `services/adapters/aurora.py:302-328`. Serverless v2 "waste" =
  `max_acu × (1 − ACUUtilization)` credits the unbilled gap between the MaxACU **ceiling** and
  actual consumption — but v2 bills **consumed ACU**, not MaxCapacity, so lowering Max saves $0
  unless the workload hit the ceiling (e.g. ~$1,051/mo fabricated at 25% util, max 16 ACU).
  **Fix:** do not credit `(1−util)×max_acu`; demote to $0 advisory **or** count only evidenced
  consumed-ACU above a concrete proposed lower Max. Also fix the metric name
  (`ServerlessV2CapacityUtilization` → AWS canonical `ACUUtilization`).
- **batch C2** — `services/adapters/batch.py:59-61`. Graviton saving = `hourly × 730 × rate`
  with a hardcoded 24/7 assumption; Batch CEs are bursty (minvCpus→maxvCpus). **Fix:** derive
  hours from CloudWatch `CPUUtilization` datapoint count / Batch `ListJobs` runtime / a
  `minvCpus` floor, gated on `fast_mode`; no runtime evidence → $0 advisory.
- **glue C2** — `services/adapters/glue.py:44-49,66-74`. `ASSUMED_MONTHLY_DPU_HOURS = 160`
  multiplied into every counted saving with no run history. **Fix:** derive monthly DPU-hours
  from `glue.get_job_runs` (Σ `ExecutionTime×DPU` / `DPUSeconds` over a trailing window); no
  history → $0 advisory.
- **msk C1** — `services/adapters/msk.py:40-56`. Counted saving = `(broker+storage) × 0.30`
  blanket factor, no utilization, no target instance (the rec's own Note admits it). **Fix:**
  compute an exact current→target broker price delta (one size down, both via
  `get_msk_broker_hourly_price`, mirror `ec2.py`) gated on a real utilization signal, **or**
  demote to $0 advisory.
- **sagemaker C1** — `services/adapters/sagemaker.py:175-217`. `_check_idle_notebooks` flags
  **every** `InService` notebook and counts its **full** monthly cost as savings with **zero**
  idle evidence; does not honor `fast_mode`. **Fix:** gate on a real idle signal
  (`describe_notebook_instance` `LastModifiedTime` age, or a CW agent metric); count only when
  proven idle, else $0 advisory; honor `ctx.fast_mode`.
- **lightsail C1** — adapter `lightsail.py:48-52` / shim `lightsail.py:73-86`. `oversized_instances`
  is name-based (no metric); the card shows `bundle_cost × 0.3` but the adapter counts the
  **full** bundle cost (3.33× over-count + headline-vs-card desync). **Fix:** make oversized
  advisory ($0, `Counted=False`) **or** count the exact current−one-size-down bundle delta; stop
  summing full bundle cost for a "verify utilization" nudge. (Also fixes its string-vs-counted
  desync — Cluster F.)
- **dynamodb C1** — see Cluster D (it is both a stacking double-count **and** an unmetered-factor
  fabrication; the dedup is the primary fix, advisory demotion of Reserved is the secondary).

### Cluster C — Silent failures that fabricate savings or cause false-positive deletion (classify + fail safe)

- **ami C1** — `services/ami.py:92-119`. Launch-template / ASG / launch-config resolution uses
  `except: pass`; a denied/throttled describe leaves in-use `ImageId`s out of `running_amis`, so
  an AMI referenced **only** by an LT/ASG (scaled-to-0 ASG, scale-out launch template) passes the
  `if ami_id in running_amis` guard and is emitted as **"deregister and delete snapshots"** — a
  destructive false positive with no operator signal. **Fix:** classify the error via
  `record_aws_error`, and **fail safe** — when any reference source cannot be enumerated, suppress
  unused-AMI emission (treat unresolved references as in-use). Also fix H1 (outer `except` empties
  the whole tab silently) and H2 (paginate LT/ASG describes).
- **dms C2** — `services/dms.py:70-99`. `avg_cpu = sum(...) / max(len(Datapoints),1)` → with zero
  datapoints = **0.0**, which passes both `<30` and `<5`, flagging a metric-less / brand-new
  instance as rightsizable **and** "unused". **Fix:** guard on datapoint count — `len==0` (or below
  a coverage threshold) ⇒ skip or $0 advisory; compute avg only over real datapoints.
- **dms C3** — `services/dms.py:100-113`. The inner `except Exception:` around
  `get_metric_statistics` swallows AccessDenied/Throttling/OptInRequired and **still appends a
  counted 35% rec** with no metric and no warn — account-wide CW AccessDenied ⇒ every instance
  gets a fabricated 35% saving. **Fix:** classify the error via `record_aws_error`; do **not**
  emit a counted rec on metric failure (drop or $0 advisory).
- **mediastore C1** — `services/mediastore.py:45-58,60-73,75-89,95-109`. The 3 activity reads use
  `except: continue` (no warn); the unused-container rec fires on `total_activity == 0`, which is
  also the value when the reads merely **failed**, while the independent size read can still
  succeed → an **active** container is flagged "no activity → consider deletion" with a non-zero
  saving. **Fix:** classify each `get_metric_statistics` failure via `record_aws_error`; on **any**
  activity-read failure set a `read_failed` flag and **skip** the container; only emit when all
  three activity reads succeeded **and** returned datapoints.

### Cluster D — Intra-adapter double-count / stacking (dedup; cap at monthly cost)

- **dms C1** — adapter `dms.py:39-48` / shim `services/dms.py:74-99`. The `avg_cpu < 5` block is
  **nested inside** `avg_cpu < 30`, so a very-low-CPU instance is appended to **both**
  `instance_rightsizing` **and** `unused_instances`; `scan()` prices **every** rec at
  `monthly × 0.35`, so one InstanceId is counted ~**0.70×** monthly (e.g. dms.c5.large $86.87 →
  counted $60.81). **Fix:** dedup by `InstanceId` before pricing — an instance is counted **once**
  (either terminate/full-cost OR a single rightsizing lever), never both; price from a per-InstanceId
  set, not the flattened rec list. (Do alongside C2/C3 — same shim.)
- **dynamodb C1** — `services/adapters/dynamodb.py:114-130` (factors at `services/dynamodb.py:82-89`,
  recs at `374-395`). A table with RCU≥101 **and** WCU≥101 emits **both** over-provisioned (×0.40)
  **and** reserved-capacity (×0.66) recs; `scan()` sums both factors → **1.06× the table's entire
  monthly cost** (physically impossible >100%). They are mutually-exclusive remedies (rightsize OR
  reserve). **Fix:** emit only one rec per table (or demote Reserved to advisory per H2) and never
  sum both; cap any single table's counted saving at its `monthly_current`.

### Cluster E — Wrong rate / wrong region scaling (pin the SKU; respect region-flat rates)

- **quicksight C1** — `services/adapters/quicksight.py:38-50`. `SPICE_PRICE_PER_GB = 0.38` is
  applied to **all** accounts; live Pricing API confirms **Standard SPICE = $0.25/GB-Mo** (SKU
  `T4GAEKP5WQQWCUD5`) vs **Enterprise = $0.38** (SKU `R8PKSKFCHES8YSKK`), so every Standard account
  is overstated **52%**. Edition is available from `describe_account_subscription().AccountInfo.Edition`
  but the helper reads only `AccountSubscriptionStatus`. **Fix:** resolve `Edition`, pin the SPICE
  rate per edition via a pinned-filter Pricing lookup; if edition cannot be resolved, mark the rec
  `Counted=False`. (Fix H3 at the same time: the display string drops `pricing_multiplier` while the
  counted number applies it — single-source the value.)
- **transfer C1** — adapter `transfer.py:59-63` / shim `transfer_svc.py:54`. The counted path
  multiplies `removable × 0.30 × 730 × pricing_multiplier`, but live Pricing API confirms
  `ProtocolHours = $0.30/hr` is **identical across regions** (region-flat), so the EC2-derived
  `pricing_multiplier` (1.08–1.25) inflates the headline +8%…+25%; the shim string omits the
  multiplier, so card ≠ counted (e.g. ap-southeast-2: card $438.00, counted $503.70). **Fix:** do
  **not** apply `pricing_multiplier` to the region-flat ProtocolHours rate; single-source one
  region-correct dollar (shim carries an integer `RemovableProtocols`, adapter owns the string +
  number) so card == counted in all regions. Attach an `AuditBasis`.

### Cluster F — String-vs-counted desync & counted-but-$0 (make the card equal the headline)

- **batch C1** — adapter `batch.py:55-62` / shim `batch_svc.py:43-99`. Only the **Graviton** rec
  carries `InstanceTypes`, so only it is priced; Fargate-Spot / EC2-Spot / Job-Rightsizing recs
  ship "60–90%" `EstimatedSavings` **strings** that render on cards but contribute **$0** to the
  headline (Graviton-only total). **Fix:** either (a) rewrite their strings to honest
  "$0.00/month — advisory: enable Spot/Fargate-Spot" with `Counted=False`, or (b) wire real
  pricing — Spot = on-demand−Spot via `describe_spot_price_history`; Fargate-Spot = Fargate
  on-demand−spot delta; Job-Rightsizing = current−target vCPU/mem. Also fix H1 (`is_fargate`
  detection broken — every Fargate CE misclassified into the EC2 branch).
  *(Note: the verifier flagged batch C1 as a trust/display desync rather than a true dollar
  fabrication — lower-priority than the Cluster B fabrications, but still fix the honesty gap.)*
- **glue C1** — `services/adapters/glue.py:52-58` (+ shim `services/glue.py:33-43`). The shim always
  injects `MaxCapacity` (default 0), so `dpu_count = rec.get("MaxCapacity", rec.get("NumberOfWorkers"))`
  returns **0** for every modern worker-based job (key exists → never falls through) → priced at
  **$0**, counted but contributing nothing, with no `PricingWarning`. Only legacy `MaxCapacity>10`
  jobs ever produce a non-zero dollar. **Fix:** resolve by **truthiness** —
  `dpu_count = rec.get("MaxCapacity") or rec.get("NumberOfWorkers")` (or have the shim omit the zero
  key) — then apply the `WorkerType→DPU` multiplier (H3: G.1X=1, G.2X=2, …) so `NumberOfWorkers` is
  converted to DPU before pricing.
- **lightsail C1** — see Cluster B (the same fix removes both the fabrication and the desync).

### Cluster G — Mis-attribution (anchor on full usage-type tokens)

- **network_cost C1** — `services/adapters/network_cost.py:219-228`. The transfer-bucket classifier
  matches bare substrings, so (validated live): cross-AZ `DataTransfer-Regional-Bytes` matches the
  `'region'` branch → scored ×0.30 in the **Cross-Region** tab; inter-region `USE1-USW2-AWS-Out-Bytes`
  falls through to the `else` → **egress** ×0.40; the `cross_az` keyword set is effectively dead. The
  "Cross-Region Spend" stat card is actually cross-AZ traffic. **Fix:** anchor on full usage-type
  tokens: inter-region = `<SRC>-<DST>-AWS-(In|Out)-Bytes` (two region codes); cross-AZ =
  `DataTransfer-Regional-Bytes`; egress = `DataTransfer-Out-Bytes`/`-Out-ABytes`. Test cross-AZ
  (Regional) **before** inter-region; drop the bare `'region'`/`'az'` substrings. Add tests feeding
  `USE1-DataTransfer-Regional-Bytes` (→ cross_az) and `USE1-USW2-AWS-Out-Bytes` (→ cross_region).

---

## Execution order & dependencies

1. **SR-1** (pricing engine) → unblocks correct node pricing for ElastiCache C2 / Redshift C2.
2. **SR-3** (CoH buckets) → ElastiCache C1 / OpenSearch C1 / Redshift C1 (depends on SR-1 for the
   heuristic levers it dedups against to be correctly priced).
3. **step_functions C1** → then **SR-2** (flat-$50) for API Gateway + Step Functions.
4. Clusters A, B, C, D, E, F, G — independent; tackle by service. Within a service, do all its
   CRITICALs (and the named H-items that share the same code path) in one commit.
5. **opensearch C2** (idle domain priced $0 — `opensearch.py:46-49,58-64,87` + shim `147-155`):
   add an `Idle Domain` rate = full domain cost (`get_instance_monthly_price(type) × InstanceCount
   + storage`); requires the shim to carry `InstanceType`+`InstanceCount`(+EBS) on the idle rec;
   ensure the idle/delete lever wins the per-domain dedup over Graviton. Do **after** SR-1 + SR-3.

## Definition of done

- All 29 CRITICALs fixed (or explicitly, honestly demoted to $0 advisory with `Counted=False`).
- Every counted dollar validated against the live AWS Pricing API and carrying an `AuditBasis`.
- `counted == rendered` for every affected adapter (no string-vs-number desync).
- New/updated `tests/test_<svc>_audit_fixes.py` per service; full suite green.
- Regression gate green: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`
  (golden/reporter snapshots refreshed **intentionally** only for SR-1/SR-2/SR-3, with the diff
  reviewed and called out).
- `services/adapters/CLAUDE.md` rows reconciled where a fix changes the pricing/dedup behavior.
- Stage only changed files; one cluster per commit.

## Follow-ups (not CRITICAL — track separately)

- 83 HIGH findings (see `docs/audits/UNIFIED_AUDIT_FINDINGS.md` → "P1 — Confirmed HIGH"); several
  named H-items above are bundled into the CRITICAL fixes — check them off there.
- The 1 **refuted** finding (lambda H1) is excluded — do not action it.
- The 4 **revised** findings (cloudfront H2, lightsail H2, opensearch H3, step_functions H1) carry
  verifier corrections — read the "corrected" note before fixing.

## PROMPT (end)
