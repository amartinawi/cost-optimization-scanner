# LOW Cost-Correctness Remediation Prompt

A single, code-grounded cleanup brief for the **120 LOW findings** surfaced by the all-services cost audit (`docs/audits/UNIFIED_AUDIT_FINDINGS.md`). These are polish / coverage-edge / doc-drift / latent-desync items — **not adversarially verified**: each was captured from a single audit pass, so **confirm the cited `file:lines` and reproduce the defect before changing anything**. Paste the **PROMPT** section into a fresh session. *(Includes `network` NET-03…07, added after the `NetworkModule` gap-audit.)*

Scope is **strictly cost**: every emitted recommendation must produce a concrete, account-specific dollar saving. A finding that cannot be quantified from evidence becomes a **$0 advisory** (`Counted=False`, rendered-not-counted) — never a fabricated counted dollar. Most LOW items are doc/render/coverage cleanups with **no live dollar impact today**; many are *latent* behind a CRITICAL/HIGH fix in the same adapter and should be folded into that commit.

---

## PROMPT (copy from here)

You are clearing the **120 LOW cost-correctness cleanup items** in this AWS Cost Optimization Scanner, grouped **by service (alphabetical)**. Each row gives exact `file:lines`, the defect (one phrase), and the fix (one phrase). These are backlog-grade: low risk, mostly doc/render/coverage/latent-desync.

**Caveat — not adversarially verified.** Unlike the CRITICAL/HIGH briefs, these findings were not re-verified against the live code or the Pricing API. **Re-read the cited `file:lines`, reproduce the defect, and validate any rate live (AWS Pricing MCP) before editing.** If a finding does not reproduce, drop it and say so.

### Global rules (short)

1. **Advisory-$0 is the default remedy** for any saving you cannot quantify from evidence: mirror `services/adapters/lambda_svc.py` — `Counted=False`, `EstimatedMonthlySavings=0.0`, honest `EstimatedSavings` string. Use `services/_savings.mark_zero_savings_advisory` where it fits.
2. **Classify, don't swallow.** Replace `except: pass` / `except: continue` / logger-only paths with `services/_aws_errors.record_aws_error(ctx, e, …)` (`AccessDenied`/`UnauthorizedOperation`/`OptInRequired` → `ctx.permission_issue`, else `ctx.warn`).
3. **Counted == rendered.** The number summed into `total_monthly_savings` must equal the dollar shown on the card. Kill every string-vs-number desync.
4. **Immutability** — build new rec dicts; never mutate shared inputs.
5. **Tests + regression gate.** Where a fix changes logic (not pure docs), extend `tests/test_<svc>_audit_fixes.py` (mirror `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`). Keep green: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`. LOW items should **not** shift the golden/reporter snapshots — if one does, you have a real dollar change; stop and re-classify it.
6. **Stage only the files you changed.**

### Execution note

**Batch by service; low risk.** For any service with an open CRITICAL/HIGH/MEDIUM fix, **fold its LOW rows into that touching commit** (same code path, same review). Pure doc-only rows (`services/adapters/CLAUDE.md`, `core/CLAUDE.md`, docstrings) may be swept into one "docs reconcile" commit at the end. Do the shared-pattern cross-references (below) under their CRITICAL owner and just check them off here.

### Shared-pattern cross-references (do under the CRITICAL owner, check off here)

- **opensearch L1** (`core/pricing_engine.py` `MaxResults=1` / no operation pin) — **covered under CRITICAL SR-1**; OpenSearch is deterministic in practice (one SKU per `instanceType`), so the SR-1 fix plus the legacy `.elasticsearch`→`.search` normalization closes it. Check off there.
- **s3 S3-N2** (orphaned `cost_hub_splits['s3']`) — **mirror CRITICAL SR-3 pattern** (orphaned Cost-Hub bucket): either drop the dead wiring or consume + dedup CoH > heuristic by normalized bucket id.
- **api_gateway L1 / step_functions L4** doc rows reference the `_FLAT_SAVINGS_SERVICES` flat-$50 path — **update only after CRITICAL SR-2 removes it**.
- **elasticache L1 / opensearch L2** doc rows describe pricing/CoH behavior that **CRITICAL SR-1/SR-3 + ElastiCache C1/C2 / OpenSearch C1** change — reconcile docs after those land.

---

## PER-SERVICE BACKLOG

### ami
Doc/render polish only; counted==rendered holds today. Fold into the AMI HIGH/MEDIUM commit (`ami C1` silent-failure fix).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/CLAUDE.md:Parse-rate table row 'ami.py | parse_dollar_savings()'` | Doc lists ami.py as parse-rate, but adapter sums the float `EstimatedMonthlySavings` (`parse_dollar_savings` never called) | Move ami.py out of Parse-rate; describe as field-extraction summing the monthly float (snapshot GB × EBS snapshot rate) |
| L2 | `html_report_generator.py:2416-2424,2711-2745` | AMI renders via dedicated `_get_amis_content`, whose age buckets start at "90-180 days"; 31-90-day unused AMIs mislabeled | Add a "30-90 days" bucket to `age_groups`; correct the brief's Phase 0c note (uses `_get_amis_content`, not generic per-rec) |
| L3 | `html_report_generator.py:2588-2612,2449-2453` | `_extract_amis_data` merges EC2 `'ami'` CheckCategory recs into the panel total but strip/headline use AMI-service total only — latent counted-vs-rendered desync | Drop the EC2 `'ami'` merge (AMI is now its own adapter), or compute strip/headline from the same merged+deduped set |

