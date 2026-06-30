# Changelog

All notable changes to the AWS Cost Optimization Scanner project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (third deep-audit remediation — ap-south-1 + ap-southeast-1, account bnc)
Two regions of a new account (784852663902) were deep-audited (adversarially
verified): ap-south-1 (15 confirmed) and ap-southeast-1 (5 confirmed). Every
headline reconciled and all big-ticket rates were live-verified to the cent; the
defects were in error visibility, a self-introduced regression, and missed
savings. Nine fixes under the same invariant (counted dollars are
account-specific and defensible; advisories are `$0` `Counted=False`, rendered
but never summed; double-counting is the cardinal sin).

- **Advisory-only services lost their HTML tab (regression).** The prior
  count-semantics change made the reporter tab gate key off the counted-only
  `total_recommendations`, so a service whose cards are all `$0` advisories
  (S3's 138 bucket cards, `commitment_analysis` RI/SP advisories, redshift,
  athena, network_cost) rendered **no tab at all**. The gate now keys off a new
  `total_rendered` (counted + advisory cards); the headline count stays
  counted-only. `total_services_scanned` was realigned to the same
  rendered-aware count so it matches the rendered tabs.
- **NAT Gateway Cost Optimization Hub savings were silently dropped.** CoH
  computed per-NAT idle savings, but a 3-layer gap (`network` absent from
  `_HUB_SERVICES`, no `NatGateway` entry in the orchestrator `type_map`, and the
  network adapter consumed no CoH) discarded them — self-reported as a
  "dropped type" warning. The network adapter now consumes
  `cost_hub_splits["network"]` and de-duplicates the local VPC-scoped
  consolidation levers CoH supersedes (CoH > heuristic, demotion by VPC via the
  NAT→VPC topology the shim now exposes); when CoH covers only some of a VPC's
  NATs this under-counts rather than overstates (the safe direction).
- **RDS snapshot reconciliation capped the string but not the numeric.**
  `reconcile_snapshot_savings` rewrote the `EstimatedSavings` string to the
  Cost-Explorer-capped value but left `EstimatedMonthlySavings` at the uncapped
  upper bound (confirmed +$719.60 overstatement in the field). The numeric is
  now capped in lockstep; the no-CE-data advisory-demote branch also zeroes the
  numeric (`EstimatedMonthlySavings=0.0`, upper bound moved to
  `PotentialMonthlySavings`) so a demoted advisory cannot leak into a numeric sum.
- **EIP fallback region-scaled a globally flat rate.** Public IPv4 / EIP is a
  flat $3.65/mo in every commercial region, but the `pricing_engine=None`
  fallback multiplied it by `pricing_multiplier` (in both `core/pricing_engine`
  and `services/elastic_ip`). The flat constant is now used directly. The counted
  EIP recs (unassociated / on-stopped-instance / multiple-per-instance) also gained
  the numeric `EstimatedMonthlySavings` field they were missing.
- **Athena advisory string not `$0`-formatted.** A metric-gapped athena rec was
  demoted to `Counted=False`/`EMV=0` but kept the bare "Up to 75% scan-cost
  reduction" string. All three branches (measured / no-data / fast-mode) now set
  a string that agrees with the rec's counted state.
- **Silent error-swallowing classified (observability).** Eight previously-bare
  `except: pass` / `return` sites that masked an `AccessDenied`/throttle as
  "no resources" now route through `record_aws_error`: bedrock (PT / knowledge-base
  / agent enumeration), sagemaker (endpoint / notebook / training-job enumeration),
  step_functions (per-machine CloudWatch read), and s3 (`GetBucketWebsite`). The
  normal fallback paths (paginator-unavailable, `NoSuchWebsiteConfiguration`,
  no-datapoints) stay silent.

### Fixed (second deep-audit remediation — eu-west-1 / level-Shoes-prod re-scan)
A re-scan of the remediated account (019903302182, 90 → counted-only ~30 recs)
was deep-audited again (43 agents, adversarially verified). The prior batch was
confirmed active (pricing-fallback now surfaces in `scan_warnings`; ElastiCache
`cache.m6g.medium` $0 fallback produces no counted dollar; pricing accurate to
the cent on Aurora/EC2/RDS/OpenSearch/ElastiCache). Seventeen new findings
collapsed to six distinct defects, all fixed under the same invariant — a
rendered dollar must be **counted, account-specific, and defensible**, advisories
are `$0` `Counted=False` (rendered, never summed), and `counted == rendered` at
the per-rec field level.

- **EBS snapshots — advisory + accurate sizing (fidelity).** The `ebs_snapshots`
  cards rendered a non-zero `$/month (max estimate)` outside the headline total,
  carried no `Counted` flag, and sized on the provisioned `VolumeSize` (~2×
  overstatement). They are now explicit `$0` `Counted=False` advisories
  (`EstimatedMonthlySavings=0.0`, recoverable figure in `PotentialMonthlySavings`),
  sized on **`FullSnapshotSizeInBytes`** (the AMI fix, ported) with a `VolumeSize`
  fallback and a no-fabrication guard. The synthetic Snapshots tab now shows the
  potential as an explicit advisory (`not in counted total`) and no longer feeds a
  dollar into the savings-sorted tab order.
- **`total_recommendations` now means counted opportunities everywhere.** The
  headline counted `$0` advisories for some adapters but not others ("90"
  opportunities where only ~30 carried a counted dollar). `ScanResultBuilder` and
  the reporter now count only `Counted != False` recs uniformly (the S3
  convention, centralised) — advisories still render as cards but never inflate the
  count. A `count` placeholder with no materialised recs is still trusted.
- **Lambda Cost Hub card rendered a placeholder (counted != rendered).** The CoH
  rec arrived with only AWS's camelCase `estimatedMonthlySavings`, so the card
  showed the literal `"Cost optimization"` while `$7.51/mo` was silently summed.
  Lambda now normalises CoH recs to a PascalCase `EstimatedSavings` string +
  `Counted=True` (as EBS/EC2 already do); the camelCase key stays the source of
  truth for the sum (no double count).
- **OpenSearch advisory field consistency.** A demoted idle-domain advisory kept
  its pre-demotion `EstimatedMonthlySavings` (e.g. `846.49`) while its string read
  `$0.00 — advisory`. The advisory loop now zeroes the numeric and preserves the
  figure as `PotentialMonthlySavings` (mirrors ElastiCache).
- **RDS snapshot recs carry a numeric dollar.** The Old-RDS / Old-Aurora-Cluster
  snapshot recs were counted via `parse_dollar_savings` on the string but exposed
  no numeric `EstimatedMonthlySavings` (a JSON consumer summing the field got
  `$0`). The numeric is now set alongside the string (the sum still flows through
  the string, so no double count).
- **AMI snapshot dollar rounded once.** The card showed `$2.50` while the counted
  numeric was `$2.4987`; the monthly figure is now rounded to the cent before it
  feeds both the string and the numeric (`counted == rendered`).

### Fixed (deep-audit remediation — eu-central-1 / Jarir-M2 + eu-west-1 / level-Shoes-prod)
Two adversarially-verified deep audits of live multi-account scans (account
071985014680 / 315 recs and 019903302182 / 92 recs) surfaced a fresh class of
dollar-fidelity defects that mocked tests did not. Same through-line: a counted
dollar must be account-specific and defensible, `counted == rendered` at the
per-rec level, double-counting is the cardinal sin, and anything speculative is a
`$0` `Counted=False` advisory (rendered, never summed). All fixes verified against
the scans' JSON + live regional pricing; the regression + reporter snapshots are
unmoved.

- **Aurora I/O-tier sized on phantom storage (fidelity).** `_check_io_tier` read
  `AllocatedStorage`, which `describe_db_clusters` always reports as `1` for Aurora
  (storage is auto-managed) — so the I/O-tier saving was computed against ~1 GB.
  Now uses the CloudWatch `VolumeBytesUsed` average (bytes/1e9 = GB) for real
  storage, prices the I/O rate from a new **live** `PricingEngine.get_aurora_io_rate_per_million()`
  (us-east-1 $0.20/M, eu rates ~$0.22/M; region-scaled fallback), skips cleanly when
  storage is unmeasurable, and short-circuits `aurora-iopt1` clusters (already I/O-optimized).
- **Aurora Graviton card misrepresented the deployed class (counted == rendered).**
  When a reader was also a rightsizing candidate, the Graviton rec showed
  `CurrentSize` = the hypothetical *post-rightsize* class (e.g. `db.r5.2xlarge`) as if
  deployed, while the instance was actually `db.r5.8xlarge`. The dollar is correctly
  priced on the rightsized class (so it never overlaps the rightsizing rec); that
  basis is now disclosed via a new `PricingBasisSize` field and the card shows the
  real deployed class.
- **OpenSearch counted ≠ rendered + irreversible-delete safety.** Synced each rec's
  `EstimatedSavings` string to its counted dollar (no more headline/card divergence);
  gated idle-domain full-cost DELETE recs on a CloudWatch corroboration flag
  (`SearchRate`/`IndexingRate` < 1) — uncorroborated idle domains demote to `$0`
  advisory rather than recommending an irreversible deletion; and suppressed the
  storage-lever dollar on a domain already counted as an idle-delete (double-count).
- **EC2 CO/CoH ASG dedup gap (double-count).** Cost Optimization Hub surfaces ASG
  recs under the **ASG name** while Compute Optimizer surfaces the same capacity under
  the bare **instance id**, so an ASG could be counted twice. CO recs are now deduped
  against both the instance id and the ASG-name tag, and `UNDER_PROVISIONED` /
  non-running CO recs (which carry no cost saving) are dropped from the count.
- **Route53 / API Gateway rate over-scaling (fidelity).** Route53 hosted-zone pricing
  is globally flat ($0.50/zone) and the API Gateway REST request rate is already
  regional — both were being multiplied by `ctx.pricing_multiplier` a second time,
  inflating non-us regions. Removed the extra multiply; both now satisfy
  `counted == rendered`, and Route53 recs carry explicit `Counted` flags.
