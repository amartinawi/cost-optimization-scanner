# Remediation Ledger — verbatim capture (Wave 0 input)

**Source:** user-pasted artifact text (originally a claude.ai/code/artifact URL that returned
"Page not found"; user pasted the content directly). This file is the canonical capture all
validation greps run against. DO NOT edit — it is the artifact under test.

---

# AWS Cost Optimization Scanner · Audit Remediation Sweep Remediation Ledger

Eight tranches closing the findings of a 31-adapter blind sweep. Every entry below is a merged
commit on main, grouped by the run that shipped it and marked by what it did to the number the
scanner reports.

2026-08-09 → 2026-08-10
tranche 1 `10f0b0c` → tranche 8 `7cce9f5`
51 code commits
17 Wrong dollars removed
18 Real dollars recovered
16 Correctness only
8/8 Ranking items closed
~33 Tail items left

↓ removed a counted dollar that was wrong — phantom, fabricated, or double-counted
↑ recovered a real saving the scanner was missing or refusing to count
● correctness no change to the total: plumbing, gating, rendering, or a fail-open closed

## The invariant behind every entry

A counted dollar must be account-specific and defensible. Anything speculative is a $0 advisory
that still renders but is never summed. Where a fix could go either way, it goes toward
under-counting.

---

## 1 `10f0b0c` — Ranking items 1–5
The highest-dollar phantoms, plus the two adapters that were reporting nothing at all.

- **AR-1 ↓ removed — apprunner.** A bare-numeric Memory config was read as GB when App Runner
  reports MB — a 1024× counted phantom on every idle service.
- **OS-1 · OS-2 ↓ removed — opensearch.** The extended-support surcharge read was not
  region-scoped, so the phantom multiplied per scanned region. Storage was also priced without
  the data-node count.
- **NC-1 ● correctness — network_cost.** The Cost Explorer transfer query used a dimension that
  matched nothing, so the adapter returned empty on every account. Rewritten with a strict
  classifier and a zero-row warning. Still unverified against a live account.
- **SM-1 · SM-3 ↑ recovered — sagemaker.** Idle reads used a dimension set AWS does not publish,
  so no endpoint ever read as busy; fleet cost ignored variant instance counts — a ~87%
  under-count.
- **SM-2 · BR-1…BR-4 ↓ removed — sagemaker · bedrock.** A flat 30% consolidation factor, and
  Provisioned Throughput dollars built on a blended token rate and an unverified rate table —
  all demoted to advisory.

**Review blocker fixed before merge.** The SageMaker idle verdict failed open: a denied
CloudWatch read produced a counted delete recommendation. Now guarded by a creation-time check
and a fail-closed verdict.

## 2 `f6c6c2e` — Ranking item 6, first half
Flat-rate fabrications and levers that counted without evidence.

- **LS-1 ↓ removed — lightsail.** Globally flat bundle rates were being region-scaled,
  fabricating a regional premium that does not exist. Whole-tab phantom.
- **CF-1 · CF-2 ↓ removed — cloudfront.** An advisory-only adapter never set Counted=False, so
  every $0 card counted as a recommendation — contradicting its own docstring.
- **EC2-2 · EC2-3 ↓ removed — ec2.** Auto Scaling recommendations bypassed the commitment gate
  entirely; the dedicated-tenancy lever assumed a flat 30% where the real delta is ~6%.
- **EC2-4 ● correctness — ec2.** A denied autoscaling:Describe* could raise the counted total by
  making ASG members look standalone. Losing evidence now demotes.
- **EKS-1 · EKS-2 ↓ removed — eks.** Idle clusters were flagged without corroboration, and a
  failed capacity enumeration read as "zero nodes" — a counted delete on a live Fargate cluster.
- **CN-1 ↑ recovered — containers.** ECS rightsizing was gated on Container Insights being
  enabled, though the metrics it needs are standard and free.
- **MON-1 · MON-2 ↓ removed — monitoring.** The stale-custom-metric lever was inverted — it
  flagged active metrics. Deleted and replaced with a measured-spend advisory.