### api_gateway
Doc-only; dead config. The flat-$50 mechanism is removed under CRITICAL SR-2.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/api_gateway.py:13, 28-33 (and services/adapters/CLAUDE.md 'Parse-rate (5 adapters)' row)` | Docstrings/CLAUDE.md claim "Keyword-rate" + "REST/HTTP"; reality is REST-only, CloudWatch-Count request-delta + reporter flat-$50 | Restate as REST-only, CloudWatch-Count delta at $2.50/M first-tier, plus the `_FLAT_SAVINGS_SERVICES` path (removed per SR-2) |
| L2 | `services/api_gateway.py:18-44 (29-33)` | `API_GATEWAY_OPTIMIZATION_DESCRIPTIONS` are never emitted; `caching_opportunities` text is wrong-direction (cache is a $-adder) | Delete the four never-rendered description entries (at minimum `caching_opportunities`) |

### apprunner
All four are **latent behind apprunner C1** (CRITICAL Cluster A — dead shim, $0/0 recs). Fix alongside C1 in one commit.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/apprunner.py:94-98` | Dual-billing model adds full-month provisioned memory **plus** active-hours memory → overstates basis by `mem_gb×0.007×active_hours` | Bill provisioned memory only for non-active hours: `mem_gb×0.007×(730−active_hours)` |
| L2 | `services/adapters/apprunner.py:83-93` | Malformed `InstanceConfiguration` silently priced at a 1 vCPU/2 GB default | On parse failure `ctx.warn` and emit $0 advisory (`Counted=False`), not a priced default |
| L3 | `services/adapters/CLAUDE.md:apprunner.py row under 'Live Pricing (19 adapters)'` | CLAUDE.md misclassifies App Runner as Live Pricing with a "×730" formula it does not use | Move to a Module-constant grouping; restate provisioned + active dual-billing formula and the 160-hour assumption |
| L4 | `services/apprunner.py:41-51` | `describe_service` result fetched then discarded (`_ = instance_config`) — wasted API call, pricing data never wired | Attach `InstanceConfiguration` (Cpu/Memory) to an emitted rec, or remove the call until a check consumes it |

### athena
Both moot until **athena C1** (CRITICAL Cluster A — dead shim) re-wires recs; fix together.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/athena.py:32-45` | Inner `except Exception: continue` swallows denied/throttled `get_work_group` with no record | Classify: AccessDenied/Unauthorized → `ctx.permission_issue('athena','athena:GetWorkGroup')`, else `ctx.warn` |
| L2 | `reporter_phase_b.py:1865-1876` | Generic renderer styles savings only `if "EstimatedSavings" in rec`; adapter writes `EstimatedMonthlySavings` float → styled line won't render once recs exist | Have adapter also set `EstimatedSavings=f"${s:.2f}/month"` (or add Athena branch); ensure $0 advisories render their `PricingWarning` |

### aurora
Independent of aurora C1 (Cluster B). L2 is a silent-failure class item.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/aurora.py:340-341` | `_check_io_tier` skips silently in fast_mode (no warn), unlike the rightsizing path | Thread `ctx` in; emit one `ctx.warn` on the fast_mode skip |
| L2 | `services/adapters/aurora.py:451-453` | Per-cluster failure recorded via `logger.warning` only, never `ctx.warn` | Replace with `ctx.warn(..., 'aurora')` |
| L3 | `services/adapters/aurora.py:38,195,217,250-251` | ACU rate + instance pricing not storage-mode-aware → I/O-Optimized clusters understated (conservative) | Detect `aurora-iopt1` StorageType; select I/O-Optimized ACU rate and pass `aurora_io_optimized=True` |
| L4 | `services/adapters/aurora.py:408` | `GroupingSpec(by='check_category')` references a key recs never set (harmless; grouping unused) | Align spec to an existing key (`by='source'`) or expose `check_category` |
| L5 | `services/adapters/aurora.py:1-9,440-447` | Docstring advertises clone/snapshot/Global-DB/backtrack checks that scan() never implements | Trim docstring to implemented checks or mark others TODO |