- **ElastiCache EMV hygiene.** The advisory branch of the EstimatedSavings sync now
  zeros `EstimatedMonthlySavings` (keeping the figure in `PotentialMonthlySavings`),
  and the enhanced-checks card renderer sums the group's `Counted`-bearing EMV instead
  of echoing `clusters[0]`'s string.
- **Public-IP "should be private" is advisory, not counted.**
  `services/elastic_ip.py`'s `public_ips_should_be_private` on a *running* instance is
  now a `$0` `Counted=False` advisory: the public IPv4 is billed, but the saving is
  realizable only if the IP can be removed (NAT/VPN/bastion legitimately need it).
  Unassociated/stopped-instance EIP releases remain definite counted savings.
- **AMI size never fabricated (fidelity).** When `describe_snapshots` fails, the
  backing size falls back to the AMI block-device-mapping `VolumeSize` and the rec is
  flagged `SizeEstimated` with an `AuditBasis` disclosure; the old magic `8` GB default
  is gone — with no defensible size the AMI is skipped rather than emitting a guessed
  dollar.
- **RDS advisory count hygiene.** Reserved-Instance and backup-retention advisories
  (`Counted=False`) are excluded from the adapter's `total_recommendations`, aligning
  RDS with the S3 convention (`counted == rendered` headline).
- **Scan diagnostics surfaced in the report.** `scan_warnings` + `permission_issues`
  were serialized to the JSON but never rendered; the HTML executive summary now
  includes a collapsible diagnostics disclosure. Removed the dead
  `self.scan_warnings`/`self.permission_issues` fields from `cost_optimizer.py`
  (warnings live on `ctx._warnings`).
- **Pricing fallbacks are no longer silent.** When a live Pricing-API lookup fails,
  `PricingEngine` substitutes a region-scaled constant; those events stayed on the
  engine's own `warnings` list and never reached the report. The orchestrator now
  drains them (deduplicated with an occurrence count, across region siblings) into
  `ctx` scan warnings via a new `PricingEngine.drain_warnings()`, so the diagnostics
  disclose exactly which rates were estimated rather than fetched live.

### Fixed (first real-scan audit — M360 / ap-south-1)
The first live multi-account scan surfaced a class of issues that static review +
mocked unit tests structurally could not: runtime AWS API errors, dead code that
emits no finding, and report noise at real scale. All adversarially verified
against the scan's JSON + live ap-south-1 pricing.

- **CloudFront dead-code storm (175 of 181 warnings).** `services/cloudfront.py`
  fetched `CacheHitRate`/`Requests` per distribution to compute a value that was
  then discarded (`_ = (...)`); the `Requests` call used `Period=60` over 7 days
  (10080 datapoints > the 1440 limit), raising `InvalidParameterCombination` on
  every distribution. The dead block is deleted (175 fewer warnings + ~350 fewer
  API calls). The surviving price-class lever now reads CloudWatch from
  **us-east-1**, where CloudFront publishes its metrics.
- **30-day forecast overstated ~2×.** `core/trend_analysis.py` called
  `get_cost_forecast(Granularity="MONTHLY")` over a window straddling two calendar
  months, so Cost Explorer summed two full-month buckets (a "$57k 30-day forecast"
  against a ~$27k/mo run rate). Switched to `Granularity="DAILY"`.
- **QuickSight non-functional against real AWS.** `describe_spice_capacity` is not
  a boto3 operation (no read API for SPICE capacity exists) — it raised
  `AttributeError` every scan. Guarded with `hasattr` (skips cleanly in prod;
  unit-tested via the fake client). QuickSight identity ops (`ListUsers`) now
  route to the **us-east-1 identity endpoint** via `ClientRegistry._GLOBAL_SERVICES`.
- **DMS terminate safety (fidelity).** "Unused instance" terminate recs were gated
  on CPU < 5% alone; a low-CPU instance running a CDC task is in-use, and
  recommending its termination is dangerous. Now gated on `describe_replication_tasks`
  — any attached task (or an unreadable task state) demotes the rec to a $0 advisory
  instead of a counted full-cost terminate. Also removed the dead
  `describe_replication_configs` paginator block (it crashed on a non-existent
  paginator while discarding its result).
- **RDS upper-bound snapshots demoted (fidelity).** Snapshot savings that could not
  be validated against a Cost Explorer backup actual were counted at the
  provisioned-size **upper bound** (overstatement). They are now `Counted=False`
  advisories — rendered with the upper-bound figure but excluded from the headline.
- **S3 count-flag consistency.** $0 bucket-analysis cards used a bespoke
  `Advisory=True` the reporter's count logic ignored (showing 336 advisory cards as
  "counted"). They now carry the standard `Counted=False`; savings-bearing buckets
  carry `Counted=True`.
- **Aurora card render parity.** Rightsizing/Graviton/IO-tier recs now carry the
  numeric `EstimatedMonthlySavings` + `Counted` (not only `monthly_savings`), so a
  multi-rec Aurora group's card sums correctly instead of showing the first rec only.
- **Log hygiene.** `core/trend_analysis.py`'s 27 `print("🔍 …")` debug lines →
  `logging`; MediaStore's unreachable-endpoint error in unsupported regions →
  quiet debug skip; Savings Plans `DataUnavailableException` (no active plans) →
  informational note, not an error warning; `fastest_growing` now requires a
  ≥ $1 prior-month base (kills 52,114%-growth noise on sub-penny services).

### Fixed (cost-fidelity LOW remediation — `docs/audits/LOW_REMEDIATION_PROMPT.md`)
Closes the LOW backlog (the ~100 REPRODUCES findings of the 34-adapter audit),
completing the cost-fidelity sweep after CRITICAL and HIGH. The through-line is
unchanged — a rendered dollar must be **counted, account-specific, and
defensible**; anything speculative is a `$0` `Counted=False` advisory (rendered,
never summed); AWS errors are classified via `services/_aws_errors.record_aws_error`,
never swallowed. Every finding was re-verified read-only against the integrated
tree (a per-service verification pass); the static regression and reporter
snapshots are unmoved.

- **Service-local fixes (24 services).** Per-service savings grounding, dedup
  keys, count hygiene, pagination, and error classification — e.g. api_gateway
  drops four never-rendered dead descriptions; apprunner stops pricing a missing
  Memory config at a fabricated 2 GB; athena/containers paginate
  `list_work_groups`/`describe_repositories`; aurora/dms/ec2/rds/redshift/
  dynamodb/eks/commitment_analysis/monitoring/network/s3/lambda/step_functions/
  sagemaker/mediastore/glue/ebs/file_systems/lightsail/transfer each land their
  verified items.
- **Error classification.** ec2 `get_auto_scaling_checks` and containers ECR
  `list_images` failures now route through `record_aws_error` (AccessDenied →
  `permission_issue`) instead of a bare warning (NET-04, containers L3).
- **Render correctness.** SageMaker/Bedrock recs match their snake_case keys in
  the generic renderer (right card header + resource id, no raw-property dump);
  ECR recs land in the correctly-named group; network_cost transfer recs carry
  the honest "Audit Based" badge; the S3 enhanced-check total rejects non-monthly
  unit strings via `parse_dollar_savings`.
- **Dead code / dead wiring.** Removed the unreachable `backup`/`route53`
  Phase-A descriptors, the unreachable `_extract_file_systems_resources`
  extractor, and the unconsumed `s3` Cost Optimization Hub bucket
  (`cost_hub_splits["s3"]` was populated but read by no adapter).
- **Adapter precision.** dynamodb suppresses rightsizing on empty tables (a
  deletion candidate); file_systems sums the full-precision `_savings` float and
  paginates `describe_file_caches`; sagemaker's Active-Endpoints stat excludes
  idle endpoints; ebs CO/CoH recs carry an `AuditBasis`; transfer declares its
  CloudWatch contract; workspaces flags non-Windows bundle-rightsizing advisories;
  quicksight (L3) now surfaces sub-50%-idle SPICE as a `$0` `Counted=False`
  advisory (the potential figure is shown but never summed, and it is excluded
  from the rec-count headline) instead of being dropped — partial SPICE headroom
  is needed for dataset refreshes, so it has no concrete reclaimable dollar.
- **Documentation.** Rewrote `services/adapters/CLAUDE.md` Pricing Models into an
  accurate six-category taxonomy covering all 34 adapters (the prior table
  documented 26 and miscounted), and corrected per-adapter pricing-strategy rows.

### Fixed (cost-fidelity HIGH remediation — `docs/audits/UNIFIED_AUDIT_FINDINGS.md` / `HIGH_REMEDIATION_PROMPT.md`)
Closes the 83 actionable HIGH findings across the 34-adapter audit. Same
through-line as the CRITICAL batch: a rendered dollar must be **counted,
account-specific, and defensible** — a blanket factor with no per-resource usage
evidence becomes a `$0` `Counted=False` advisory (rendered, never summed), a
wrong-SKU/region rate is pinned to the live AWS Pricing API, and a saving counted
by one lever is never re-counted by an overlapping lever or by Cost Hub. Each
remediated service carries a `tests/test_<svc>_high_fixes.py`; the static
regression and reporter snapshots are unmoved (adapter logic changes do not touch
the golden fixtures). Landed:

- **Cluster A — silent failures classified (13 findings).** Every swallowed
  `except: pass` / `logger`-only AWS error now routes through
  `services/_aws_errors.record_aws_error` (AccessDenied/Unauthorized/OptInRequired
  → `ctx.permission_issue`, else `ctx.warn`), so an IAM-gapped API surfaces as a
  permission issue instead of a clean-looking empty tab — and a *failed* metric
  read marks the rec `Counted=False` rather than fabricating a `$0`. Services:
  api_gateway (H1/H2), aurora (H4), batch (H2/H3), cloudfront (H1), containers
  (H2, ECS CO helper), ec2 (H1, ASG-member dedup degradation now visible +
  partial set returned), monitoring (H1), network_cost (H3), quicksight (H1/H2 —
  a ListUsers gap no longer silently zeroes the SPICE check), workspaces (C1).
  New `tests/test_cluster_a_silent_failures.py`.