## 3 `e9ccd4f` — Ranking item 6 remainder · item 7a
Checks that could never fire, and the first pass at the Cost Optimization Hub plumbing.

- **H3 · H4 · H5 ↑ recovered — commitment.** The expiry check read a misspelled Cost Explorer
  key and a field that does not exist — dead on every account. RI waste read a nonexistent
  field and emitted counted-$0 rows. Both fixtures encoded exactly those wrong shapes, so the
  gate passed by construction.
- **DDB-A · DDB-C ↑ recovered — dynamodb.** AWS's own computed savings were discarded because
  the covered-table set was built from the wrong rows. CoH now counts and the local lever
  demotes.
- **LAM-1 · LAM-2 ↓ removed — lambda.** Provisioned Concurrency dollars escaped the Compute
  Savings Plan gate — AWS documents that a Compute SP covers PC. The counted dollar was also
  unrenderable behind a percentage string.
- **WS-3 ↓ removed — workspaces.** A missing ComputeTypeName silently defaulted to STANDARD and
  booked a fabricated $35 — highest-risk on exactly the ERROR-state WorkSpaces the check targets.
- **FS-1 · FS-6 ↓ removed — file_systems.** Idle EFS priced every byte at the Standard rate
  though the API reports the class split — a ~$1,267/mo phantom on a 90%-tiered 5 TB file
  system. FSx SSD→HDD is not an in-place change and was demoted.
- **rank 7a ● correctness — orchestrator · report.** Six CoH type_map keys that are not real
  ResourceTypes removed, with a hygiene test validating every key against the live botocore enum.
  The EC2 advanced-checks renderer now honors Counted.

## 4 `1ad837d` — Ranking item 7b · verification gaps
Metric names, pagination, and the CoH types that were real but unrouted.

- **TR-2 ● correctness — transfer.** AWS/Transfer publishes BytesIn/BytesOut, not
  BytesUploaded/BytesDownloaded. The wrong names returned empty with no error — any idle gate
  built on it would have read "no traffic" for every server.
- **GL-1 ↑ recovered — glue.** get_dev_endpoints was a one-shot call. Every endpoint past the
  first page was dropped, at $1,606/mo for a 5-DPU endpoint.
- **NET-B · NET-E ↑ recovered — load_balancer.** A :.0f-only string made the headline take $16
  for a $16.43 load balancer while numeric consumers read $0. New lever: a load balancer with
  listeners but zero registered targets, tri-state so any ambiguity abstains.
- **rank 7b ↑ recovered — orchestrator · rds · aurora.** Four real CoH types routed. Storage
  recs count but never suppress — "shrink this volume" is a different remediation from "disable
  this Multi-AZ".

**Blocker found in self-review — and the lesson it became.** The suppression guard had three
writers, not one. Guarding rds_logic and the aurora adapter left RdsModule publishing the same
id through the back door, defeating the fix two lines earlier. Recorded as lesson A6.

## 5 `37b0fb9` — Ranking item 8 — new levers
Five charges nothing detected. Every rate re-verified against the live Pricing API rather than
the sweep's citation; two of those checks changed the implementation.

- **CF-4 ↑ recovered — cloudfront.** Dedicated-IP custom SSL is a flat $600/month charge. Billed
  per certificate, not per distribution — two distributions sharing one cost $600, not $1,200 —
  and only while the distribution is enabled.
- **AG-3 ↑ recovered — api_gateway.** REST stage caches bill 24/7 at $14.60–$2,774/month by
  size. The code had found the rate and deleted the lever; counted now only on a proven-zero
  30-day stage read.
- **TR-1 · TR-3 ↑ recovered — transfer.** An ONLINE server bills $219/month per protocol
  regardless of traffic. Also: ListedServer has no Protocols member, so the protocol lever read
  [] on every real account and could never fire.
- **MSK-1 · MSK-5 ↑ recovered — msk.** Idle clusters now count their whole broker + storage
  spend. NumberOfBrokerNodes no longer defaults to 3 — harmless while advisory, a fabricated 3×
  multiplier once counted.