### batch
Doc + dead-code + render-header polish. Fold into the batch CRITICAL commit (`batch C1`).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/batch.py:30-31; CLAUDE.md Flat-rate row` | Docstring says Graviton=0.10 (code uses 0.20) + claims a `pricing_multiplier` the code drops; CLAUDE.md mislabels as Flat-rate | Fix to Graviton=0.20, remove multiplier claim; relabel CLAUDE.md row "EC2-derived: get_ec2_hourly_price×730×factor" |
| L2 | `services/adapters/batch.py:11` | `BATCH_COMPUTE_FALLBACK_MONTHLY=150.0` is unreachable dead code (single grep hit) | Remove the constant (or wire a real region-scaled fallback; never the flat $150) |
| L3 | `reporter_phase_b.py:1815-1863` | `_render_generic_other_rec` resource_id chain lacks CE/job-def name → card header reads "…: Resource" | Add `rec.get("ComputeEnvironmentName") or rec.get("JobDefinitionName")` to the chain |

### bedrock
Mostly render/doc; the PT analysis itself is dead until **bedrock C1** (Cluster A).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `reporter_phase_b.py:1812-1876 (id resolution 1845-1847; key dump 1865-1869)` | snake_case rec keys vs PascalCase renderer → card header "…: Resource", `monthly_savings` dumped unstyled (money total correct) | Add a `('bedrock', <source>)` PHASE_B handler, or normalize rec keys + expose `EstimatedSavings` |
| L2 | `services/adapters/bedrock.py:315-319 (vs html_report_generator.py 156-162)` | Module `stat_cards` (Idle PTs / Monthly Savings) never rendered; HTML descriptor uses different cards | Drive stat cards from one source of truth; reconcile module spec vs descriptor |
| L3 | `services/adapters/CLAUDE.md:12-33 (Live-Pricing table), 34-48` | No bedrock row in the pricing-models tables (undocumented module-constant adapter) | Add a module-constant bedrock row (PT_HOURLY_PRICE + token rate; no CoH/CO; CloudWatch-gated; 4 generic-per-rec sources) |
| L4 | `services/adapters/bedrock.py:147-150,186-207` | Idle gate fires only at exactly `invocations==0`; breakeven "switch to on-demand" ignores `commitmentDuration`/`commitmentExpirationTime` | Add a low-utilization threshold; gate/annotate the breakeven rec by commitment term (advisory until expiry) |

### cloudfront
All $0/coverage today (cloudfront H2 is "revised" in CRITICAL follow-ups).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `reporter_phase_a.py:136,143-144; services/cloudfront.py:70` | Extractor reads `'PriceClass'` but rec key is `'CurrentPriceClass'` → current price class never shown on card | Read `rec.get('CurrentPriceClass')` (fallback `'PriceClass'`) in `_extract_cloudfront_details` |
| L2 | `reporter_phase_b.py:2366-2367, 2377-2379` | No `(cloudfront,enhanced_checks)` override → $0 config-pattern rec falls through to green "Metric Backed" badge | Add `('cloudfront','enhanced_checks') -> 'Audit Based'` to `SOURCE_TYPE_MAP` (S3 precedent) |
| L3 | `services/cloudfront.py:46, 64` | Coverage gated to `PriceClass_All` + >1000 req/week + Enabled only | When quantification re-enabled, extend to `PriceClass_200→100`, lower/justify threshold, document disabled-distribution exclusion |

### commitment_analysis
All advisory (no headline impact); doc + coverage-breadth + one missing warn.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/commitment_analysis.py:110-112` | Missing CE client returns empty findings with no warn/permission_issue | Emit `ctx.warn`/`ctx.permission_issue` before `_empty_findings()` |
| L2 | `services/adapters/commitment_analysis.py:76, 282` | `AVG_SP_DISCOUNT_RATE=0.30` is a flat factor presented as a concrete figure, unlabeled estimate | Label as estimate and/or derive per-service discount from the live offering-rate matrix already fetched for Fargate |
| L3 | `services/adapters/commitment_analysis.py:80-85 vs html_report_generator.py:178-184` | Adapter `stat_cards` tuple diverges from the HTML descriptor that actually renders (RI Coverage vs Monthly Savings) | Reconcile to a single source of truth for the four cards |
| L4 | `services/adapters/commitment_analysis.py:6-16, 69` | Docstring claims ~7 CE calls/$0.07 but scan() issues ~20 calls/~$0.20 | Update docstring to the real ~12-call purchase matrix + account-coverage + Fargate cost-and-usage (~$0.20/scan) |
| L5 | `services/rds_logic.py:16-25, 79; core/scan_orchestrator.py:104-112` | Same RDS RI opportunity shows as advisory card in both RDS tab and commitment_analysis tab (no double-count, both excluded) | Suppress the RDS-tab heuristic RI card when a CoH RI rec routes here, or add a cross-reference note |
| L6 | `services/adapters/commitment_analysis.py:368-393, 408-448, 478-485, 728-767` | Coverage gaps: empty RI coverage, SP-only expiry, EC2/Compute-SP-only purchase matrix, EKS-Fargate excluded | Document as deliberate limitations (or add RI expiry, broaden `_SP_TYPES`/`_RI_SERVICES`, add EKS to `_fargate_legs`) |

### containers
Render/doc/count-precision only (no savings-path impact).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `reporter_phase_b.py:944-973, 983-992` | Group key "ECR Lifecycle Missing" ≠ emitted CheckCategory "ECR Lifecycle Management"; three dead grouping branches | Rename group key to match; remove EKS / Unused-ECS / Container-Insights dead branches |
| L2 | `services/containers.py:224` | `get_ecr_analysis` uses un-paginated `describe_repositories` → repo count under-reports >100 repos (cosmetic) | Use `ecr.get_paginator('describe_repositories')` (match the reclaim path) |
| L3 | `services/containers.py:241-243` | ECR image-count failure logged via `logger` only, not `ctx` | Route through `_ecr_failure(ctx, …)` so AccessDenied→permission_issue |
| L4 | `services/adapters/CLAUDE.md:containers.py row (Live Pricing table)` | Row says "Module constants" and omits CoH+CO (ECS) sources; pricing is actually live PricingEngine | Restate as live Fargate/ECR via PricingEngine (FALLBACK on failure); note CoH + CO (ECS) consumption |

