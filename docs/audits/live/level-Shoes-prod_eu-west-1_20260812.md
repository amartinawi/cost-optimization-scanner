# Output Audit — level-Shoes-prod / eu-west-1 / 2026-08-12

- **Report**: `level-Shoes-prod_eu-west-1.html` (account 019903302182), log
  `level-Shoes-prod_eu-west-1.log`, baseline preserved as
  `level-Shoes-prod_eu-west-1_baseline.html`
- **Headline**: $2,106.29/mo counted, 29 counted recs + 76 advisories,
  17 services with findings of 34 scanned
- **Account context**: **no commitments of any kind.** Verified by enumeration
  (lesson F6, written after the bnc miss), not inferred from one API:
  `ec2:describe_reserved_instances`, `rds:describe_reserved_db_instances`,
  `elasticache:describe_reserved_cache_nodes`, `redshift:describe_reserved_nodes`
  and `opensearch:describe_reserved_instances` all return NONE, **and** CE shows
  no `HeavyUsage`/`Reserved` usage type anywhere in the account. So the C6
  commitment sweeps are genuinely moot here. Role `MS-Expert` has working CE.
- **Method**: OUTPUT_AUDIT_PROTOCOL Layers 1–3. Cost Explorer usage types were
  again the decisive oracle — they carried both the largest finding and the
  refutation of a candidate one.

## Verified correct (audit passes)

| Check | Result |
|-------|--------|
| Layer 1 harness | **1 FAIL** — and the FAIL is **S16**, the sweep added hours earlier on the bnc audit, catching LS-1 on a fresh account unprompted |
| S1 headline reconciliation | $2,106.29 = sum of 11 non-zero tab totals, to the cent |
| `audit_upper_bounds.py` | no counted saving rests on an uncorroborated upper bound |
| **M360-3 (OpenSearch JVM gate)** | fired again — `levelshoes-es-prod` CPU 5% but peak JVMMemoryPressure **77%** vs the 38% a halved heap allows. Second account where it prevented a heap-bound downsize |
| **M360-1 (Aurora memory floor)** | fired on `levelshoes-db-reader` |
| **Aurora Graviton $785.48** | **EXACT.** db.r5.8xlarge $5.120/hr − db.r6g.8xlarge $4.582/hr = $0.538 × 730 = $392.74 × 2. Cluster `StorageType` is standard, and the scanner correctly used the Standard SKUs, not I/O-Optimized ($6.656/$5.957) — the M360 trap navigated |
| **ElastiCache Valkey $98.10** | **EXACT** against live Valkey SKUs (below) |
| **OpenSearch Graviton $81.76** | **EXACT.** c5.2xlarge.search $0.566 − c6g.2xlarge.search $0.510 = $0.056 × 730 × 2. Engine `Elasticsearch_7.9` meets the Graviton minimum, so the lever is legitimately available |
| RDS snapshot reconciliation | anchored to CE **to the cent**: `EU-Aurora:BackupUsage` $179.62 matches `actual_billed_backup_pool` exactly; rate $0.021/GB = 179.62/8,553.3 GB. Counted $136.88 sits under the share-scaled ceiling |
| OpenSearch storage $4.29 | 150 GB × 2 nodes × $0.013 × 1.1 — OS-2 (VolumeSize is PER NODE) handled; CE's actual gp2 rate $0.149/GB vs the scanner's $0.1485 is 0.3% |
| EC2 CoH $274.89 | AWS-supplied, and with no commitments there is nothing to demote against |
| ElastiCache memory gating | correct — peak `DatabaseMemoryUsagePercentage` 1.35–3.72%, zero evictions, on every counted downsize |
| EIP $3.65 | one unassociated EIP, exact |
| Best-lever dedup | correct: the r6g.xlarge clusters' Valkey lever ($66.87) is demoted behind their larger downsize lever ($167.17); the m5.large clusters' Graviton ($5.84) behind Valkey ($25.11) |

## Live rate verification (Layer 2a)

eu-west-1, all against the AWS Pricing API unless noted.