- **MON-3 ● correctness — monitoring.** CloudWatch Logs ingestion measured at the $0.50→$0.25/GB
  Standard→IA delta, and deliberately not counted: AWS documents that log class cannot be
  changed after creation, so it needs a new log group and every producer repointed.

**Both blockers were render-side.** Every lever was correct in the adapter and wrong on the
card: CF-4 rendered "Unknown" because the extractor only knew DistributionId while the rec is
keyed by certificate; AG-3's two stages of one API rendered as identical rows. Recorded as
lesson D5.

## 6 `8a7f7b0` — The wrong-dollar slice of the tail
With the ranking exhausted, the tail was cut by the invariant instead: counted dollars that
were wrong.

- **NET-A · NET-D · NET-F ↓ removed — vpc_endpoints.** An endpoint that was both nonprod-tagged
  and a duplicate was priced by both loops. The "keep two for different route tables" rationale
  is a gateway-endpoint concept and never applied to interface endpoints, so it also exempted
  the exactly-2 case. The counted dollar here had no test at all.
- **GL-4 ↓ removed — glue.** An unreadable DPU footprint fell back to AWS's 5-DPU default and
  counted $1,606/month on a quantity nobody measured.
- **DMS-4 ↓ removed — dms.** An unpriced rec was appended unchanged — counted in the headline,
  contributing $0, with "Rightsize for ~35% savings" prose nothing computed.
- **DMS-1 ↑ recovered — dms.** The Multi-AZ→Single-AZ lever is a pure config finding but was
  reachable only through the CloudWatch CPU path, so a dev instance at normal utilization was
  never seen. ~$204/mo per r5.large.
- **NET-C · NET-D ↓ removed — network.** Dev/test NATs counted a full base off a tag alone —
  and neither remediation the rec proposes recovers the full base. Multi-EIP counted under text
  reading "review if all are necessary".
- **AUR-C ↓ removed — aurora.** Every x86 family mapped to a Graviton2 target regardless of
  generation, so db.r7i → db.r6g — a backwards migration, priced against a cheaper class than
  the real target.
- **AUR-G ● correctness — reporter.** Demotion preserves the gross but the card printed only
  "$0.00 — advisory", hiding $418.20/mo with no indication anything was withheld.

## 7 `f869e64` — The under-count slice
The mirror of tranche 6: money the scanner was silently leaving on the table.

- **LAM-3 ↑ recovered — lambda.** A never-invoked Provisioned Concurrency allocation is 100%
  waste but resolved to $0, because a no-datapoints read was indistinguishable from a failed one.
- **OS-5 · OS-4 ↑ recovered — opensearch.** The Graviton map ended at m5/c5/r5, so every
  current-generation Intel family abstained. The downsize target stepped one rung and gave up —
  and OpenSearch families have gaps, so m5.12xlarge probed a nonexistent m5.8xlarge and
  abstained on the most expensive node in the domain.
- **OS-7 · OS-9 ↑ recovered — opensearch.** Master and UltraWarm tiers were missing from the
  idle-domain price. And a Reserved Instance covers instance hours — demoting the whole rec
  threw away the storage leg, which deleting the domain still frees.
- **MON-7 ↑ recovered — route53.** Every removable hosted zone was priced against the same
  starting count, so the tier ladder stood still: 26 zones with 3 removable came to $0.30 when
  the real saving is $1.10.

**The lessons file earning its keep.** Lesson C13 changed LAM-3 before a line of the gate was
written: the obvious metric is documented as emitted per version or alias, so a function-level
read of it would have reported "never invoked" for every function with PC — a fail-open across
whole accounts. The corroboration is Invocations at the canonical dimension instead. Lesson D5
then caught the tranche's only blocker.

## 8 `7cce9f5` — Ranking item 7 residue — the ranking closes
The last two CoH types with an adapter able to consume them.

- **SageMakerEndpoint · WorkSpaces ↑ recovered — orchestrator · sagemaker · workspaces.** Both
  are real ResourceTypes with no bucket, so AWS's computed dollars were dropped. All three
  wire-up layers — bucket, route, and a consuming adapter under a matching key — are now
  asserted by tests, because a break in any one is silent.