### dms
Fold into the DMS commit alongside CRITICAL dms C1/C2/C3 (Clusters C/D).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/dms.py:48` | `savings += monthly*0.35` is a flat reduction factor, no target class (string asserts "~35%") | Pick a concrete target (one size down / Single-AZ) and count `current−target`, or demote to $0 advisory; record `AuditBasis` |
| L2 | `services/dms.py:115-124` | `describe_replication_configs` paginator wrapped in `except Exception: pass` (serverless permission gap swallowed) | Classify via `ctx.warn`/`ctx.permission_issue`; or remove the empty serverless paginator |
| L3 | `services/dms.py:126-127` | Outer `except` records generic `ctx.warn`, never classifies AccessDenied as permission_issue | Classify via `services/_aws_errors.record_aws_error` |
| L4 | `services/dms.py:54` | `status=='available'` gate skips billable `modifying`/`storage-full`/`upgrading` instances | Broaden to all billable states (exclude only creating/deleting/failed); rely on metric/price evidence |
| L5 | `services/dms.py:84,96; reporter_phase_a.py:450-454` | Card shows `resources[0]` prose ("Full instance cost" / "~35%") while headline sums `×0.35`/`×0.5` → card never shows counted number | Render per-rec `EstimatedMonthlySavings` on the card so counted==rendered (mirror Lambda AuditBasis) |

### dynamodb
Fold into the DynamoDB CRITICAL commit (`dynamodb C1`, Clusters B/D).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/dynamodb.py:114-130` | $0 "enable monitoring" nudges classified as **counted** (never sets `Counted=False`) → inflates "N counted" | Call `mark_zero_savings_advisory(...)` (or set `Counted=False`) on the $0 categories |
| L2 | `services/dynamodb.py:294-304` | Empty PROVISIONED table deletion saving is $0 (string says "100% of table costs"); empty high-RCU/WCU table gets rightsize/reserve instead | Attach provisioned RCU/WCU to the unused rec (factor 1.00 = full cost); suppress over-provisioned/reserved when `ItemCount==0` |
| L3 | `services/adapters/dynamodb.py:141-147` | CoH ARN→table dedup breaks on index ARNs (`split('/')[-1]` yields GSI); Global Tables/storage unmodeled | Parse `resource_id.split(':table/')[-1].split('/')[0]`; document Global Tables/storage as gaps |
| L4 | `services/dynamodb.py:291-292` | `table_analysis` doesn't skip non-ACTIVE tables (enhanced does); `ItemCount==0` staleness can mis-flag active tables | Apply `TableStatus=='ACTIVE'` gate in both shims; corroborate `ItemCount==0` with recent `ConsumedWriteCapacityUnits` |

### ebs
EBS dollar paths are sound (memory: EBS CoH fixed); these are dedup/efficiency/AuditBasis/coverage polish.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/ebs.py:61-100` | `_drop_stale_delete_recs` only re-validates `delete` CoH actions; Rightsize/Upgrade pass unverified | Optionally re-validate non-delete CoH recs (volume still exists/matches), or document the CoH lag in the card basis |
| L2 | `services/ebs.py:557; services/adapters/ebs.py:161` | `get_unattached_volumes` invoked twice/scan; `compute_ebs_checks` copy discarded by partition → wasted describes + latent double-count surface | Stop seeding `unattached_volumes` in `compute_ebs_checks` (adapter owns it), or reuse one result |
| L3 | `services/adapters/ebs.py:198-199; reporter_phase_b.py:409-437` | CoH/CO recs lack structured `AuditBasis`; `_render_ebs_compute_optimizer` prints no $ though CO savings hit headline | Attach `AuditBasis` to CoH/CO recs; render per-finding $ in `_render_ebs_compute_optimizer` |
| L4 | `services/ebs.py:464-472, 403-411` | gp3 throughput-only over-provisioning and st1/sc1 over-provisioning unpriced ($0 missed savings) | Add gp3-throughput rightsizing (gated on `VolumeThroughputPercentage`); document st1/sc1 exclusion in CLAUDE.md |

### ec2
EC2 dollar deltas are the reference pattern; these are render-lockstep/precision/coverage edges.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/ec2.py:20-34` | `_coh_is_renderable` omits two render-time skips (`'actionType' not in rec`, `finding=='optimized'`) → counted-but-not-shown | Add both predicates to `_coh_is_renderable` so source filter and renderer stay in lockstep |
| L2 | `services/ec2.py:317-333` | Linux price fallback understates non-Linux (Windows ~2× Linux) savings when OS-specific SKU misses (conservative) | On non-Linux miss `ctx.warn` (Linux lower bound) or skip; document the fallback in `PricingBasis` |
| L3 | `services/ec2.py:847-936` | ASG `oversized_instances` uses 0.40 factor, no `>0` guard ($0 cards), no EKS-ASG/Spot exclusion (consumed advisory by network) | Add `if asg_node_savings>0` guard; apply `_is_eks_nodegroup_asg`; compute exact one-size-down delta or keep explicitly advisory |
| L4 | `services/ec2.py:112-121` | `_INSTANCE_STORE_FAMILIES` omits newest store families (i7ie, i8g, trn/inf local-store) → store-check missed | Refresh family set against current AWS instance-store list; add periodic-review note |

### eks_cost
Advisory/doc with one missing AuditBasis; latent double-count if `Counted` ever flips.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/eks.py:288-303` | `failed_cluster` counted rec lacks `audit_basis` (extended_support/idle_cluster have it) | Add `audit_basis` (rate/unit/formula/evidence "cluster.status == FAILED") |
| L2 | `services/adapters/eks.py:423-426` | `_node_group_monthly_cost` `except → 0.0` silently drops the advisory (hits `ng_monthly<=0` skip) | Make the lookup miss observable; optionally `ctx.warn` on the except |
| L3 | `services/adapters/CLAUDE.md:Live Pricing table (the 19-adapter list)` | No eks.py row despite live PricingEngine usage + CoH consumption | Add an eks.py row (live control-plane + Extended-Support; node-group/Fargate advisory; consumes `cost_hub_splits["eks_cost"]`; no CO) |
| L4 | `services/adapters/eks.py:361-401` | Spot (×0.70) AND Graviton (×0.20) advisory both emitted per node group — latent double-count if ever counted | Keep advisory + mutually-exclusive; if ever counted use max, never sum |

### elasticache
Doc drift relates to SR-1/C2 (engine-aware pricing). Reconcile after those land.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/CLAUDE.md:32; core/CLAUDE.md PricingEngine table row 'get_elasticache_node_monthly_price(engine, node_type)'` | Both docs cite `get_elasticache_node_monthly_price()` which does not exist (adapter uses `get_instance_monthly_price`); signature falsely implies engine-awareness | Update both rows to `get_instance_monthly_price('AmazonElastiCache', node_type)`; note engine-awareness is a gap (C2), not a feature |
| L2 | `services/elasticache.py:60-119` | ElastiCache Serverless never enumerated; Reserved-Nodes trigger (`NumCacheNodes>=2`) effectively only fires for Memcached | Document Serverless as out-of-scope (or add `describe_serverless_caches`); base Reserved advisory on `describe_replication_groups` node counts |