| Claim | Scanner | Live | Verdict |
|-------|---------|------|---------|
| db.r5.8xlarge (Aurora MySQL, Std) | $5.120/hr | $5.1200 | EXACT |
| db.r6g.8xlarge (Aurora MySQL, Std) | $4.582/hr | $4.5820 | EXACT |
| c5.2xlarge.search | $0.566/hr | $0.5660 (CE: $842.21/1,488 hr) | EXACT |
| c6g.2xlarge.search | $0.510/hr | $0.5100 | EXACT |
| cache.r6g.xlarge Redis | $0.458/hr | $0.4580 | EXACT |
| cache.r6g.xlarge **Valkey** | implied $0.3664 | $0.3664 | EXACT |
| cache.m6g.large Redis / Valkey | $0.164 / $0.1312 | same | EXACT |
| cache.m5.large Redis / Valkey | $0.172 / $0.1376 | same | EXACT |
| Aurora backup storage | $0.021/GB-mo | CE $179.62 / 8,553.3 GB | EXACT |
| EKS extended support | $0.50/cluster-hr | CE `EUC1-…:extendedSupport` $130.50/261 hr | EXACT (but see LS-1) |
| ElastiCache ext. support r6g.xlarge | *not read* | $0.366/hr (CE $544.61/1,488 hr) | **MISSING — LS-2** |
| ElastiCache ext. support m6g.large | *not read* | $0.131/hr (CE $194.93/1,488 hr) | **MISSING — LS-2** |

## Findings

### LS-1 — EKS extended support counted on a cluster AWS does not bill (C21) — CRITICAL

$365.00/mo — **17.3% of the headline** — counted against `levelshoes-prod` for
a surcharge that is not billed. This is the **BNC-1 defect on a second
account**, and the evidence here is cleaner than at bnc.

`levelshoes-prod` runs Kubernetes 1.33 (`EXTENDED_SUPPORT` since 2026-07-29)
with `upgradePolicy.supportType = **STANDARD**` — AWS auto-upgrades such a
cluster and documents that it incurs no extended-support charges.

Cost Explorer is unambiguous:

| Usage type | July | August 1–12 |
|------------|------|-------------|
| `EU-AmazonEKS-Hours:perCluster` | $148.80 (1,488 hr = 2 clusters) | $52.20 (522 hr = 2 clusters) |
| `EU-AmazonEKS-Hours:extendedSupport` | **absent** | **absent** |
| `EUC1-AmazonEKS-Hours:extendedSupport` | $36.00 (72 hr) | $130.50 (261 hr) |

There is **no eu-west-1 extended-support line at all**, while a *sibling
region* has one for a different cluster. That is stronger than bnc's 2-of-3
ratio: the usage type demonstrably exists on this account and would appear if
billed. It does not.

- **ALREADY FIXED** by `fix/eks-extended-support-policy-gate` (BNC-1). No new
  work; this report is independent confirmation on a second account, and
  **S16 flagged it automatically** — the sweep written a few hours earlier
  caught the class on data it had never seen.
- Predicted delta: **−$365.00** → eks_cost **$365.00 → $0.00**.

### LS-2 — the billed ElastiCache Extended Support surcharge is not reported at all (NEW) — HIGH

The account pays **$725.62/mo** of ElastiCache Extended Support and the report
shows **$0** of it. That is **34% of the entire headline**, and more than any
single counted item in it.

| CE usage type (July) | Amount | Hours | Rate |
|----------------------|--------|-------|------|
| `EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.xlarge` | $544.61 | 1,488 = 2 × 744 | $0.366/hr |
| `EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.m6g.large` | $194.93 | 1,488 = 2 × 744 | $0.131/hr |

Both rates reconcile exactly against the Pricing API, and the attribution is
unambiguous: the four nodes on Redis **5.0.6** are
`levelshoes-prod-cache-redis-001/002` (cache.r6g.xlarge) and
`levelshoes-prod-session-001/002` (cache.m6g.large) — exactly 2 × 744 hours per
node type. `ls-ms-redis-prod-001/002` run Redis **7.1.0** and carry **no**
surcharge line, so the charge maps onto precisely the EOL clusters.

`grep -rn extended_support services/adapters/` returned only `eks.py` and
`opensearch.py`. There was no ElastiCache check to be wrong — the lever simply
did not exist.

