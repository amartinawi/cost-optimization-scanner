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