### file_systems
CoH/CO do not cover EFS/FSx (memory note) — savings are local. These are guard/parse-safety/pagination/precision items; no live $0 today.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/efs_fsx.py:319-333` | Idle-EFS counted branch lacks the `if savings > 0` guard the other three counted branches have (latent $0-counted) | Wrap the append in `if savings > 0:` for parity and the "$0-counted is a bug" invariant |
| L2 | `services/efs_fsx.py:422-425` | Advisory gross/One-Zone strings carry a parseable `$X/month`; counted-safety relies only on SourceBlock segregation (fragile) | Drop the `/month` unit from indicative strings, or route advisories through `mark_zero_savings_advisory` |
| L3 | `services/efs_fsx.py:225,569` | FSx `describe_file_caches` is unpaginated and called twice | Paginate with NextToken and read once (mirror `describe_file_systems`) |
| L4 | `reporter_phase_a.py:92-101` | Render sums full-precision `_savings` while headline parses the 2-decimal string → cents-level divergence across many FS | Have `_fs_savings` and headline read the same value (`round(_savings, 2)` in both) |
| L5 | `reporter_phase_b.py:1368-1369,1584` | Dead Phase-B renderer + resource extractor for file_systems (unreachable; live renderer is `render_file_systems`) | Remove the dead `_render_generic_file_systems_rec` branch + `_extract_file_systems_resources`, or comment them dead |
| L6 | `services/file_systems_logic.py:59-76` | `cold_gb` subtracts a 30-day cumulative IO **flow** from a point-in-time **size** (dimensionally off, but strictly conservative) | Acceptable as-is for cost-safety; if higher recall wanted, gate "hot" on a per-file access metric |

### glue
Fold into the Glue CRITICAL commit (`glue C1/C2`).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/glue.py:26-76` | One broad `try` spans get_jobs/get_dev_endpoints/get_crawlers; a denial aborts the rest and is only `ctx.warn`, never permission_issue | Wrap each API call separately; route via `record_aws_error` (AccessDenied/Unauthorized/OptInRequired → permission_issue) |

### lambda
Lambda is the reference pattern; these are coverage/render/pagination edges (all advisory $).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| M2 | `services/lambda_svc.py:47-60, 247` | `ARM_SUPPORTED_RUNTIMES` allowlist drift (omits python3.13/nodejs22/al2023/dotnet9) + container-image exclusion → ARM nudge skipped (no $ — advisory) | Broaden the allowlist (+container branch), or flip to a known-x86-only exclusion allowlist |
| L1 | `reporter_phase_b.py:2362, 2448; html_report_generator.py:3311-3313` | Dead `('lambda','compute_optimizer')` PHASE_B handler + source-label (lambda renders via Phase A) | Remove the two dead bindings or annotate intentional; no snapshot change |
| L2 | `reporter_phase_a.py:450-451` | Counted PC card renders prose `EstimatedSavings` ("Up to 90%…"), not the computed account-specific dollar | Render `EstimatedMonthlySavings` for counted PC recs (intentional reporter-snapshot refresh) |
| L3 | `services/lambda_svc.py:206-213` | `list_provisioned_concurrency_configs` read without pagination → many aliases/versions under-counted | Loop on `NextMarker` (or `get_paginator`) |
| L4 | `services/lambda_svc.py:189-200; services/adapters/lambda_svc.py:148-200` | Excessive-Memory + ARM structurally $0 advisory (no Duration/GB-seconds collected) — documented honesty choice | OPTIONAL: wire Duration×Invocations×mem_gb to price the exact memory delta (metric-gated); else keep advisory |

### lightsail
Fold into the Lightsail CRITICAL commit (`lightsail C1`, Clusters B/F).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/lightsail.py:88-89` | `get_static_ips()` single un-paginated call (returns `nextPageToken` never followed) → large accounts truncate | Loop on `pageToken` until exhausted (mirror `get_instances` paginator) |
| L2 | `services/adapters/lightsail.py:51; services/lightsail.py:98` | Flat us-east-1 bundles region-scaled by the EC2-derived `pricing_multiplier` (wrong curve); `$3.65` IP string never region-scaled | Use real per-region Lightsail price (or document the EC2-multiplier approximation in `AuditBasis`); region-scale the IP rate when counted |
| D1 | `services/adapters/CLAUDE.md:Live Pricing table, lightsail.py row` | Row claims live AWS Pricing API; adapter abandoned that dead path and prices from hardcoded `_BUNDLE_COSTS` | Move to a "bundle-dict / flat-rate" description (`get_lightsail_bundle_cost` × `pricing_multiplier`; source = module dict, NOT live) |

### mediastore
Fold into the MediaStore CRITICAL commit (`mediastore C1`, Cluster C). MediaStore is retired (low real risk).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/mediastore.py:29-31` | `list_containers` not paginated — NextToken ignored | Loop on NextToken (or `get_paginator('list_containers')`) |
| L2 | `services/mediastore.py:36, 95` | Only fully-idle (`total_activity==0`) ACTIVE containers fire; large cold-storage + low usage skipped | Optionally add an under-utilization/cold-storage advisory (MediaStore IA $0.0125/GB-mo), evidence-gated like S3 IA |
| L3 | `services/adapters/mediastore.py:41-49 (core/pricing_engine.py:668-680)` | Borrowed S3-Standard rate is correct ($0.023) but always assumes Standard; MediaStore IA ($0.0125) overstates ~1.84× | Document the Standard-class assumption in `AuditBasis` (storage class unknowable from CloudWatch); no code change |
| L4 | `services/mediastore.py:41-42, 75-92, 95` | Ingest cost is structurally always $0 in emitted recs (fires only at `total_activity==0`) — dead term | Remove the dead ingest computation (or reuse it if an under-utilization check is added per L2) |