**The juxtaposition is the point.** The same report invents a $365/mo EKS
surcharge AWS is not billing while missing a $725.62/mo ElastiCache surcharge
it is. Both are the same question — *is AWS actually charging this?* — and the
answer to both is one CE usage-type read.

- **FIXED** on `fix/elasticache-extended-support`. Measured from CE, never
  inferred from engine-version numbers (inferring is the EKS defect twice
  over). REGION-scoped (OS-1: an account-wide read re-counts the surcharge in
  every scanned region) and fail-closed on an unset region or a CE error.
- **Attribution is better than OpenSearch's.** The ElastiCache usage type
  embeds the NODE TYPE, so no CE resource-level granularity is needed. The rec
  names the clusters on that node type, narrowed to those whose engine version
  is behind, so a current-version cluster sharing a node type is never
  implicated. A node type no live cluster reports is still counted — it is
  billed — with an empty cluster list rather than a guess.
- **No double count with the downsize lever.** The surcharge is summed rather
  than entered into the per-cluster best-lever dedup, because it is additive:
  the downsize/Graviton/Valkey levers price the `NodeUsage` leg only. Checked
  arithmetically — for a r6g.xlarge node, surcharge removal at current size
  ($0.366) plus the NodeUsage-only downsize ($0.458 − $0.229) equals exactly
  the both-actions saving from $0.824/hr to $0.229/hr.
- Predicted delta: **+$725.62** → elasticache **$432.44 → $1,158.06**.

### LS-3 — Data Lifecycle Manager images counted as unused AMIs (AFS-2 / C19) — MEDIUM

2 of the 3 counted AMI recs (**$3.34 of $3.61**) are EBS **Data Lifecycle
Manager** images. Confirmed by tag:

```
ami-0231524c54bcff2b8  DLM_policy-005c32eff971bbaee_i-05da5bbda9bad482b_03.17.2024…
  aws:dlm:lifecycle-policy-id     policy-005c32eff971bbaee
  aws:dlm:lifecycle-schedule-name Schedule 1
  dlm:managed                     true
```

AFS-2 established this exact class — an AMI whose lifecycle belongs to an AWS
service is never "referenced by a running instance" *by construction*, so the
gate can never be false, and deregistering it circumvents the plan that owns
it. That fix keyed on the `aws:backup:` namespace alone. DLM is the **other**
AWS-native AMI creator, and it will be flagged on 100% of accounts using DLM,
forever.

The dollars are small; the structure is not. This is C19 recurring because the
first fix enumerated one manager instead of the class of managers.

- **FIXED** on `fix/ami-dlm-managed`. Detection is now table-driven over the
  AWS services that create AMIs on a schedule, each keyed on its tag NAMESPACE
  with a name prefix as the copy-safe fallback. The rec names the **right**
  manager and lever, so an operator holding a DLM image is pointed at the DLM
  policy's retention rule rather than at a nonexistent AWS Backup plan.
- The third AMI, `pritunl-vpn-server-latest` (917 days, untagged), is a
  genuinely unused image and still counts.
- Predicted delta: **−$3.34** → ami **$3.61 → $0.27**.

### LS-4 — the scanner probes ElastiCache node sizes that do not exist (NEW) — LOW

The log's most alarming line is a non-event:

```
pricing fallback [eu-west-1] Pricing API returned no result for
AmazonElastiCache cache.m6g.medium in eu-west-1 → $0.000000
⚠️ Warning: Live pricing unavailable — used fallback rate: …
```

`cache.m6g.medium` **does not exist** — every ElastiCache family except the
burstable t-series starts at `large` (confirmed against the Pricing API's
`instanceType` attribute values). The downsize lever walked one rung below the
floor and asked for a nonexistent SKU.

**The lever behaved correctly**: the $0 rate did not produce a phantom; both
`cache.m6g.large` clusters abstained to `$0.00 — advisory`. But the operator is
shown a "Live pricing unavailable" warning that reads like a pricing outage,
and an API call is spent to learn nothing. OpenSearch already carries this
guard (`_LARGE_FLOOR_FAMILIES`); ElastiCache did not.