- **D4 ● correctness — workspaces.** The new CoH source bypassed the $0 gate that runs over
  local recs, so a payload with no savings figure would count as a recommendation while
  contributing nothing.

**Left unrouted on purpose.** MemoryDBCluster and DocumentDBCluster have no adapter and no tab.
Routing them would land AWS's dollars in a bucket nothing renders. A test pins the absence so
nobody "fixes" it with a route — the actual work is writing those adapters.

---

## Recurring classes, now written down
Five lessons added to the audit prompts — Recorded in `_LIVE_AUDIT_LESSONS.md`, which every
per-service audit prompt is pasted alongside — plus a new invariant sweep and a new
counted-lever checklist.

- **A6** — A dedup guard is only as good as its least-guarded producer. Grep every writer of the
  shared key set, not just the consumer you are editing.
- **C12** — A strict no-fallback mode on a cached lookup needs its own cache namespace, or an
  earlier lenient caller poisons it and the guard silently does nothing.
- **C13** — Know a metric's reporting criteria before writing an idle gate. Emitted-only-during-
  activity makes an empty series proof; emitted-continuously makes it a reason to abstain.
  Dimension sets matter for the same reason.
- **D5** — A new counted lever needs a render check, not just an adapter check. Four tranches
  running, the surviving defect was render-side.
- **D6** — A demoted rec's masked figure must reach the card, or the reader has no indication
  anything was withheld.

## What is left
~33 MEDIUM/LOW tail items — now mostly hygiene, coverage gaps and dead code, listed per adapter
in the sweep document. MemoryDB and DocumentDB adapters — net-new features, not fixes. Until
they exist, those two CoH types stay unrouted. Two new levers with evidence in hand — CloudWatch
Logs retention (unlike class migration, an in-place change and so genuinely countable) and
apigatewayv2 coverage.

## The open risk
Eight tranches have changed pricing, gating, demotion and rendering across most of the 34
adapters — all verified against fakes and live pricing APIs, and never once against a live scan.
The rewritten network_cost Cost Explorer query is the specific item fakes cannot settle: only a
real account proves the row shapes. It is the natural anchor for a broader "does a real scan
still reconcile" check, and it needs credentials.

| Tranche | Scope | Merge | Commits |
|---|---|---|---|
| 1 | Ranking 1–5 | 10f0b0c | 8 |
| 2 | Ranking 6, first half | f6c6c2e | 9 |
| 3 | Ranking 6 remainder, 7a | e9ccd4f | 8 |
| 4 | Ranking 7b, verification gaps | 1ad837d | 5 |
| 5 | Ranking 8, new levers | 37b0fb9 | 7 |
| 6 | Tail: wrong counted dollars | 8a7f7b0 | 7 |
| 7 | Tail: under-counts | f869e64 | 5 |
| 8 | Ranking 7 residue | 7cce9f5 | 2 |

Every tranche shipped test-first, gated on the full suite plus the regression and reporter
snapshots, and was adversarially self-reviewed before merge. Counts of commits are non-merge
commits touching services/, core/ or the reporters.

---

## SHA provenance (recorded Wave 0, observed live)

- **Ledger-claimed terminal SHA:** `7cce9f5` (Merge fix/sweep-priority-8) — EXISTS on main ✓
- **Observed HEAD at validation time:** `e608ceb` (docs(audits): tranche 9 fix status)
- **Drift (3 out-of-ledger-scope commits past the ledger's terminal):**
  - `4fd394b` feat(fsx): count idle file systems (FS-7)
  - `1c594c6` feat(bedrock): surface custom-model storage (BR-6)
  - `71cb80b` feat(msk): stop discarding serverless clusters (MSK-3)
  - `e608ceb` docs(audits): tranche 9 fix status — unscanned dominant costs
- **Ledger scope:** tranches 1–8 only. FS-7 / BR-6 / MSK-3 / tranche-9 are OUT OF SCOPE for this
  validation (they post-date the ledger). All finding verification runs against observed HEAD
  `e608ceb`, not the ledger's terminal `7cce9f5` — a finding "fixed by tranche N" must still be
  fixed at current HEAD.