### monitoring
Dead-cost walk + tier model + fast-mode + render/doc polish.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/monitoring.py:206-273` | CloudTrail walk emits nothing yet still calls `describe_trails` (un-paginated) + `get_event_selectors` per trail; errors logger-only | Delete the CloudTrail walk (no cost rec) or gate behind a real priced check + paginate + route errors through `ctx` |
| L2 | `services/monitoring.py:31-51` | Custom-metrics 4th tier ($0.02 above 1M) not modeled → cost($2M)=$114,500 modeled vs $84,500 true (edge) | Add the 1,000,000 breakpoint at $0.02 to `_cw_custom_metrics_monthly_cost` |
| L3 | `services/adapters/monitoring.py:22-26` | `reads_fast_mode` not declared and no shim gates on `ctx.fast_mode` → full describes under `--fast` | Declare `reads_fast_mode=True` and short-circuit `list_metrics`/`describe_alarms` (optionally log groups) when fast_mode |
| L4 | `reporter_phase_b.py:1223-1233` | Per-category card shows only `resources[0]['EstimatedSavings']`; multi-resource categories understate; advisory % under "savings" label | Render per-category sum of counted `EstimatedMonthlySavings`; label advisory cards |
| L5 | `services/adapters/CLAUDE.md / reporter_phase_a.py:337-354` | CLAUDE.md omits the 4-domain composite (CW+CloudTrail+Backup+Route53); dead `backup`/`route53` Phase-A descriptors | Update CLAUDE.md row; remove the inert backup/route53 Phase-A descriptors + service-order entries |

### msk
Fold into the MSK CRITICAL commit (`msk C1`, Cluster B).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/msk.py:40,56 + core/pricing_engine.py:728-736` | Engine-None path emits counted-zero recs (no warn); EC2 fallback miss returns region-blind hardcoded `0.15` (vs the region-scaled `0.10` storage literal) | Mark recs $0 advisory (`Counted=False`) and/or `ctx.warn` when pricing unavailable; region-scale the `0.15` last-ditch constant |
| L2 | `services/msk.py:72-79 + services/adapters/msk.py:35` | Express brokers, MSK Serverless (DCU), Tiered storage un-priced (`list_clusters_v2` discarded) | Document the gap in CLAUDE.md; optionally emit a $0 advisory naming the un-priced cluster classes |

### network
Fold into the `network` NET-01 (HIGH) load-balancer commit and NET-02 (MEDIUM) VPC-endpoint commit where the code path overlaps. `network` = NetworkModule (`services/adapters/network.py`), distinct from `network_cost` below.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| NET-03 | `services/elastic_ip.py:65-82` (`eips_on_stopped_instances`), `84-96` (`multiple_eips_per_instance`) | A stopped instance holding >1 EIP is counted in both checks → each EIP's $3.65 attributed twice | Exclude `instance_ids` already emitted by `eips_on_stopped_instances` from the `multiple_eips_per_instance` loop (count each EIP once) |
| NET-04 | `services/ec2.py:911-913, 925-929` (`get_auto_scaling_checks`) | ASG sub-shim catches with `ctx.warn` directly, bypassing `record_aws_error` → AccessDenied misclassified as a warn (the 5th sub-shim out of step with the other 4) | Route the except blocks through `services/_aws_errors.record_aws_error(ctx, e, service=..., context=...)` so permission errors land on `ctx.permission_issue` |
| NET-05 | `services/vpc_endpoints.py:36-42` (checks dict); `load_balancer.py:73-83` (`zero_traffic_albs`) | Declared `unused_interface_endpoints` / `no_traffic_endpoints` / `zero_traffic_albs` categories are never populated; "Unused VPC endpoints" description is unbacked | Implement a CloudWatch-gated idle check (advisory if fast_mode/no metrics, counted on proven 0 traffic) or remove the dead categories and trim the descriptions |
| NET-06 | `services/nat_gateway.py:125-148` (`nat_for_aws_services`) vs `services/vpc_endpoints.py:71-93` (`missing_gateway_endpoints`) | The missing-S3/DDB-gateway-endpoint advisory is emitted by two sub-shims for the same VPC (duplicate noise) | Consolidate into one sub-shim (`vpc_endpoints.py` already enumerates all VPCs); drop `nat_for_aws_services` or cross-reference so each VPC is reported once |
| NET-07 | `core/pricing_engine.py:1539-1548` (`_fetch_vpc_endpoint_price`) | Interface-endpoint price fetch not constrained to the hourly dimension — relies on per-GB == per-hour coincidence | Switch to `_call_pricing_api_hourly` (or add a `usagetype.endswith('VpcEndpoint-Hours')` / `unit=='Hrs'` guard) so it always selects the hourly endpoint SKU |