- **SR-1 — Phase-A card dollar single-sourced (`reporter_phase_a.py`).** The
  descriptor-driven `render_grouped_by_category` now renders a category group's
  *counted* `EstimatedMonthlySavings` sum (advisory `Counted=False` recs excluded)
  instead of the first rec's free-text `EstimatedSavings` percentage / hardcoded
  "$316/month" string; an advisory-only group renders "$0.00/month — advisory".
  Repairs API Gateway H3, Glue H1, Step Functions H2. New
  `tests/test_reporter_sr1_counted_rendered.py`.
- **SR-2 — Phase-B advisory vs counted (`reporter_phase_b.py`).** Grouped/per-rec
  renderers now sum only counted recs into card totals and label `Counted=False`
  recs "advisory — not added to the tab total": `_render_opensearch_enhanced_checks`
  (OpenSearch H2), `_render_eks_source` (EKS H3), and `_render_generic_other_rec`
  (MSK H2, Redshift H3, Commitment H1 purchase/SP-coverage advisories) — which
  also stops dumping internal `EstimatedMonthlySavings`/`Counted` as raw property
  rows and single-sources the per-rec dollar. New
  `tests/test_reporter_sr2_advisory_counted.py`. Reporter snapshot move: **only
  `ami`** (removes a redundant raw "Estimatedmonthlysavings: 5.0" line; the
  displayed `$5.00/month` and the headline are unchanged).
- **Pricing engine — load-bearing rates pinned (live-validated, us-east-1, AWS
  Pricing API 2026-06).** `core/pricing_engine.py`: new
  `get_aurora_io_storage_premium_per_gb()` (`IO-Optimized − Standard` ≈
  `$0.125/GB-Mo`, was a `0.025` constant ~5× low — aurora H1) and
  `get_aurora_io_instance_premium_monthly()` (~30% instance premium — aurora H2);
  `_fetch_msk_broker_price` now filters on real attributes (`computeFamily` +
  `productFamily='…(MSK)'` + `operation=RunBroker`) so the live broker-price path
  is no longer 100% dead, markup recalibrated `1.4 → 2.19×` (msk H1); new
  `get_dms_instance_monthly_price()` pins the `InstanceUsg:dms.<type>` vs
  `Multi-AZUsg:dms.<type>` usagetype so the SKU is deterministic (dms H1/H2).
  New `tests/test_pricing_engine_high_fixes.py`.
- **Cluster B/C — fabricated config-dimension dollars & reduction factors → exact
  delta or `$0` advisory.**
  - *ec2 H2.* The four tag-heuristic levers (cron `0.85`, batch `0.75`,
    instance-store `0.15`, non-prod `0.64`) no longer count a blanket-factor
    dollar from Name/Environment tags alone. `get_advanced_ec2_checks` gates each
    on a `corroborated_ids` set the adapter derives from the CloudWatch
    idle/low-CPU rightsizing findings; an uncorroborated lever is a `$0`
    `Counted=False` advisory that still renders (speculative figure preserved in
    `AdvisoryEstimate`) but is excluded from `best_by_instance` and the headline.
    Spot migration (live on-demand−Spot delta) is unaffected.
  - *network_cost H1/H2.* The adapter sees only **blended Cost Explorer dollars**
    (no per-flow GB / co-location / topology), so the cross-region `0.30`,
    cross-AZ `0.50`, egress `0.40` reduction factors and the circular TGW branch
    (which re-derived GB from dollars already scored cross-region/cross-AZ — a
    ~20% double-count) were all unbacked. Every transfer/TGW rec is now a `$0`
    `Counted=False` advisory — measured spend and the lever are shown, no
    fabricated dollar enters the total.
  - *Per-service deltas (workflow batch):* elasticache H2 (Graviton `0.20` →
    exact `(x86 − Graviton)` node-price delta), opensearch H3 (gp2→gp3 flat-20% →
    `EBS_GB × (gp2−gp3)` rate delta), dynamodb H1 (over-provisioned blanket-factor
    → CW low-utilization-gated `current − rightsized`, else advisory), monitoring
    H2/H3 (never-expiring-logs & 50%-removable-metrics → `$0` advisory absent a
    measured staleness signal), redshift H2, lightsail H1/H2/H3 (synthetic ×2
    bundle ladder → live per-bundle list prices, OS-aware, unknown bundle →
    `Counted=False`), transfer H2 (`(n−1)` protocol removal gated on per-protocol
    usage evidence), workspaces C2/C3 (AutoStop CW-gated; real `ComputeType`
    propagated so a GRAPHICSPRO is not priced as STANDARD).
- **Cluster D — overlapping/cross-adapter double-counts deduped.** containers H1
  (ECS CO vs CoH), ebs H1 (unattached delete vs gp2→gp3/IOPS migration on the same
  volume), eks H1 (CoH vs cluster heuristic), aurora H3 + rds H1 (an Aurora member
  counted in both the RDS and Aurora tabs — single owner established), dynamodb H2
  (Reserved Capacity demoted, never summed with the rightsizing lever), redshift H1
  (RI demoted to advisory — `commitment_analysis` owns RI dollars), sagemaker H2
  (idle endpoint excluded from its consolidation group).
- **Cluster E/F/G/H/I — rate/region, count-hygiene, fail-safe deletion, coverage,
  fast-mode.** glue H2 (READY dev endpoint priced from its own DPU footprint),
  mediastore H1 (no-storage `$0` rec excluded from `total_recommendations`),
  sagemaker H1 (one-time spot-training cost no longer summed into the monthly
  headline), ami H3 + eks H2 + transfer H1 (delete candidates fail safe — Fleet/
  Spot-Fleet/shared-AMI references, Karpenter 0-nodegroup clusters, and STOPPED
  servers are corroborated or demoted to advisory before a destructive rec is
  counted), dynamodb H3 + elasticache H1 (GSI throughput summed; `NumNodes`
  threaded so multi-node clusters price on the real node count).
- **Pre-commit adversarial review hardening.** A 14-agent diff review of the
  batch (one reviewer per service group + a doc-fidelity and a dead-code sweep,
  each finding adversarially verified) surfaced two real cost-fidelity defects
  that the green test suite missed, now fixed:
  - *Node pricing zeroed outside us-east-1.* `PricingEngine._select_instance_node_hourly`
    front-anchored the node usagetype (`startswith("Node:")` / exact
    `"NodeUsage:<type>"`), but the AWS Pricing API region-prefixes usagetypes
    outside us-east-1 (`EUW1-NodeUsage:…`, `EU-Node:…`), so the selector returned
    `None` → a silent `$0` fallback that zeroed every ElastiCache/Redshift node
    cost basis (and all dependent rightsizing/idle levers) in every non-default
    region — a real price mis-read as zero, invisible to the us-east-1-only
    fixtures. Now strips the `<REGION>-` prefix before matching (mirrors the
    sibling EBS/DMS/Aurora suffix selectors); `tests/test_pricing_engine.py`
    adds a region-prefixed case.
  - *Report-layer keyword fabrication (extends SR-2 to ec2/dynamodb).*
    `_calculate_service_savings` synthesized `$25-$200/rec` from
    recommendation-text keywords (ec2 "schedule" → $150, dynamodb "reserved" →
    $200, else a `$25`/`$50` default) whenever an adapter's
    `total_monthly_savings` was `$0` — re-fabricating into the tab headline /
    exec-summary exactly the advisory dollars the ec2 H2 / dynamodb fixes demote
    to `$0` (a non-prod EC2 advisory reading "schedule stop/start" re-counted
    $150). The canonical adapter total is now authoritative; the keyword/default
    tables are removed. `tests/test_reporter_snapshots.py` guards the ec2/dynamodb
    keyword paths. Plus four `services/adapters/CLAUDE.md` rows corrected to match
    the changed cost models (glue ×730 + jobs advisory, apprunner memory-only,
    batch advisory-only, elasticache Graviton exact-delta).

### Fixed (cost-fidelity CRITICAL remediation — `docs/audits/UNIFIED_AUDIT_FINDINGS.md` / `CRITICAL_REMEDIATION_PROMPT.md`)
Cross-service batch closing the CRITICAL findings from the 34-adapter audit. The
through-line: a rendered dollar must be **counted, account-specific, and
defensible** — fabricated config-only dollars become `$0` advisories
(`Counted=False`), and a saving counted by one lever is never re-counted by an
overlapping lever or by Cost Optimization Hub.

- **Shared fixes.**
  - *SR-1 — engine/OS-aware instance SKU.* `PricingEngine._select_instance_node_hourly`
    disambiguates instance-type SKUs that several engines share (ElastiCache
    Redis/Memcached/Valkey), so a node is priced at its real per-engine rate.
  - *SR-2 — flat-rate escape hatch removed.* Dropped the `_FLAT_SAVINGS_SERVICES`
    bypass so every adapter routes through the counted/advisory discipline.
  - *SR-3 — shared CoH de-dup.* New `services/_coh_dedup.py`
    (`normalize_resource_id`, `is_renderable_coh_rec`, `coh_savings`) gives every
    CoH-consuming adapter one canonical resource key and authority order
    (CoH > CO > heuristic). Mirrors the inline RDS `_coh_is_renderable`.
- **ElastiCache C2 (silent $0).** `describe_cache_clusters` returns a lowercase
  engine (`"redis"`), but the Pricing-API guard is case-sensitive (`"Redis"`) —
  every counted node silently priced $0. Normalized via `.capitalize()` in the
  TERM_MATCH filter + `attributes_exact` guard. ElastiCache now also **consumes
  `cost_hub_splits["elasticache"]`** (CoH-covered clusters demote their heuristic
  levers; single highest-$ lever counted per cluster).
- **API Gateway C1.** A `$0` REST→HTTP/caching rec is labeled `Counted=False`
  (advisory) instead of inflating `total_recommendations` with a no-dollar row.