- **FIXED** on `fix/elasticache-size-floor`. Also corrects an existing
  parametrized test that pinned `cache.r6g.large → cache.r6g.medium` as the
  expected target — the same nonexistent-SKU expectation the OpenSearch ladder
  had with `c5.medium.search`.
- Predicted delta: **$0.00** (warning removed, no counted dollar changes).

## Refuted during Layer 3 (recorded so they are not re-raised)

**1. The Valkey lever is a flat 20% of node price (C4/C9 shape).** Every Valkey
saving is exactly 20.0% of the node cost ($119.72→$23.94, $125.56→$25.11,
$334.34→$66.87), the classic fabricated-fraction signature, and the Valkey recs
carry **no `AuditBasis`** while every other ElastiCache lever does.
**Refuted on live prices**: `Valkey` is a real `cacheEngine` in the Pricing API,
and AWS's published Valkey rate is exactly 80% of Redis for every node type
checked — cache.m6g.large 0.1640→0.1312, cache.m5.large 0.1720→0.1376,
cache.r6g.xlarge 0.4580→0.3664. The constant reproduces AWS's actual published
differential to the cent; it is not a fabricated fraction. *(The missing
`AuditBasis` remains a disclosure gap — noted below, not a dollar finding.)*

**2. The `$0.000000` pricing fallback inflates a downsize.** A $0 target price
would make `current − target` the full current cost. **Refuted**: the adapter
abstained — both affected clusters render `$0.00 — advisory`, and the counted
ElastiCache dollars come from clusters with fully-priced targets. The $0 rate
never reached a counted figure. (The wasted probe became LS-4.)

**3. Aurora Graviton priced off the wrong storage SKU.** eu-west-1 exposes both
Standard and I/O-Optimized Aurora SKUs, and the M360 audit showed the choice
matters ($5.120/$4.582 vs $6.656/$5.957 — a $117.53/mo difference per
instance). **Refuted**: `describe_db_clusters` reports no I/O-Optimized storage
type for `levelshoes-prod`, and the scanner used the Standard pair. Correct.

**4. RDS snapshot savings could exceed the billed backup pool (C11).**
`billed_pool_share` is **0.999693** — 6,518 of 6,520 GB of footprint flagged,
which looks like claiming the entire pool. **Refuted**: the ceiling is anchored
to CE's real `EU-Aurora:BackupUsage` $179.62 and the counted $136.88 sits under
it; CE reports 8,553 GB billed against a 6,520 GB describe-derived footprint
(the difference being automated/continuous backup), so the denominator is
conservative in the safe direction.

**5. BNC-3 (OpenSearch dedicated-master tier) applies here.** It does not:
`levelshoes-es-prod` has `DedicatedMasterEnabled = False` and 2 data nodes, and
CE's 1,488 `EU-ESInstance:c5.2xlarge` hours confirm exactly 2 billed nodes. The
fix contributes **$0.00** on this account — recorded so the branch is not
credited with a delta it does not produce here.

**6. AMI and ECR recs render without a resource name (D5).** Three recs showed
`None` for the id in my first extraction. **Refuted** — my dump script's key
list was incomplete; the recs carry `ImageId`/`RepositoryName` and render. Same
false alarm as the bnc Route53 card; the extraction helper, not the data.

## Open / not fixed

- **LS-5 (LOW, not fixed)** — the ElastiCache **Valkey** recs carry no
  `AuditBasis`, while the Underutilized and Graviton levers on the same tab
  both publish two named prices. The dollar is correct (refutation 1), but a
  counted figure should be defensible from the report alone. Deriving it from
  the live `cacheEngine=Valkey` SKU instead of the 20% constant would also make
  it self-maintaining if AWS ever varies the differential by region or family.
- **eu-central-1 EKS extended support** — CE shows `EUC1-…:extendedSupport` at
  $130.50 over 11 days (~$365/mo) on a cluster outside this report's region.
  Correctly absent from an eu-west-1 report, but it is real money this account
  is paying, and a scan of eu-central-1 would find it.

## Coverage gaps (savings ABSENT from the $2,106.29)