### network_cost
Fold into the network_cost CRITICAL commit (`network_cost C1`, Cluster G).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `reporter_phase_b.py:2397` | `tgw_vs_peering` badged "Metric Backed" (it's a CE/heuristic estimate); 3 other sources unbadged; stat-card descriptors diverge and drop Monthly Savings | Re-label CE-derived sources "Audit Based" (add to `SOURCE_TYPE_MAP`); reconcile stat-card descriptors to keep Monthly Savings |
| L2 | `services/adapters/network_cost.py:129-133, 212-228` | Cross-adapter overlap with `network` (NAT) — no shared `covered` set; scope limited to "AWS Data Transfer"; negative-cost rows skipped | Document single-owner rule (network_cost owns transfer bytes, network owns NAT-hours); add shared `covered` set if network ever counts per-GB NAT |

### opensearch
L1 covered under CRITICAL SR-1; L2 doc reconciles after SR-3.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `core/pricing_engine.py:1290-1298, 1601` | `_fetch_generic_instance_price` filters only instanceType+location, `MaxResults=1`, no `operation=ESDomain` pin; legacy `.elasticsearch`-suffixed types price to $0 (deterministic for OpenSearch in practice) | **Covered under CRITICAL SR-1** — additionally pin `operation='ESDomain'` and normalize `.elasticsearch`→`.search` before lookup; check off there |
| L2 | `services/adapters/CLAUDE.md:Parse-rate table row 'opensearch.py | Keyword-based'` | Doc files opensearch as pure Keyword-based; reality is live AmazonES instance pricing + storage constant + CoH (SR-3) | Restate as "hybrid keyword + live AmazonES instance pricing + gp3 storage constant"; note CoH consumption once C1 lands |

### quicksight
Fold into the QuickSight CRITICAL commit (`quicksight C1`, Cluster E).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/quicksight.py:71` | Rec `'Edition'` is `PurchaseMode` (defaults ENTERPRISE), not billing edition — mislabels the card | Set `Edition` from `describe_account_subscription().AccountInfo.Edition`; rename the PurchaseMode field if shown |
| L2 | `services/adapters/quicksight.py:51-58` | $0-advisory branch sets `PricingWarning` but not `Counted=False` → would tally as counted if reached (latent) | Set `rec['Counted']=False` on the $0/PricingWarning path |
| L3 | `services/quicksight.py:63` | Hard 50%-idle trigger excludes accounts at <50% idle but materially idle | Treat 50% as a flag heuristic; emit a (possibly advisory) rec for any meaningful idle, saving still from measured unused GB |

### rds
RDS is a reference pattern; one advisory rate bug + two coverage-consistency items.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/rds.py:490-495` | Backup-retention advisory calls `get_rds_backup_storage_price_per_gb()` with no engine → always $0.095/GB-mo even for Aurora ($0.021, ~4.5× too high; advisory only) | Pass the instance engine (mirror the snapshot `_backup_rate` path at 106-115) |
| L2 | `services/rds.py:547-551` | Scheduling gate uses DB-name substring only; ignores the Environment tag the Multi-AZ check honors | Reuse the tag-aware non-prod resolution (401-435) for the scheduling gate |
| L3 | `services/rds.py:394-456` | Multi-AZ-disable check lacks the Aurora-engine exclusion scheduling has → could mis-price Aurora members + feed H1 overlap | Add `and not (engine or '').startswith('aurora')` to the Multi-AZ trigger |

### redshift
Fold into the Redshift commit alongside CRITICAL redshift C1 (SR-3) / C2 (SR-1).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/redshift.py:31-73` | Config-only savings, no CloudWatch — RI gate is age>30d AND nodes>=2 (no utilization signal) | Gate counted RI/rightsizing on CPU/query utilization (declare `requires_cloudwatch`/`reads_fast_mode`); attach structured `AuditBasis` |
| L2 | `services/adapters/redshift.py:11, 17-21, 51-57` | Dead constants/branches: `REDSHIFT_NODE_MONTHLY_FALLBACK`, pause 1.00 factor, default 0.24, two empty buckets | Delete the dead constants/factors/buckets, OR implement a real paused-cluster (`ClusterStatus=='paused'`) RMS check |
| L3 | `services/redshift.py:46-53` | `ClusterCreateTime` string-fallback hardcodes age=30 → `30>30` False → RI gate never fires on that path (under-counts) | Parse the ISO string to a datetime (or use `ClusterCreateTime` directly) |
| L4 | `reporter_phase_b.py:1815-1861` | RI/rightsizing recs key `ClusterIdentifier`, but resource_id chain omits it → cards titled "…: Resource" | Add `or rec.get('ClusterIdentifier')` to the chain (or a dedicated Redshift handler) |

### s3
S3-N2 mirrors SR-3. Others are latent render/parse-safety + pagination.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| S3-N2 | `core/scan_orchestrator.py:52-60, 86-95, 144` | `cost_hub_splits['s3']` populated but no S3 code reads it; `'S3Bucket'` in type_map suppresses the unbucketed-type warning → silent drop if AWS adds S3 CoH | **Mirror SR-3**: drop the dead wiring (`'s3'` from `_HUB_SERVICES`, `'S3Bucket'` from type_map), OR consume + dedup CoH > heuristic by normalized bucket id |
| S3-N3 | `reporter_phase_b.py:827-836` | `_render_s3_enhanced_checks` uses an ad-hoc parser that would count a per-unit `$/GB` string (render-vs-count desync, latent) | Reuse `parse_dollar_savings(rec.get('EstimatedSavings',''))` so render and count share the rate-string rejection rule |
| S3-N4 | `services/s3.py:109-111, 651-670, 1038-1058` | `intelligent_tiering` class (has lifecycle, no IT) is credited the IA delta (brief expected exclusion); ignores IT monitoring fee + IA 128 KB/30-day minimums | Exclude `intelligent_tiering` from `_GAP_OPPORTUNITY_CLASSES` (align brief), OR net out IT monitoring fee + gate on avg object size; document the choice |
| S3-N5 | `services/s3.py:831-833, 1157-1158` | `list_buckets` not paginated — >10k-bucket accounts (ListBuckets now paginates) silently under-enumerated | Paginate `ListBuckets` with `ContinuationToken` in both `get_s3_bucket_analysis` and `get_enhanced_s3_checks` |

### sagemaker
Fold into the SageMaker CRITICAL commit (`sagemaker C1`, Cluster B).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `reporter_phase_b.py:1345-1385, 1812-1876` | Generic renderer key mismatch: recs use lowercase `endpoint_name`/`notebook_name`/`job_name` → title "…: Resource", savings unstyled + `check_category` double-prints | Add the lowercase keys to `_render_generic_other_rec` resolver and format `monthly_savings` as currency, or register a small sagemaker handler |
| L2 | `services/adapters/sagemaker.py:82-95` | `_list_endpoints` paginator fallback does a single `list_endpoints()` (caps ~100, swallows a second failure) | Drop the non-paginating fallback or loop on NextToken; record the failure on `ctx` |
| L3 | `services/adapters/sagemaker.py:125-131, 439-441` | `active_endpoint_count` increments before the zero-invocations test → idle endpoints counted as both Active and Idle; CLAUDE.md omits sagemaker | Document that active includes idle (or subtract idle); add the sagemaker Live-Pricing row (`get_sagemaker_instance_monthly`) |

### step_functions
Fold into the Step Functions CRITICAL commit (`step_functions C1` Cluster A / C2 SR-2). L4 doc updates after SR-2 removes the flat-$50 path.

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/adapters/step_functions.py:22-24` | `required_clients()` declares `'states'` but the shim uses `'stepfunctions'` client (never consumed in prod today) | Change `required_clients()` to `("stepfunctions", "cloudwatch")` |
| L2 | `services/adapters/step_functions.py:42, 87-92` | Free tier (4,000 transitions/month/account) not subtracted from the counted formula | When counting a real Standard cost, subtract the 4,000-transition free-tier before `$0.025/1K`; record in `AuditBasis` |
| L3 | `services/step_functions.py:29-34, 45, 81-82` | STANDARD-only coverage (EXPRESS skipped); dead `excessive_transitions`/`polling_workflows`/`nonprod_24x7` config + orphaned descriptor | Remove the dead keys/descriptor (or implement); decide + document Express scope |
| L4 | `services/adapters/CLAUDE.md:Live Pricing table, step_functions.py row` | Row implies a working counted CloudWatch saving; reality is dead lever (C1) + flat-$50 (C2/SR-2) + STANDARD-only | After SR-2 lands, restate: dead `eligible_for_migration` lever, advisory dollars, STANDARD-only, no CoH/CO |

### transfer
Fold into the Transfer CRITICAL commit (`transfer C1`, Cluster E).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/transfer_svc.py:82-85, 90` | Data note uses `$0.09/GB` (generic S3 egress) not Transfer Family `$0.04/GB up + $0.04/GB down`; a 14-day Sum mislabeled "/mo" (note uncounted) | Use $0.04 up + $0.04 down against the actual upload/download split; project the 14-day window ×30/14 or relabel "14-day" |
| L2 | `services/transfer_svc.py:28; services/adapters/transfer.py:54-57` | Dead `endpoint_optimization` key; non-list-Protocols + STOPPED single-protocol servers leave $0 recs that tally as **counted** | Remove the dead key; set `Counted=False` on any `EstimatedMonthlySavings<=0` rec via `mark_zero_savings_advisory` |
| L3 | `services/adapters/CLAUDE.md transfer.py row; services/adapters/transfer.py:19-21` | Doc files transfer under "Live Pricing" (it uses only a module constant); `required_clients()` omits `'cloudwatch'` | Move to a Module-constant grouping; add `'cloudwatch'` to `required_clients()` with `requires_cloudwatch`/`reads_fast_mode` |

### workspaces
Coverage + doc (most fleets Windows+Included, so low real impact).

| id | file:lines | defect | fix |
|----|-----------|--------|-----|
| L1 | `services/workspaces.py:23-37,63-73` | Bundle table is Windows + License-Included only; BYOL/Linux/Ubuntu/storage variants mispriced (compute-type-only lookup) | Key price lookup on `(compute_type, operatingSystem, license)` per WorkSpace, or scope the rec to Windows+Included and skip others |
| L2 | `services/adapters/CLAUDE.md:Live-Pricing table, workspaces.py row` | Row cites `get_instance_monthly_price("AmazonWorkSpaces", …)` which structurally returns 0 (no `instanceType` attribute); adapter uses hardcoded `WORKSPACE_BUNDLE_MONTHLY` | Restate the row as the hardcoded us-east-1 Windows+Included table region-scaled by `pricing_multiplier` |

---

## Definition of done

- All 120 LOW items resolved: fixed, honestly demoted to $0 advisory (`Counted=False`), or explicitly dropped/documented as an intentional limitation with a one-line rationale.
- Any LOW item that re-derives as a real counted-dollar change is **re-classified up** (don't silently bury a fabrication in the cleanup pass).
- `counted == rendered` preserved for every touched adapter (no new string-vs-number desync).
- Logic changes carry tests in `tests/test_<svc>_audit_fixes.py`; full suite green.
- Regression gate green: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py` — and **unchanged** (a LOW fix that moves a snapshot is a red flag; investigate before refreshing).
- `services/adapters/CLAUDE.md` / `core/CLAUDE.md` rows reconciled for every doc-drift row above (sweepable into one final "docs reconcile" commit).
- Shared-pattern items (opensearch L1 → SR-1; s3 S3-N2 → SR-3; api_gateway L1 / step_functions L4 → SR-2; elasticache L1 / opensearch L2 → SR-1/SR-3) checked off under their CRITICAL owner, not duplicated here.
- Stage only changed files; one service (or the docs sweep) per commit.

## PROMPT (end)