- **QuickSight C1.** SPICE priced edition-aware (`quicksight_spice_rate`):
  Standard `$0.25/GB`, Enterprise `$0.38/GB` (was `$0.38` for both → +52% on
  Standard capacity).
- **Transfer C1.** ProtocolHours is a region-flat rate; the spurious
  `pricing_multiplier` was dropped so card == counted.
- **OpenSearch C2.** An idle-domain rec (full domain cost) now wins the per-domain
  de-dup over an overlapping Graviton-migration rec (a fraction of the same cost),
  which is demoted to advisory — no stacked discounts on one domain.
- **DynamoDB C1.** One counted winner per table (highest-factor lever; reserved
  over over-provisioned), capped at `monthly_current` — never the sum of
  overlapping capacity levers.
- **App Runner C1.** An idle service is priced at its recoverable provisioned
  memory charge (`$0.007/GB/hr × 730`), not a placeholder.
- **Bedrock C1 (idle Provisioned Throughput).** `_derive_model_id` parses the
  foundation-model ARN's final segment (the botocore `ProvisionedModelSummary`
  has no `modelId` member, so the old `pt.get("modelId")` always returned `""` →
  `$1/hr` fabricated and the CloudWatch `ModelId` queries matched nothing) and
  strips the `-YYYYMMDD-vN:N` version suffix to hit the bare `PT_HOURLY_PRICE`
  keys. **Idle-PT tightening (this batch):** `_get_pt_invocation_sum` is now
  tri-state — a CloudWatch read failure → abstain (never delete an unmeasured
  PT); **no** `Invocations` datapoints → candidate idle → `$0` advisory naming
  the estimated recoverable spend (AWS does not publish zero-value datapoints, so
  absence is suggestive, not proof); an explicit `Sum == 0` datapoint → proven
  idle → counted recoverable commitment (still gated at >$1/mo). Previously absent
  datapoints returned `None` and the idle PT was invisible (neither counted nor
  surfaced). Locked by `tests/test_bedrock_idle_pt.py` (13 cases).
- **Tests.** `tests/test_audit_fixes_counted_dollars.py` (apprunner, quicksight
  Standard/Enterprise, transfer, opensearch C2, dynamodb dedup+cap, api_gateway
  advisory), `tests/test_coh_bucket_audit_fixes.py`, `tests/test_bedrock_idle_pt.py`,
  and a probe-confirmed ElastiCache C2 case in `tests/test_pricing_engine.py`.
  Full suite: 567 passed, 1 live-only skip.

### Fixed (pricing — EU region Price List names, audit S3-J)
- **`REGION_DISPLAY_NAMES` corrected for older EU regions.** The AWS Price List
  API names them `"EU (Frankfurt|London|Paris|Stockholm|Milan)"`, but the map had
  `"Europe (...)"`, so every pricing lookup for those regions silently fell back
  to us-east-1 constants (verified cross-service: S3/EC2/RDS). Surfaced once S3-I
  began pricing buckets at their home region — the credited eu-central-1 bucket
  was understated ($35.34 → correct $37.03; live Frankfurt $0.0245−$0.0135).
  `eu-central-2` (Zurich) and `eu-south-2` (Spain) genuinely use `"Europe (...)"`
  and are unchanged. Locked by `test_eu_region_display_names_match_price_list_api`.

### Changed (S3 adapter — cost-fidelity remediation, audit `docs/audits/S3_AUDIT_FINDINGS.md`)
- **Evidence-gated savings replace assumed percentages (S3-A/S3-B).** Removed
  `S3_SAVINGS_FACTORS` (0.30/0.20/0.40 "assume ~65% IA-eligible"). A bucket now
  earns a concrete dollar only when it holds S3 Standard bytes AND CloudWatch
  request metrics (`GetRequests`, whole-bucket FilterId, 30d) show **zero** GETs
  → the saving is the real `standard_gb × (Standard − Standard-IA)` rate delta,
  recorded in `PricingBasis`. No evidence (metrics off / fast mode) → `$0.00`
  advisory. Adds `_assess_bucket_coldness`, `COLD_LOOKBACK_DAYS`,
  `_GAP_OPPORTUNITY_CLASSES`. New IAM dependency `s3:GetMetricsConfiguration`
  (fail-safe on denial). Live M360 ap-south-1: old factor model **$323.62/mo** →
  evidence-gated **$37.70/mo**; suppresses ~$263/mo of fabricated savings on hot,
  actively-served media buckets (where IA would *raise* cost).
- **Per-class costing (S3-A/S3-F).** `EstimatedMonthlyCost` now sums each storage
  class at its own live rate (`_cost_from_class_sizes`/`_s3_price_per_gb`); a
  Glacier/Deep-Archive bucket is no longer priced as Standard (was up to ~23×
  over). Dead `_estimate_s3_bucket_cost` removed.