- **Athena workgroup `primary`** — `PublishCloudWatchMetricsEnabled` off, so
  scan spend is unmeasurable (ATH-6 behaving correctly: one warning, no
  fabricated card).
- **21 services returned nothing at all**, including `bedrock`, `s3`,
  `lambda`, `monitoring` and `cloudfront`. On an account whose RDS bill alone
  is $7,618/mo this is worth an explicit spot-check rather than reading as a
  clean estate.
- **2 stale Cost Hub volume-delete recs dropped** — both for `in-use` volumes;
  the E-series guard working.

## Status

| ID | Class | Severity | Claim | Status |
|----|-------|----------|-------|--------|
| LS-1 | C21 | CRITICAL | $365.00 counted for an EKS surcharge AWS does not bill (STANDARD upgrade policy); CE shows no eu-west-1 extended-support line at all | **ALREADY FIXED** by `fix/eks-extended-support-policy-gate`; **auto-detected by S16** |
| LS-2 | NEW | HIGH | $725.62/mo of billed ElastiCache Extended Support (34% of headline) entirely unreported | **FIXED** — `fix/elasticache-extended-support`, predicted +$725.62 |
| LS-3 | AFS-2 / C19 | MEDIUM | DLM-managed AMIs counted as unused; gate can never be false for them | **FIXED** — `fix/ami-dlm-managed`, predicted −$3.34 |
| LS-4 | NEW | LOW | Downsize lever probes nonexistent `cache.m6g.medium`, printing a false pricing-outage warning | **FIXED** — `fix/elasticache-size-floor`, predicted $0.00 |
| LS-5 | — | LOW | Valkey recs carry no `AuditBasis` | OPEN — dollar verified exact, disclosure only |
| — | — | — | Valkey flat 20% factor | REFUTED — matches AWS's real published Valkey rate exactly on all 3 node types |
| — | — | — | `$0.000000` fallback inflating a downsize | REFUTED — lever abstained; no counted dollar touched |
| — | — | — | Aurora Graviton on the wrong storage SKU | REFUTED — cluster is standard storage, Standard SKUs used |
| — | — | — | RDS snapshots at 99.97% of the pool (C11) | REFUTED — CE-anchored and conservative |
| — | — | — | BNC-3 master tier applies here | REFUTED — no dedicated master; $0 delta |

**Predicted headline: $2,106.29 → $2,463.57** (−$365.00 + $725.62 − $3.34).
This is the first audited report whose headline goes **UP**: the largest single
error was a *missing* real charge, not a phantom one.

## What this round says about the harness

- **S16 paid for itself within hours.** Written on the bnc audit, it flagged
  LS-1 on a different account, a different region and a report it had never
  seen — no human hypothesis required.
- **Lesson F6 changed the method and it mattered.** The commitment context here
  was established by enumerating all five RI surfaces plus a CE `HeavyUsage`
  cross-check, rather than inferring from one call. The answer happened to be
  "none", but it is now an evidenced none.
- **The two extended-support defects are one lesson (C24, added).** A billed
  surcharge is a real, removable, account-specific cost, and the ONLY way to
  know whether AWS is charging it is to read the usage type. Inferring from a
  version number invents charges; not looking at all misses them. This report
  did both at once, in opposite directions, ~$1,090 apart.

## Reconciliation

**PENDING** — awaiting operator re-scan of `level-Shoes-prod eu-west-1` with
all four branches applied. The protocol requires the headline to move by
exactly +$357.28 to **$2,463.57**, with eks_cost $0.00, elasticache $1,158.06,
ami $0.27, and **every other tab unchanged to the cent**.

Non-dollar assertions for the same re-scan:

- the `cache.m6g.medium` pricing-fallback warning is **gone**;
- the ElastiCache tab shows 2 new `ElastiCache Extended Support` cards naming
  the r6g.xlarge and m6g.large node types and their 5.0.6 clusters, and never
  naming `ls-ms-redis-prod-*`;
- the two DLM AMI cards render as `$0.00 — advisory` citing the **DLM policy's
  retention rule**, not an AWS Backup plan;
- Layer 1 returns **0 FAIL** (S16 silent).