- **Region-correct pricing (S3-I).** `PricingEngine.for_region(region)` returns a
  cached sibling engine scoped to a resource's home region (shared global pricing
  client, separate cache). S3 buckets are now priced at their own region, not the
  scan region — fixes out-of-region buckets reading the scan rate (e.g. us-east-1
  buckets priced at ap-south-1's $0.025 instead of $0.023, +8.7%).
- **Correct S3 SKU + tier selection (S3-D/S3-E).** `_fetch_s3_price` pins
  `volumeType`+`productFamily=Storage` and `_select_s3_storage_rate` skips
  Staging/Overhead rows; `_extract_s3_base_rate` selects the `beginRange==0` base
  tier ($0.023, not the API's first-serialized $0.022). Verified live vs Pricing
  API (2026-06-18).
- **Count hygiene (S3-C).** `total_recommendations` counts only $-bearing records;
  advisory/visibility records stay rendered and are tallied in
  `extras["advisory_count"]`.
- **Reporter caveats (S3-H).** Renders `PricingBasis`, advisory note, and
  fast-mode sampling warning per bucket. Regional multipliers documented as
  fallback-only (S3-G). SUMMARY.md S3 verdict WARN → PASS.

### Added (RDS snapshot savings reconciled to actual spend — Tier 1)
- **Snapshot upper bounds are now capped at actual billed backup (Cost Explorer).**
  `services.advisor.get_rds_backup_actuals` queries CE (`GetCostAndUsage`, last
  complete month, RDS service, region-scoped, grouped by `USAGE_TYPE`) and sums
  the billed backup usage types per engine (`*:ChargedBackupUsage` standard,
  `Aurora:BackupUsage` Aurora). `services.rds_logic.reconcile_snapshot_savings`
  caps each engine pool's snapshot savings at that actual — but only when the
  actual is a positive number below the upper bound (a 0/missing actual leaves the
  upper bound untouched, so a CE gap never zeroes real savings). Findings carry
  `reconciled_to_actual_billed` / `reconciliation_factor` in AuditBasis, and the
  reporter shows "(reconciled to actual billed backup $X/mo via Cost Explorer)".
  Skipped in `fast_mode`; needs `ce:GetCostAndUsage`. Live M360 ap-south-1
  headline: **$838.83 → $328.22** (standard snapshots $621.30 → actual $110.69).
  Note: the cap is bounded by *total* billed backup (incl. automated backups), so
  it remains a conservative ceiling, not exact per-snapshot attribution.

### Fixed (RDS snapshot finding quality — found via prod scan)
- **No more `$0.00` snapshot findings (B1)**. Some snapshots (notably Aurora
  cluster snapshots) return `AllocatedStorage=0`, which produced `$0.00/month`
  cards. Size-unreported snapshots are now emitted as **advisory** ("size not
  reported by the API; delete to stop backup charges") — still surfaced/counted,
  but no misleading zero.
- **Snapshot $ labelled as an upper bound (B2/B3)**. Snapshot savings use the
  provisioned/allocated size, but AWS bills on actual (compressed/incremental)
  bytes, so the figure is now stated as an **upper bound** in both EstimatedSavings
  and AuditBasis. Aurora cluster-snapshot sizing via `AllocatedStorage` is
  documented as unreliable.

### Fixed (RDS Aurora pricing — found via prod scan)
- **Aurora snapshots priced at the Aurora backup rate (C-A1, CRITICAL)**. Aurora
  cluster/DB snapshots were billed at the standard RDS rate ($0.095/GB-mo) instead
  of Aurora's $0.021/GB-mo, inflating every Aurora snapshot saving ~4.5×. A real
  eu-west-1 scan headline dropped from **$619.22 → $136.88/mo** after the fix.
  `get_rds_backup_storage_price_per_gb(engine)` is now engine-aware and snapshots
  carry `engine`/`rate` in their AuditBasis.
- **Aurora storage mode pinned (M-A1)**. Instance pricing now selects Standard
  ("EBS Only") vs I/O-Optimized ("Aurora IO Optimization Mode") from the cluster's
  StorageType, so the two SKUs ($5.12 vs $6.656/hr for db.r5.8xlarge) no longer
  collide non-deterministically.
- **Aurora instance counting (L-A1)**. `aurora-mysql`/`aurora-postgresql` were
  miscounted as MySQL/PostgreSQL (substring order); Aurora is now checked first.

### Fixed (RDS deep-audit second pass)
- **SQL Server / Oracle edition pricing (N-H1)**. Instance pricing pinned only
  `databaseEngine`, so all editions collided and MaxResults returned one
  arbitrarily (db.m5.large SQL Server Web $0.311/hr vs Standard $0.977/hr — 3.1x).
  Now derive `databaseEdition` from the engine string and pin it.
- **License model from the instance (N-M1)**. License was hardcoded by a static
  engine set; Oracle EE (BYOL-only, no "No license required" row) matched nothing
  and silently fell back to the MySQL constant, and Oracle SE2 LI ($0.438/hr) vs
  BYOL ($0.171/hr) is a 2.6x swing. Now read each instance's `LicenseModel` and
  thread it through pricing.
- **Backup retention is advisory (N-M2)**. The old `allocated×rate×days/30` model
  double-counted and ignored the free allotment (= 100% of provisioned storage).
  The billable excess isn't derivable at scan time, so the check is now advisory
  (rendered/counted, excluded from the savings headline) with the free-allotment
  rule + per-GB rate instead of a fabricated figure.
- **Multi-AZ / scheduling now require CloudWatch evidence (N-M3)**. Both gated on
  a DatabaseConnections read (idle→schedule, sustained-load→keep HA); fast_mode
  skips with a warning, no data warns + skips, AccessDenied → permission_issue.
- **Scheduling covers all non-Aurora engines (N-M4)** (was mysql/postgres/mariadb
  only, silently excluding Oracle/SQL Server).

### Fixed (RDS cost-accuracy audit)
See `docs/audits/RDS_REMEDIATION_PLAN.md` for the full finding catalogue.
- **Phantom gp2→gp3 storage savings removed (C1)**. Unlike EBS, RDS gp2 and gp3
  *base* storage cost the same per GB ($0.115/GB-Mo for every engine, verified
  live via the Pricing API). The flat "20% of gp2 storage cost" produced a saving
  that does not exist for RDS; removed.
- **Cost Optimization Hub bucket now consumed (H1)**. The orchestrator bucketed
  `RdsDbInstance`/`RdsDbCluster` recs into `ctx.cost_hub_splits["rds"]` but no
  adapter read it — the savings were silently dropped. The adapter now consumes
  the bucket as the authoritative source (suppresses a DB's CO/heuristic findings),
  emits a `cost_optimization_hub` source, and registers the matching reporter
  handler.
- **Counted == rendered (H3)**. De-duplication moved into the pure
  `services/rds_logic.resolve_rds_findings`: only the single highest-savings
  finding survives per DB, and losing recs are dropped from the emitted sources
  (previously the savings total was deduped but every rec still rendered, so the
  cards summed to more than the tab total).
- **Compute Optimizer opt-in placeholder no longer counted (H2)**. The synthetic
  "enable Compute Optimizer" $0 rec is converted to a warning and dropped from the
  count (it rendered to nothing), mirroring EC2.
- **Compute Optimizer permission/opt-in classified (H4, H5)**. RDS CO failures are
  recorded via `ctx.permission_issue` / `ctx.warn` instead of logger-only, and
  non-actionable (`Optimized` / zero-savings) recs are filtered at the source.
- **Reserved Instances demoted to advisory (M1)**. Still rendered, but excluded
  from the savings headline (`commitment_analysis` is the authoritative RI source
  and RI stacks with — rather than replaces — a rightsizing saving).
- **RDS pricing filters corrected (M2, M3, M4)**: storage `volumeType` mapped to
  the real Price List labels (`General Purpose`, `General Purpose-GP3`, …; the old
  `.upper()` never matched); SQL Server Multi-AZ resolved to
  `Multi-AZ (SQL Server Mirror)`; backup-storage lookup pinned to a deterministic
  engine.
- **Unknown RDS engine no longer priced silently as MySQL (L1)**; dead reduction
  constants and never-populated check buckets removed (L4); each finding now
  carries a structured `AuditBasis` (rate / region / engine / metric-window /
  formula) (L2).

## [3.4.0] - 2026-05-14

### Removed (Cost-Only Scope Refinement)
- **40+ non-cost-saving findings purged across 24 service modules**. The scanner is now strictly a cost-optimization tool — every emitted recommendation must produce a concrete account-specific $ saving. Removed: health / state checks (DEGRADED add-ons, inactive node groups, replication lag monitors), best-practice nudges (Fargate adoption, Aurora Serverless v2 migration, "consider Graviton" without instance pricing), version-upgrade prompts (Old Redis / Elasticsearch / OpenSearch versions), monitoring-enablement directives (Container Insights, CloudWatch detailed monitoring, DLM), $0/month findings with "quantify after X" tails, and percentage-range estimates with no per-account baseline. Specifically:
  - `services/ec2.py`: Monitoring Required, Auto Scaling Missing, Burstable Instance Optimization (no metrics), Stopped Instances housekeeping, Static ASGs (EKS + non-EKS), Non-Prod 24/7 ASGs, Missing Scale-In Policies, Monitoring-Only Instances.
  - `services/rds.py`: io1/io2/gp3 IOPS review, Stopped-database housekeeping, Burstable Instance Rightsizing (no metrics), Aurora Serverless v2 migration nudge (instance + cluster), Aurora I/O-Optimized "review".
  - `services/elasticache.py`: Old Engine Version (Redis < 7 upgrade nudge).
  - `services/containers.py`: ECS Container Insights Required, EKS Performance Optimization (scale-up = cost increase), EKS Container Insights Required.
  - `services/adapters/eks.py`: Node group DEGRADED state, under-utilized scaling config, "No Fargate profiles configured" best-practice nudge, marketplace add-on $0 review, Add-on DEGRADED state.
  - `services/adapters/aurora.py`: Clone sprawl, Global DB replica lag, Backtrack window.
  - `services/adapters/bedrock.py`: Idle Bedrock agent (AWS docs confirm agents themselves accrue no charge — hardcoded $5/month placeholder removed).
  - `services/monitoring.py`: Excessive log storage, Unused CloudWatch Alarms, Multi-Region CloudTrail, S3/Lambda Data Events trail flags, CloudTrail Insights, Multiple CloudTrail Trails.
  - `services/load_balancer.py`: Public Internal LB (security), Excessive ALB rules (no per-rule $), Unnecessary Cross-AZ LB (fake 1GB/hour baseline).
  - `services/route53.py`: Complex Routing Simple Use, Unnecessary Health Checks (generic $0.50/check, not per-account).
  - `services/step_functions.py`: Non-Prod 24/7 (percentage range).
  - `services/ebs.py`: Underutilized high-IOPS volumes flag (no-metrics fallback), Snapshot Lifecycle (DLM enablement).
  - `services/athena.py`: Query Results lifecycle nudge, Workgroup Optimization (scan limit).
  - `services/api_gateway.py`: API Gateway Caching (caching itself adds cost).
  - `services/cloudfront.py`: Disabled distribution housekeeping ($0 explicit), Origin Shield Review (net effect can go either way).
  - `services/dms.py`: DMS Serverless monitor nudge ("Variable based on usage").
  - `services/glue.py`: Crawler Optimization ("Variable based on frequency").
  - `services/msk.py`: MSK Serverless monitor nudge ("Variable based on usage").
  - `services/lambda_svc.py`: Lambda Low Invocation (no idle cost on Lambda — savings ~$0), Lambda VPC Configuration (mixed perf+cost), Lambda Reserved Concurrency (reserved concurrency itself is free).
  - `services/backup.py`: Cross-Region Backup Copies (resilience trade-off), Ephemeral Resource Backups (unquantified), Multiple Backup Plans (AWS Backup plans are free per AWS docs — only jobs cost money).
  - `services/apprunner.py`: Auto Scaling Optimization (hardcoded "$30/month potential" magic number).
  - `services/opensearch.py`: Old OpenSearch Version, Old Elasticsearch Version (engine cost identical across versions).

  Findings that survived this purge all carry one of: (1) a live `PricingEngine`-derived $ amount, (2) a per-resource `parse_dollar_savings`-extracted value, (3) a concrete formula like `(current - recommended) × per-unit-price × 730`. AWS Knowledge MCP was consulted to confirm the cost reality of five borderline cases (Bedrock agents, AWS Backup plans, VPC gateway endpoints, Lightsail bundle transfer allowance, ALB LCU pricing) before removal.

### Added
- **RDS Reserved Instance scenario matrix on every database**. The Reserved Instance Opportunities rec category in the RDS tab no longer disclose a single hard-coded `"1-yr no-upfront RI"` scenario; each candidate database now carries an `RIScenarios` list covering the full 6-cell purchase matrix (1yr / 3yr × No Upfront / Partial Upfront / All Upfront) computed via the new `RDS_RI_DISCOUNT_MATRIX` constants in `services/rds.py` against the live PricingEngine-derived on-demand baseline. The renderer (`_render_rds_ri_scenarios_table` in `reporter_phase_b.py`) emits a compact per-database table with the maximum-savings row highlighted, alongside the on-demand monthly baseline so the FinOps reader can audit the discount math.
- **Cost Optimization Hub commitment scenario matrix**. CoH-routed reservation / Savings Plan recommendations consumed by the Commitment Analysis tab via `ctx.cost_hub_splits["commitment_analysis"]` are now scaled from AWS's single recommended (term, payment_option) into the full 6-cell matrix using standard tier ratios (`_COH_COMMITMENT_TIER_RATIOS` in `reporter_phase_b.py`). Each commitment rec renders an inline scenarios table below the existing CoH summary table; the AWS-recommended scenario row is highlighted as the anchor and the others are shown as percent-delta relative to it.

### Changed
- **Compute Optimizer findings distributed into per-service tabs** (mirrors the earlier Cost Optimization Hub retirement). The standalone "Compute Optimizer" tab no longer renders. EC2 / EBS / RDS recommendations were already inline in their tabs via `services.advisor.get_<resource>_compute_optimizer_recommendations`; Lambda / ECS / ASG recommendations now flow through the corresponding per-service adapters: Lambda CO recs render inside the Lambda tab as a `compute_optimizer` source, ECS CO recs render inside the Containers tab as a `compute_optimizer` source, and ASG CO recs render inside the EC2 tab as an `asg_compute_optimizer` source. Adds `get_lambda_compute_optimizer_recommendations`, `get_ecs_compute_optimizer_recommendations`, `get_asg_compute_optimizer_recommendations` plus their normalization helpers to `services/advisor.py`. New `SOURCE_TYPE_MAP` + `PHASE_B_HANDLERS` bindings reuse the existing `_render_compute_optimizer_source` renderer because the rec schema is identical across all four resource types. Cleans up: the per-service-with-no-savings "Optimization Recommendations: 18 items / Monthly Savings: $0.00" surface that the standalone tab produced when the underlying CO recs had empty `savingsOpportunity` blocks.

### Removed
- **Cost Anomaly Detection adapter retired** from `services/__init__.py:ALL_MODULES`. The "Cost Anomaly Detection" tab no longer renders; the standalone scan path (Cost Explorer `get_anomalies` / `get_anomaly_monitors` / `get_anomaly_subscriptions` + CloudWatch `describe_alarms`) is removed entirely. The HIGH-severity risk signal in the executive summary now reads purely from priority-tagged recommendations across all surviving services rather than falling back to active-anomaly count. Deleted: `services/adapters/cost_anomaly.py`, `_render_cost_anomaly_source` in `reporter_phase_b.py`, `cost_anomaly` tab spec in `html_report_generator.py`, four `SOURCE_TYPE_MAP` + four `PHASE_B_HANDLERS` entries, golden fixture key.
- **Compute Optimizer adapter retired** from `services/__init__.py:ALL_MODULES`. Deleted: `services/adapters/compute_optimizer.py`, `compute_optimizer` tab spec in `html_report_generator.py`, golden fixture key. The legacy `(compute_optimizer, *)` `SOURCE_TYPE_MAP` and `PHASE_B_HANDLERS` entries are retained for any in-flight scan JSON that predates the retirement; new scans never emit them.
- Service count: 36 → 34 after both removals.

## [3.3.0] - 2026-05-14

### Added
- **Design context files**: `PRODUCT.md` (strategic intent: register=product, three-deep user funnel of cloud architect / FinOps engineer / DevOps drill-down, audit-grade voice, anti-references) and `DESIGN.md` (visual system following the Google Stitch DESIGN.md format: frontmatter tokens for colors / typography / rounded / spacing / components, plus six prose sections, plus 9 Named Rules and a Do's / Don'ts forceful enough to enforce the strategic line).
- **Sidecar** `.impeccable/design.json` (schemaVersion 2): tonal ramps per color, shadow / motion / breakpoint tokens, full HTML/CSS drop-in component snippets, narrative mapping (north star, key characteristics, rules, dos, donts). Renders in the live impeccable panel.
- **Premium-paper type system**: Newsreader (display, editorial serif via Google Fonts CDN) + IBM Plex Sans (body / labels / chrome) + IBM Plex Mono (resource IDs, code). Replaces Roboto / Roboto Mono. `font-feature-settings: "ss01", "cv05"` on body, optical-sizing on Newsreader, tabular-nums on every aligned-numeric element. Print stylesheet keeps its scoped Georgia carve-out.
- **Structured executive summary**: large Newsreader figure (`$X.XX per month`, `defensibly recoverable` italic caption) followed by a three-column dl of facts (`Annual` / `Top services` / `Open risks`) with hairline dividers. Replaces the four-card grid + risks-row + prose sentence.
- **Sticky savings-sorted jump-nav rail**: left-margin aside on viewports >= 1400px, savings-descending list of services with compact dollar chips. Hover-edge auto-hide (6px sliver at rest, slides in on hover or focus-within), `prefers-reduced-motion` pins it open. `jumpToPanel()` JS helper activates the target tab via `showTab` and smooth-scrolls the panel into view.
- **Source-confidence taxonomy** (`reporter_phase_b.VALID_SOURCE_BADGES`): single enum of four labels (`Metric Backed`, `ML Backed`, `Cost Hub`, `Audit Based`) that 1:1 matches the rendered glossary. `source_type_badge` refuses to render any out-of-enum label. Each per-service group renders as a `<section class="source-section" data-source="...">` wrapper that drives a CSS `::before` typographic prefix on every nested rec-item title (`METRIC ·`, `ML ·`, `COST HUB ·`, `AUDIT ·`), retiring the chip-badge form.
- **Priority filter strip**: `Filter: All / High priority / Medium / Low` chips above the tabs. `body[data-priority-filter]` attribute selector dims non-matching `.rec-item` cards across every tab with no DOM mutation.
- **Tab strip restructured**: tabs sort by total monthly savings descending (top earners lead), tab-chip shows compact dollar amount (`$267`, `$1.2k`) instead of recommendation count, zero-savings tabs render with no chip. New `_format_savings_chip()` helper.
- **Footer scan-JSON download**: data: URL link embeds the raw scan results in the page so the report is self-contained. JSON encoder serializes datetimes via `isoformat`.
- **Reservation matrix in `commitment_analysis`**: SP and RI purchase recommendations fan out across `(1yr, 3yr) × (No Upfront, Partial Upfront, All Upfront)` per type/service. Each rec carries explicit `term` and `payment_option` fields and renders as `Action: SP Purchase Recommendation (1yr, All Upfront)`. Each call is independent; one denial does not kill the others. Ships disabled-friendly: degrades cleanly under CE access-deny.
- **Hover-edge handle on the jump-nav rail**: a 3 × 32 px vertical bar in `--text-secondary` at 35% opacity hints at the sliver's presence; fades on expand.
- **`logger` setup** in `services/containers.py` so ECS cluster diagnostics use `logger.debug` instead of fifty per-cluster `print()` lines per scan.

### Changed
- **Cost Optimization Hub adapter retired** from `services/__init__.py:ALL_MODULES` (replaced by per-service distribution). The standalone "Cost Optimization Hub" tab no longer exists. `ScanOrchestrator._prefetch_advisor_data` now extends its `type_map` to bucket `EcsService` / `EcsTask` / `EcsCluster` into `containers` and every `*ReservedInstances` / `*SavingsPlans` into `commitment_analysis`. Unbucketed types are surfaced via `ctx.warn` so the map can be extended deliberately.
- **`containers.py` adapter** now consumes `ctx.cost_hub_splits["containers"]` and renders the recommendations as a `cost_optimization_hub` source alongside its enhanced checks.
- **`commitment_analysis.py` adapter** now consumes `ctx.cost_hub_splits["commitment_analysis"]` for CoH-curated SP / RI purchase recs, rendered alongside its CE-API-derived utilization and coverage data.
- **HTML report visual baseline**: header is now flat (no linear-gradient, no radial halo, no glassmorphism); recommendation cards use full hairline borders with priority encoded as a leading `::before` badge (`High priority`, `Medium priority`, `Low priority`) instead of a 4px colored side-stripe; info / warning / success / opportunity callouts use full 1px borders in the semantic color rather than left-stripes; hover states shift border color or background tone only (no `translateY`, no shadow upgrade); motion easing on every transition is `var(--ease-out-quart)` = `cubic-bezier(0.16, 1, 0.3, 1)` rather than Material's `cubic-bezier(0.4, 0, 0.2, 1)`; badge tonal pairs are tokenized into `--badge-{success,warning,danger,info}-{fg,bg}` for both light and dark themes; scrollbar thumb and `top-buckets-table h4` use neutral colors rather than `--primary` (Status-Channel Rule).
- **Heading outline** repaired across the report: stat-card labels emit `<div class="stat-label">` instead of `<h4>`, recommendation group titles emit `<h4>` instead of `<h5>`, and the hidden SVG sprite has `aria-hidden="true" focusable="false"`. 26 detector skipped-heading findings cleared.
- **Snapshot rollup deduplication**: `_render_ebs_enhanced_checks` skips snapshot CheckCategories so the same SnapshotId is rendered once in the dedicated Snapshots tab rather than once per CheckCategory in EBS plus again in Snapshots.

### Fixed
- **`html_report_generator._get_footer` datetime crash**: `json.dumps(scan_results)` now passes a `default=` serializer that ISO-formats datetimes and falls back to `str()` for any exotic type. The whole block is wrapped in `try / except` so even a worst-case serialization failure just omits the download link rather than aborting report generation.
- **Cost Anomaly adapter parameter rename**: `DateRange` -> `DateInterval` on `ce.get_anomalies` per the current Cost Explorer SDK. Anomaly recommendations now actually surface (when permissions allow) instead of being silently dropped by validation.
- **Source-confidence prefix glyph**: `\\00b7` CSS escape was being parsed by Python as octal NUL (`\\x00`) followed by literal `b7`, producing `METRIC \\x00b7` in the emitted CSS. Switched to the literal `·` (U+00B7) character; the HTML document is UTF-8 and the byte sequence survives the round trip cleanly.
- **Source-confidence taxonomy drift**: legacy "Static Analysis" label retired in favor of "Audit Based" (which is what the glossary defines). All `SOURCE_TYPE_MAP` and `_GENERIC_SOURCE_TYPES` entries renamed; `source_type_badge` enforces the four-label enum at render time.
- **ECS cluster Debug noise**: 50+ `print(f"Debug: Cluster X...")` lines per scan demoted to `logger.debug()` so default scan output stays terse.

## [3.2.1] - 2026-05-03

### Fixed
- **46 audit remediations** across 8 waves from `AUDIT_REPORT.md` code audit:
  - W1 (7): Critical crash in `html_report_generator.py` (KeyError), wrong dollar values in `cost_anomaly.py`, `opensearch.py`, `ebs.py`, `file_systems.py`, `lambda_svc.py`, `network_cost.py`
  - W2 (8): Contract key mismatches in `optimization_descriptions` across 7 adapters (`ebs`, `ami`, `dynamodb`, `eks`, `dms`, `s3`, `cloudfront`) and summary count in `compute_optimizer.py`
  - W3 (3): Missing client declarations in `elasticache`, `mediastore`, `network` adapters
  - W4 (10): Missing `reads_fast_mode` class attributes in 10 adapters
  - W5 (5): Pagination gaps in `commitment_analysis` (4 CE APIs), `cost_anomaly` (2 APIs), `monitoring`, `aurora`, `sagemaker` (2 APIs)
  - W6 (3): Silent `except: pass` blocks replaced with warning logs; legacy calls wrapped; `rds.py` narrowed exception scope
  - W7 (6): XSS fix via `html.escape()` in report generator, `deepcopy` for source mutation, comma-safe savings parsing, `logging.warning()` replacing `print()`, service count card fix, `json.dumps` for chartData
  - W8 (4): `datetime.utcnow()` → `datetime.now(timezone.utc)`, magic numbers → named constants, `parse_dollar_savings` percentage fallback
- **4 post-fix corrections**: `opensearch.py` (live pricing path ×2), `ebs.py` (live pricing path ×1), `compute_optimizer.py` (summary count) — all now apply `ctx.pricing_multiplier` consistently on both live and fallback pricing paths
- **Cost Optimization Hub double-counting**: Replaced fabricated `_ROUTED_TYPES` strings (`Ec2InstanceRightsizing` etc.) with correct `_ROUTED_RESOURCE_TYPES` using real AWS API `currentResourceType` values (`Ec2Instance`, `EbsVolume`, `LambdaFunction`, `RdsDbInstance`)
- **EKS/EC2 triple-counting**: Added `_is_eks_managed_instance()` helper in `services/ec2.py` to skip EKS-managed nodes in `get_enhanced_ec2_checks()` and `get_advanced_ec2_checks()`, eliminating overlap with `eks.py` and `containers.py` adapters

### Removed
- Aurora/RDS investigation closed as false positive (complementary sub-populations, no dollar overlap)

## [3.2.0] - 2026-05-02

### Added
- **Future Roadmap** (`docs/ROADMAP.md`): 28-capability research-backed roadmap across 4 phases, covering Compute Optimizer integration, Cost Optimization Hub, Aurora checks, Savings Plans analysis, AI/ML cost visibility, EKS/Kubernetes, FOCUS 1.2 export, multi-account support, and more
- **Service Audit Reports** (`docs/audits/REPORT.md`): Consolidated cross-service audit results for all 28 adapters (3 PASS, 22 WARN, 1 FAIL)

### Changed
- **CLAUDE.md (root)**: Rewritten as lean project quick-reference; agent policy moved to `AGENTS.md` as canonical source
- **CONTRIBUTING.md**: Rewritten for v3.0 ServiceModule adapter architecture (was referencing 8,677-line monolith)
- **ROADMAP.md**: Corrected baseline from 10 to 28 adapters; removed 4 items already implemented (NAT Gateway, EFS, Redshift, CloudFront); fixed competitive benchmarking and success metrics tables
- **.gitignore**: Added `.sisyphus/` to AI assistant exclusions

### Removed
- Deleted `report_audit.md` and `service_audit.md` (completed one-shot audit prompts)
- Moved `Audit/` directory to `docs/audits/` for cleaner root structure
- Moved pricing plan files from root to `docs/`

## [3.1.0] - 2026-05-01

### Added
- **Live Pricing Engine** (`core/pricing_engine.py`, 517 lines): Centralized AWS Pricing API client with in-memory `PricingCache` (6-hour TTL). 12 public methods covering EC2 instances, EBS volumes, RDS instances and storage (including Multi-AZ), S3 storage classes, and generic instance/storage lookups
- **PricingEngine integration** into `ScanContext`: All adapters access live pricing via `ctx.pricing_engine` with automatic fallback to `pricing_multiplier` on API failures
- **22 unit tests** for PricingEngine (`tests/test_pricing_engine.py`): cache behavior, API query construction, Multi-AZ storage pricing, error fallbacks

### Changed
- **11 adapters migrated** from flat-rate/heuristic pricing to live AWS Pricing API or resource-size-aware calculations:
  - `workspaces.py` — live WorkSpaces bundle pricing via `get_instance_monthly_price()`
  - `glue.py` — DPU-based pricing ($0.44/DPU/hour × 160 hrs/month × 0.30 rightsizing)
  - `lightsail.py` — live Lightsail bundle pricing via `get_instance_monthly_price()`
  - `apprunner.py` — vCPU ($0.064/hr) + memory ($0.007/GB/hr) hourly rates × 730
  - `transfer.py` — per-protocol hourly pricing ($0.30/protocol/hour × 730)
  - `mediastore.py` — S3-equivalent storage pricing via `get_s3_monthly_price_per_gb()`
  - `quicksight.py` — SPICE tier pricing ($0.25–$0.38/GB × unused capacity)
  - `containers.py` — Fargate rates ($0.04048/vCPU + $0.004445/GB/hr) × 730 with spot/rightsizing/lifecycle discounts
  - `dynamodb.py` — RCU ($0.00013/hr) + WCU ($0.00065/hr) × 730 × 0.23 reserved discount
  - `athena.py` — CloudWatch ProcessedBytes → $5/TB × 0.75 scan reduction (fast_mode fallback)
  - `step_functions.py` — CloudWatch ExecutionsStarted → $0.025/1K transitions × 0.60 (fast_mode fallback)
- **19 adapters now use live pricing** total (8 original complex adapters + 11 newly migrated)
- **RDS Multi-AZ storage pricing**: `get_rds_monthly_storage_price_per_gb()` accepts `multi_az` parameter with independent cache keys
- **Network adapter**: replaced regex parsing with `parse_dollar_savings()` from `services/_savings.py`
- **S3 volume type filter**: Fixed `GetProducts` query to include correct `volumeType` values
- **VPC Endpoint pricing**: Fixed dict collision bug for multiple endpoint types
- **MSK NumberOfBrokerNodes**: Fixed missing field extraction

### Removed
- No breaking changes. Flat-rate fallbacks preserved via `pricing_multiplier` for `--fast` mode and API failures

## [3.0.0] - 2026-04-30

### Changed (BREAKING)
- **Modular Architecture**: Replaced 8,500-line `cost_optimizer.py` monolith with clean modular architecture
  - `cost_optimizer.py` is now a 130-line thin shell delegating to `ScanOrchestrator`
  - 28 service scans extracted into independent `ServiceModule` adapter classes in `services/adapters/`
  - Core orchestration extracted into `core/` package (ScanContext, ScanOrchestrator, ScanResultBuilder, ClientRegistry, ServiceModule Protocol)

### Added
- **ServiceModule Protocol** (`core/contracts.py`): Formal interface for service adapters with key, cli_aliases, display_name, stat_cards, grouping, scan(), custom_grouping()
- **ScanOrchestrator** (`core/scan_orchestrator.py`): Iterates registered modules with safe_scan error handling, pre-fetches Cost Hub data
- **ScanResultBuilder** (`core/result_builder.py`): Serializes ServiceFindings to JSON matching legacy format
- **ClientRegistry** (`core/client_registry.py`): Caching boto3 client factory with global-service routing (us-east-1 for Route53, CloudFront, IAM, etc.)
- **AwsSessionFactory** (`core/session.py`): Session management with adaptive retry config
- **28 ServiceModule Adapters** (`services/adapters/`): One file per AWS service
  - 12 flat-rate: lightsail, redshift, dms, quicksight, apprunner, transfer, msk, workspaces, mediastore, glue, athena, batch
  - 6 parse-rate: cloudfront, api_gateway, step_functions, elasticache, opensearch, ami
  - 6 complex: ec2, ebs, rds, s3, lambda, dynamodb
  - 4 composite: file_systems (efs+fsx), containers (ecs+eks+ecr), network (eip+nat+vpc+lb+asg), monitoring (cloudwatch+cloudtrail+backup+route53)
- **BaseServiceModule** (`services/_base.py`): Base class with default implementations
- **Service Descriptor Dict pattern** in reporter: Reduced `if service_key ==` branches from 62 to 7
- **131 tests** including 112 new snapshot tests for reporter refactoring
- **resolve_cli_keys** (`core/filtering.py`): Alias-based CLI service filtering

### Removed
- ~8,400 lines of inline service scan logic from `cost_optimizer.py` (preserved as `.bak` locally)

### Reporter Refactoring (Phase 1)
- **html_report_generator.py**: 4,380 -> 2,432 lines (-44%)
- **reporter_phase_a.py** (424 lines): Descriptor-driven grouped services
- **reporter_phase_b.py** (1,502 lines): Function registry for source handlers
- Smart grouping: 62 -> 7 `if service_key ==` branches
- EC2 pre-filter + S3 extra stats registries for clean data flow

## [2.6.0] - 2026-01-25

### Added
- **📊 Executive Summary Tab**: Interactive dashboard for executive-level cost optimization reporting
  - **First Tab**: Executive summary now appears as the first active tab in HTML reports
  - **Interactive Charts**: Pie and bar charts showing cost savings distribution by AWS service
  - **Key Metrics Dashboard**: Total savings, recommendations, and services scanned at a glance
  - **Click-to-Filter**: Click chart segments to navigate directly to specific service tabs
  - **AWS-Themed Styling**: Professional blue/orange color scheme matching AWS branding
  - **Chart.js Integration**: Modern, responsive charts with hover tooltips and animations
  - **Empty State Handling**: User-friendly message when no recommendations are found
  - **Mobile Responsive**: Charts adapt to different screen sizes for mobile viewing
- **🌙 Dark Mode Support**: Complete dark theme implementation with toggle functionality
  - **Toggle Button**: Fixed-position button with moon/sun icons in top-right corner
  - **Theme Persistence**: Remembers user preference using localStorage across sessions
  - **Dynamic Chart Colors**: Charts automatically adapt colors and text for dark mode
  - **Professional Dark Theme**: Dark backgrounds (#121212, #1e1e1e) with light text
  - **Smooth Transitions**: Instant theme switching with CSS transitions
  - **Full Integration**: All UI elements including executive summary adapt to selected theme

### Enhanced
- **Report Navigation**: Improved tab switching with visual feedback for filtered services
- **Professional Presentation**: Executive-ready formatting for C-level cost optimization discussions

## [2.5.9] - 2026-01-23

### Fixed
- **Conditional Recommendations**: Usage-based gating added to prevent false positives
  - API Gateway REST→HTTP: Only recommends for simple APIs (≤10 resources)
  - Step Functions Standard→Express: Only for high-volume workflows (>100 executions/day)
  - CloudFront Price Class: Only for active distributions (>1000 requests/week)
  - Lambda ARM Migration: Only for actively used functions (>10 invocations/week)
- **Network Pagination**: Added pagination for VPC endpoints and VPCs
- **ElastiCache Valkey**: Corrected duplicate keys and removed savings claim
- **Snapshots Report**: Deduplicated Snapshot IDs and filtered invalid entries
- **EC2 Report**: Filter ECS resources from Cost Optimization Hub section
- **CloudWatch Logs**: Retention savings use storage pricing

### Enhanced
- **Metric-Backed Analysis**: CloudWatch gating for selected recommendations
- **Report Accuracy**: Improved deduplication and validation in snapshots and EC2 tabs

## [2.5.8] - 2026-01-22

### Fixed
- **HTML Report Accuracy**: Fixed resource categorization and data validation issues
  - EC2 tab now excludes ECS resources (proper service separation)
  - S3 tab eliminates "Unknown" bucket entries (enhanced field detection)
  - Improved resource type filtering for cleaner reports
- **DynamoDB CloudWatch Integration**: Metric-backed billing mode recommendations
  - 14-day CloudWatch analysis for On-Demand → Provisioned recommendations
  - Smart utilization logic (70% threshold, predictability checks)
  - Eliminates inappropriate recommendations for spiky/low-usage tables
  - Clear guidance when CloudWatch metrics unavailable

### Enhanced
- **Report Quality**: Clean, accurate resource categorization across all service tabs
- **Data-Driven Recommendations**: CloudWatch metrics replace heuristic-based suggestions
- **User Experience**: Actionable recommendations with detailed justifications

### Technical
- Enhanced HTML report generator with comprehensive resource filtering
- Improved DynamoDB analysis with utilization and variability calculations
- Better field compatibility for standard and enhanced check formats

## [2.5.1] - 2026-01-21

### Fixed
- **Critical Parsing Bug**: Fixed snapshot savings parsing errors in HTML report generator
  - Resolved thousands of parsing warnings: "Could not parse snapshot savings"
  - Enhanced parsing logic to handle descriptive text like "(max estimate)"
  - Accurate savings calculations for all snapshot optimization recommendations
  - Clean scan output without parsing noise

## [2.5.0] - 2026-01-20

### Added
- **Container Insights Integration**: Real CloudWatch metrics for ECS/EKS rightsizing
  - ECS: CPU/Memory utilization analysis with explicit enablement verification
  - EKS: Cluster-level metrics with add-on and manual installation detection
  - Metric-backed recommendations with 7-day measurement periods
- **Enhanced S3 Storage Classes**: Added Glacier Instant Retrieval and Express One Zone
- **S3 Intelligent-Tiering Archive Access**: Added $0.0036/GB tier tracking
- **Regional Pricing**: Updated per‑region multipliers where defined

### Changed
- **Pricing Accuracy Improvements**:
  - S3 Glacier: Fixed from $0.004 to $0.0036/GB (11% more accurate)
  - Elastic IP: Updated from $3.60 to $3.65/month (730-hour calculation)
  - Added AWS documentation source URLs for all pricing constants
- **Container Insights Detection**: Two-tier detection (add-on + metrics fallback)
- **Timezone Consistency**: All CloudWatch queries use UTC timestamps
- **Message Precision**: Specific CPU/memory values with measurement periods

### Fixed
- **Critical ECS Loop Bug**: Fixed instance_type/state variables inside EC2 instance loop
- **EBS Volume Filtering**: Exclude volumes attached to stopped instances from unattached list
- **S3 Fast-Mode Warnings**: Removed extrapolation, added size estimation disclaimers
- **DynamoDB CloudWatch Integration**: Real capacity utilization metrics analysis
- **Container Insights Duplication**: Moved cluster-level checks outside nodegroup loops
- **RDS Indentation Issues**: Fixed all Unknown findings and resourceArn fields
- **Regional Pricing Consistency**: Applied multipliers across all cost calculations

### Security
- **IAM Policy Updated**: Added Container Insights and enhanced monitoring permissions
- **Error Handling**: Graceful fallbacks for all CloudWatch metric queries

### Documentation
- **Source Citations**: AWS documentation URLs for all pricing constants
- **Regional Multiplier Documentation**: Clear explanation of pricing variations
- **Container Insights Guide**: Setup and enablement instructions
- **Enhanced README**: Updated with latest features and capabilities

## [2.4.0] - 2026-01-20

### Fixed - Service Filtering & Report Generation
- **Case-Insensitive Service Filtering**: Service names now case-insensitive (MSK, msk, Msk all work)
- **Service Isolation**: `--scan-only` now properly isolates services (no data leakage from other services)
- **CloudFront/API Gateway/Step Functions**: Added to service_map for proper filtering (30 total filterable services)
- **HTML Report Accuracy**: Reports now show only scanned services with recommendations
- **Data Collection Filtering**: EBS/RDS data only collected when those services are scanned
- **Empty Recommendations Fix**: Skipped services now correctly return empty lists (not dicts)
- **File Systems Filtering**: EFS/FSx properly skipped when not in scan-only list

### Enhanced - HTML Report Generator
- **Grouped Findings Support**: Lightsail, DMS, Glue now display recommendations grouped by category
- **Dual Format Support**: Handles both old format (dict with count) and new format (direct lists)
- **Services Scanned Count**: Now shows only services with recommendations
- **Tab Visibility**: Services with 0 recommendations automatically hidden from tabs

### Fixed - IAM Policy
- **Complete Permissions**: Added missing elasticloadbalancingv2 permissions for ALB/NLB
- **S3 Multipart Uploads**: Added s3:ListMultipartUploads permission
- **Explicit Permissions**: Replaced wildcards with specific EC2/RDS permissions
- **IAM Policy**: Updated permission coverage for supported services

### Fixed - Documentation
- **Service Filtering**: Updated count from 25 to 30 categories (added CloudFront, API Gateway, Step Functions)
- **Documentation Updates**: Aligned README and architecture notes with current service coverage
- **Cost Model**: Documented heuristic estimation approach and limitations

### Fixed - Code Quality
- **Deprecation Warning**: Replaced datetime.utcnow() with timezone-aware datetime.now(timezone.utc)
- **Duplicate Removal**: Removed duplicate "Additional Services" container (API Gateway/Step Functions)
- **Indentation Errors**: Fixed mediastore_findings and total_savings calculation
- **Orphaned Code**: Removed duplicate step_functions_checks assignment

### Technical Improvements
- **Service Filtering Logic**: Normalized service names to lowercase before comparison
- **Recommendation Counting**: Fixed len() calls on dict vs list inconsistencies
- **HTML Generator**: Added Lightsail, DMS, Glue, Redshift to grouped services skip lists
- **Service Map**: Now includes 30 filterable services (was 25)

## [2.3.0] - 2025-12-15

### Added
- **Multi‑Service Coverage**: Comprehensive coverage across compute, storage, networking
- **Regional Pricing**: Region‑aware cost multipliers where defined
- **Professional HTML Reports**: Interactive multi-tab interface

### Features
- **EC2 Optimization**: Idle instances, rightsizing, Graviton migration, Spot opportunities
- **Storage Optimization**: EBS gp2→gp3 migration, S3 lifecycle policies, EFS optimization
- **Database Optimization**: RDS Graviton migration, DynamoDB capacity rightsizing
- **Container Optimization**: ECS/EKS rightsizing, ECR lifecycle policies
- **Network Optimization**: EIP management, NAT Gateway optimization, Load Balancer analysis

## [2.2.0] - 2025-11-20

### Added
- **Service Filtering**: Target specific services with --scan-only and --skip-service
- **Fast Mode**: Optimized scanning for large S3 environments (100+ buckets)
- **Enhanced Error Tracking**: Comprehensive permission issue visibility
- **Cross-Region S3 Analysis**: Proper analysis across all AWS regions

### Changed
- **Performance Improvements**: Faster scans when using service filtering
- **Report Quality**: Reduced duplication with intelligent grouping
- **API Resilience**: Smart retry logic for throttling scenarios

## [2.1.0] - 2025-10-15

### Added
- **Cost Optimization Hub Integration**: AWS native recommendations
- **Compute Optimizer Integration**: ML-powered rightsizing suggestions
- **Multi-Service Analysis**: Expanded service coverage
- **Regional Pricing**: Accurate cost calculations per region

## [2.0.0] - 2025-09-10

### Added
- **Multi-Service Support**: Expanded beyond EC2 to storage, database, networking
- **Professional Reporting**: HTML reports with cost breakdowns
- **Regional Support**: Multi-region analysis capabilities
- **Advanced Filtering**: Service-specific optimization checks

### Breaking Changes
- **Configuration Format**: Updated command-line interface
- **Report Structure**: New HTML-based output format
- **API Requirements**: Additional IAM permissions for expanded services

## [1.0.0] - 2025-08-01

### Added
- **Initial Release**: Basic AWS cost optimization scanning
- **EC2 Analysis**: Instance rightsizing and idle detection
- **S3 Analysis**: Storage class optimization
- **Basic Reporting**: Text-based output format
- **Core Features**: Foundation for multi-service expansion

---

## Migration Guide

### From 2.4.x to 2.5.0
- **Container Insights**: Enable for ECS/EKS clusters to get metric-backed recommendations
- **IAM Permissions**: Update policy to include CloudWatch metrics access
- **Regional Pricing**: Review cost estimates with updated pricing constants

### From 2.3.x to 2.4.0
- **Service Filtering**: Use --scan-only for faster, targeted scans
- **Fast Mode**: Add --fast flag for large S3 environments
- **Error Handling**: Review permission issues in enhanced error output

### From 2.x to 2.3.0
- **IAM Policy**: Update to include permissions for supported services
- **Regional Support**: Verify region-specific pricing calculations
- **Report Format**: Transition to new HTML report structure

## Support

For questions about specific versions or migration assistance:
- **Documentation**: See README.md and ARCHITECTURE.md
- **Issues**: Report bugs via GitHub Issues
- **Discussions**: Join GitHub Discussions for community support
