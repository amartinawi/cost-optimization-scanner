# Run Findings — AWS Cost Optimization Scanner Adapter Audit

- **Run date:** 2026-08-09
- **Run slug:** `audit-services-deep` (ZOdyssey, audit-only)
- **Scope:** 34 AWS cost-optimization adapters / shims across 8 specialist cohorts (C1–C8)
- **Cohorts:**
  - C1 compute-core: `ec2`, `ebs`, `ami`, `lightsail`
  - C2 container: `eks_cost`, `containers`, `batch`, `apprunner`
  - C3 db-reserved: `rds`, `aurora`, `dynamodb`, `redshift`
  - C4 cache-search: `elasticache`, `opensearch`, `msk`
  - C5 storage+data-movement: `s3`, `file_systems`, `transfer`, `dms`, `glue`
  - C6 serverless+analytics: `lambda` (`services/adapters/lambda_svc.py`), `step_functions`, `athena`, `quicksight`
  - C7 network: `network`, `network_cost`, `cloudfront`, `api_gateway`, `mediastore`
  - C8 composite+special: `commitment_analysis`, `sagemaker`, `bedrock`, `monitoring`, `workspaces`
- **Methodology:** Each cohort applied (a) adapter-FIRST validation via `codegraph_explore` against the on-disk `.codegraph/` index, (b) the A1–F5 recurring cost-fidelity bug-class sweep from `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md` applied to BOTH each adapter AND the draft findings, then (c) a confirm/refute pass against authoritative AWS sources (AWS doc URLs, `aws pricing get-products` SKUs, and explicit lessons classes). Where live AWS APIs were unreachable in the audit sandbox, on-disk pricing constants (which carry their own date+SKU citations) were cross-checked against public AWS doc URLs. Banner handling: every STALE-banner claim was re-verified by citing the CURRENT adapter `file:line` where the fix now lives before being classified CONFIRMED-already-fixed.
- **VERIFICATION STATUS (2026-08-09, post-publication):** Independently verified — see `RUN-FINDINGS-2026-08-09-VERIFICATION.md`. Verdict: **REPORT_NEEDS_REVISIONS**. Of the 35 NEW findings: 30 confirmed accurate, **2 REFUTED** (marked `[REFUTED-IN-VERIFICATION]` below — eks_cost fallback constant, apprunner pagination), **1 escalated to HIGH** (athena unconditional 0.75 factor), 2 downgraded (dynamodb fast-mode, redshift `required_clients`). Blind re-sweeps found ~20 missed findings on rds/s3/elasticache alone (each scored NEW: 0 here) and further material false negatives across the other 31 adapters — the Key-takeaways claim "No NEW CRITICAL or HIGH counted-dollar defect exists in any of the 34 adapters" is **WITHDRAWN**. Revised open-NEW ledger: **33 findings = 1 HIGH / 9 MEDIUM / 23 LOW** (athena #20 → HIGH; dynamodb fast-mode and redshift ×2 → LOW; 2 refuted removed). A fix branch (`fix/verification-pass3`) closes the top verification findings (EC-1, S3-1, S3-2, RDS-1, RDS-2, RDS-4).
- **This run is read-only.** No `.py` file, no `docs/audits/prompts/*` prompt, no `_LIVE_AUDIT_LESSONS.md`, and no git state (no `git add`/commit/push) was touched by any cohort or by this consolidation. The only artifacts written are the 8 cohort notepads under `.zcode/notepads/audit-services-deep/` and this report. All fix proposals in the Proposed-fixes index are PROSE ONLY.

---

## Executive summary

| Bucket | Count |
|---|---:|
| Total findings reviewed | **254** |
| CONFIRMED-already-fixed (banner valid / correctly-implemented) | **219** |
| NEW (open) findings | **35** |
| NEW — CRITICAL | **0** |
| NEW — HIGH | **0** |
| NEW — MEDIUM | **10** |
| NEW — LOW | **25** |

### Key takeaways

- ~~**No NEW CRITICAL or HIGH counted-dollar defect exists in any of the 34 adapters.**~~ **[WITHDRAWN 2026-08-09 — verification found a CRITICAL counted-$ fabrication in elasticache (Graviton $0-target), HIGH-class misses in rds/s3/elasticache, and escalated athena #20 to HIGH; see the verification doc.]** The remainder of this bullet stands for the items the prompts flagged: Every CRITICAL/HIGH item the prompts flagged (EC2 C6 commitment demotion, AMI cross-AMI snapshot double-count, EKS Extended-Support phantom surcharge, EKS idle-cluster false-positive delete, Aurora I/O-Optimized premium, DynamoDB 1.06x stack, Redshift CoH orphan bucket, batch "only-Graviton-counts" desync, apprunner empty-inventory, step_functions flat-$50, api_gateway flat-$50, mediastore false-unused-from-CW, the SR-2 `_FLAT_SAVINGS_SERVICES` reporter fabrication, the s3 CoH orphan, the file_systems EFS IA $0.016->0.025 rate, dms flat-0.35, glue 160-hr) is CONFIRMED-already-fixed in current code, each cited to a current adapter `file:line`.
- **The 35 NEW findings are LOW-dominant (25 LOW, 10 MEDIUM).** The single dominant NEW class is **E1 silent-failure classification** (a `bare except: pass` / `except Exception: ctx.warn(...)` that swallows an `AccessDenied`/throttle without routing it through `record_aws_error` -> `ctx.permission_issue`): it appears in ~9 of the 35 NEW findings (aurora, opensearch, msk, transfer x2, athena, redshift, commitment_analysis, monitoring). Every one of these is **safe in the dollar direction** (it suppresses a finding rather than fabricating one) and **does not corrupt a counted dollar**; the defect is observability/IAM-gap surfacing, not cost-fidelity.
- **Only TWO NEW findings produce a real, bounded counted-$ error** (both MEDIUM), and both are localized: (1) `network` VPC-endpoint nonprod x duplicate double-count (`services/vpc_endpoints.py:109-148`) — over-counts an endpoint by `vpc_ep_monthly * az_count` only when a single endpoint is both nonprod-tagged AND the 3rd+ of its `vpc:service`; (2) `bedrock` PT-breakeven blended flat token rate (`services/adapters/bedrock.py:316`) — applies a single `0.000_003 $/token` to a counted `monthly_savings` instead of per-model per-direction SKUs. Neither affects CoH/CO-driven dollars.
- **The honesty invariant holds.** The expected outcome was that MOST STALE-banner services would yield CONFIRMED-already-fixed because the cost-fidelity remediation had landed before these prompts were re-run. The actual outcome matches that expectation precisely: across all 27 STALE-banner (or feature-note) services, the CONFIRMED:NEW ratio is approximately 200:30, and the 5 services carrying no banner (ec2, ebs, rds, s3, file_systems) yield CONFIRMED-dominant tallies too. No STALE-banner service exhibits a HIGH/CRITICAL NEW anomaly requiring a downgrade, and the consolidation spot-checked the two MEDIUM counted-$ candidates (network VPC, bedrock token rate) plus the commitment_analysis feature-note MEDIUM against current code — all three citations verified line-accurate, none downgraded.
- **Coverage gaps (NOT cost-fidelity bugs) are documented but not counted.** Lightsail DBs/LBs/disks, QuickSight per-user (Author/Reader), API-Gateway HTTP/WebSocket, and DynamoDB/RDS on-demand unit-semantics are flagged in their service sections as deliberate advisories or documented future enhancements, never as NEW counted-dollar defects.

### Honesty-invariant statement

> **[CAVEAT ADDED IN VERIFICATION]** The invariant below tests only for OVER-claiming (invented NEWs on remediated services) and held. It is blind to UNDER-claiming: the three adapters blind-swept in verification (rds, s3, elasticache — all scored NEW: 0 here) each carried material missed findings up to CRITICAL, and the 31-adapter extension sweep found more. "CONFIRMED-dominant" means the CONFIRMED claims are true — not that the adapters are clean. A NEW: 0 score on a STALE-banner service is a scrutiny trigger, not a health signal.

**Expected:** MOST STALE-banner services would yield CONFIRMED-already-fixed (banner disclaimers accurate); a predominantly-NEW run on STALE-banner services would be suspect.
**Actual:** The expectation is confirmed. Every STALE-banner service is CONFIRMED-dominant (5–9 CONFIRMED vs 0–4 NEW per service). The NEW findings cluster in two benign categories — E1 silent-failure-classification hygiene (observability, never wrong-$) and small coverage/pagination edges. The two NEW MEDIUM counted-$ findings (network VPC double-count, bedrock token rate) sit on services whose counted dollars are a small fraction of the report's total and whose conditions are bounded (VPC: requires nonprod-tag x 3rd+-of-service intersection; bedrock: requires a PT with non-zero on-demand token estimate below PT cost). **No rejection-duty downgrades were necessary** — every MEDIUM/LOW NEW finding carries a current-adapter `file:line` citation strong enough to retain.

---

## Per-service sections (by cohort)

Each service lists: banner status, a CONFIRMED-already-fixed sub-list (issue -> current `file:line` that fixes it), and a NEW sub-list (severity | description | `file:line` | impact | authoritative source | prose fix).

---

## C1 — compute-core

### ec2

- **Banner:** none (latest-live-audit findings header only)
- **CONFIRMED-already-fixed: 9**
  - [HIGH] `_asg_member_instance_ids` AccessDenied no longer wiped to `set()`; routes via `record_aws_error` and returns partial set — `services/adapters/ec2.py:133-139`
  - [HIGH] Burstable `CPUCreditBalance` AccessDenied now classified via `ctx.permission_issue` — `services/ec2.py:818-827`
  - [CRITICAL] C6 commitment (SP/RI) demotion gated on locally-derived recs too (`split_by_commitment` + CE headroom ceiling on all five sources) — `services/adapters/ec2.py:286-319`
  - [HIGH] Cross-source dedup CoH > CO > heuristic by normalized id + ASG-name — `services/adapters/ec2.py:216-242`
  - [HIGH] Intra-adapter `best_by_instance` keeps one heuristic per instance; anonymous recs keyed `_anon_{id(rec)}` — `services/adapters/ec2.py:254-274`
  - [HIGH] Tag-based advanced levers (cron/batch/instance-store/non-prod) corroborated by measured CW low-util; advisory in fast-mode — `services/adapters/ec2.py:25-27,202-209`
  - [HIGH] `_compute_ec2_savings` target_type exact delta; factor path guarded; no fallback fabrication — `services/ec2.py:376-432`
  - [MEDIUM] `_PREVIOUS_GEN_TARGETS` arch-safe x86->x86 multi-family; `_INSTANCE_STORE_FAMILIES` token-matched multi-family — `services/ec2.py:89-123`
  - [MEDIUM] Counted == rendered; `_coh_is_renderable` mirrors reporter; CO placeholder -> `ctx.warn` — `services/adapters/ec2.py:30-49,179-191`
- **NEW: 1**
  - **LOW | `get_enhanced_ec2_checks` and `get_advanced_ec2_checks` outer `except` are generic `ctx.warn`, not permission-classified** | `services/ec2.py:834-835` and `services/ec2.py:1224` | A denied `describe_instances`/`describe_volumes` aborts the enhanced/advanced scan and reads as "no findings" instead of surfacing `ec2:Describe*` as a permission gap. CoH/CO still run and carry most counted dollars; this is the smaller heuristic tail. | Lessons class **E1** (classify enumeration failures; mirror `services/ebs.py:322-338` and the burstable handler at `services/ec2.py:818-827`). | Prose fix: wrap each body in `try/except ClientError as ce:` that inspects `ce.response["Error"]["Code"]` and routes `UnauthorizedOperation`/`AccessDenied` to `ctx.permission_issue(..., action="ec2:DescribeInstances")`, else `ctx.warn`; keep the bare `except Exception` as a final fallback `ctx.warn`.

### ebs

- **Banner:** none (latest-live-audit findings header only)
- **CONFIRMED-already-fixed: 8**
  - [HIGH] De-minimis snapshot ($0.00 recoverable) suppressed via rounded-potential gate — `services/ebs.py:624-631`
  - [HIGH] EBS snapshot recs are `$0` `Counted=False` advisories; AMI-backed snapshots excluded — `services/ebs.py:599,605-606,663-684`
  - [HIGH] `FullSnapshotSizeInBytes` preferred over `VolumeSize` — `services/ebs.py:615-623`
  - [HIGH] CoH > CO > heuristic dedup by normalized vol-id; stale delete-recs fail-closed — `services/adapters/ebs.py:101-154,224-226`
  - [HIGH] gp2->gp3 net savings models gp3 IOPS parity; region-correct; no double-multiply on engine path — `services/adapters/ebs.py:31-44,248-272`
  - [MEDIUM] io2 tiered IOPS cost; gp3/io1 flat; unattached 100%-on-delete via live engine — `services/ebs.py:342-413`
  - [MEDIUM] CW-gated IOPS check; fast-mode skip; no-CW-data -> skip with warn (no fabricated $) — `services/ebs.py:439-482`
  - [MEDIUM] `ebs_snapshots` leak-out renders via Snapshots tab; excluded from EBS counted total — `services/adapters/ebs.py:274-299`
- **NEW: 1**
  - **LOW | `compute_ebs_checks` outer `except` is generic `ctx.warn`, not permission-classified** | `services/ebs.py:689-690` | A denied `describe_volumes` (gp2 scan) or `describe_snapshots` aborts those checks and reads as "no gp2/no old snapshots" instead of surfacing `ec2:DescribeVolumes`/`ec2:DescribeSnapshots` as a permission gap. Asymmetric with `get_unattached_volumes` (`services/ebs.py:322-335`) which DOES classify. CoH/CO/unattached still carry most counted dollars. | Lessons class **E1**. | Prose fix: mirror `get_unattached_volumes` — `try/except ClientError as ce:` routing `UnauthorizedOperation`/`AccessDenied` to `ctx.permission_issue(..., action="ec2:DescribeVolumes")`/`DescribeSnapshots`, else `ctx.warn`.

### ami

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 8. NEW: 0.
- **CONFIRMED-already-fixed: 8**
  - [CRITICAL] Cross-AMI shared-snapshot double-count fixed via `counted_snapshot_ids` set; co-dependent AMI is `$0` `Counted=False` advisory — `services/ami.py:23-103,328-388`
  - [HIGH] Dedup claim-order: snapshot claimed only AFTER skip checks pass AND sized >0 — `services/ami.py:92-102,315-330`
  - [HIGH] `FullSnapshotSizeInBytes` preferred; VolumeSize fallback flagged; unsizable -> skip (no fabrication) — `services/ami.py:72-91`
  - [HIGH] Outer `except` no longer empties tab silently; routes via `record_aws_error` — `services/ami.py:437-438`
  - [HIGH] Fail-safe unused detection: any unresolved reference -> suppress ALL deletion candidates — `services/ami.py:179,265-277`
  - [HIGH] Unused-detection reference paths: ASG-LT, EC2 Fleet, Spot Fleet, cross-account launchPermission — `services/ami.py:192-263,315-326`
  - [MEDIUM] `describe_launch_templates`/`describe_auto_scaling_groups`/`describe_images` paginated — `services/ami.py:166-249`
  - [MEDIUM] Snapshot rate region-correct via engine; multiplier ONLY on fallback — `services/ami.py:282-288`
- **NEW: 0.** All AMI-specific prompt items are CONFIRMED-already-fixed against current code. (Residual: EC2 Image Builder enumeration is a documented gap, disclosed in rec text `services/ami.py:408-410` — not a counted-$ bug.)

### lightsail

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 7. NEW: 0.
- **CONFIRMED-already-fixed: 7**
  - [HIGH] Fabricated mid-tier bundle prices replaced with live-validated AWS list prices (medium Linux = $24.00) — `services/lightsail.py:34-52`
  - [HIGH] Windows priced as Windows (OS-aware via `_win_` suffix); `_parse_bundle_id` returns `is_windows` — `services/lightsail.py:63-97,125-126`
  - [HIGH] Unknown/unrecognized bundle id -> `None` -> $0 advisory + `ctx.warn` (no $20 fabrication; old `medium_2_0` fallback GONE) — `services/lightsail.py:84-97`, `services/adapters/lightsail.py:104-125`
  - [HIGH] Counted == displayed: oversized is $0 advisory; static-IP saving now counted and region-scaled — `services/adapters/lightsail.py:64-100`
  - [HIGH] Static-IP rate is flat-global-correct: `$0.005/hr × 730 = $3.65/mo` (the AWS public-IPv4 charge); multiplier applied intentionally as approximation — `services/lightsail.py:54-58`, `services/adapters/lightsail.py:86-88`
  - [MEDIUM] `get_static_ips()` paginated via nextPageToken; coarse single-try wrapped but recorded — `services/lightsail.py:165-173,189-190`
- **NEW: 0.** Two residual items are documented LIMITATIONS, not bugs: oversized is metric-less (now a $0 advisory); whole resource classes (DBs/LBs/disks/snapshots) are uncovered (`services/lightsail.py:106-107`) — coverage gap, out of scope for cost fidelity.

---

## C2 — container

### eks_cost

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 8. NEW: 2.
- **CONFIRMED-already-fixed: 8**
  - [CRITICAL] Extended Support surcharge no longer counted from `supportType=="EXTENDED"` config field; gated on `version_status=="EXTENDED_SUPPORT"`; policy-extended-but-standard-support emits a `$0` `extended_support_pending` advisory — `services/adapters/eks.py:319-381`
  - [HIGH] CoH-vs-cluster authority dedup (`_dedupe_clusters_against_cost_hub`); check_type+actionType-aware (compute-only CoH does NOT demote the independent surcharge) — `services/adapters/eks.py:756-813`
  - [HIGH] Idle-cluster false positive corroborated by EC2 owned-node count (`_count_owned_ec2_nodes`); counts full control-plane only when `owned_node_count==0`, else demotes — `services/adapters/eks.py:418-519`
  - [MEDIUM] `failed_cluster` now carries audit_basis — `services/adapters/eks.py:393-414`
  - [MEDIUM] Node-group Spot + Graviton mutually exclusive (one advisory per node group, higher-saving lever) — `services/adapters/eks.py:575-636`
  - [MEDIUM] Node-group pricing dropped when unsizable/scaled-to-0 (no $0 noise rec) — `services/adapters/eks.py:569-573,650-663`
  - [HIGH] Bucket-name == key invariant (`EksCluster` -> `eks_cost`) across all three layers — `core/scan_orchestrator.py:75,117`, `services/adapters/eks.py:752`
  - [MEDIUM] AccessDenied classified via `ctx.permission_issue` across EKS calls — `services/adapters/eks.py:232-740`
- **NEW: 2**
  - **[REFUTED-IN-VERIFICATION]** ~~LOW | Extended-Support FALLBACK constant is stale ($0.50 vs published $0.60)~~ — the live Pricing API SKU `USE1-AmazonEKS-Hours:extendedSupport` is **$0.50/hr** (the surcharge); the $0.60 on the pricing page is the TOTAL including the $0.10 base fee. The constant is correct; applying the proposed fix would have INTRODUCED a ~$73/mo/cluster overstatement. Original text (retained for the record): **LOW | Extended-Support FALLBACK constant is stale ($0.50 vs published $0.60)** | `core/pricing_engine.py:361` (`FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY: float = 0.50`), consumed at `:1289` | On a Pricing-API failure the fallback under-states the surcharge by ~`$73/mo/cluster` (`($0.60-$0.50)×730`). Bounded: offline/permission-denied scans only; the engine path is region-correct. | AWS EKS Extended Support pricing page (currently `$0.60/cluster-hour`); lessons family **C7** (verify the rate, not memory). | Prose fix: bump `FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY` from `0.50` to `0.60` (other-region fallbacks re-derive via `self._fallback_multiplier`, which the code already does).
  - **LOW | `extended_support_pending` advisory carries a non-zero `AdvisoryEstimate`** | `services/adapters/eks.py:366` | The `Counted=False` rec correctly has `monthly_savings=0.0` (no summed-field leak), but carries a non-zero `AdvisoryEstimate`. This is the sanctioned B1-iii projection pattern ("you'll pay ~$X from date Y if you don't upgrade") and is NOT a leak — but any sweep that sums `AdvisoryEstimate` would false-positive it. | Lessons class **B1-iii** (sanctioned commitment/what-if exception) + **F4** (avoid false findings). | Prose fix: optionally rename `AdvisoryEstimate` -> `PotentialMonthlySavings` for consistency with other advisories; not a counted-$ fix.

### containers

- **Banner:** none (inline FIXED notes, verified against current code). CONFIRMED-already-fixed: 7. NEW: 1.
- **CONFIRMED-already-fixed: 7**
  - [HIGH] Compute Optimizer silent failure classified; opt-in placeholder -> `ctx.warn` and dropped — `services/adapters/containers.py:77-88`, `services/advisor.py:404-419`
  - [HIGH] Cross-source authority dedup CoH > CO > heuristic; cluster-qualified (`_heuristic_resource_key` returns `cluster/service`) — `services/adapters/containers.py:96-129,339-357`
  - [HIGH] No double region-scale on Fargate/ECR engine path; fallback constants match us-east-1 values — `services/adapters/containers.py:228-329`, `core/pricing_engine.py:342-364`
  - [HIGH] ARM Graviton vs x86 Fargate priced correctly via arch/os (Windows OS leg only when `os.startswith("win")`) — `services/adapters/containers.py:265-270,314-329`, `services/containers.py:308-315`
  - [MEDIUM] ECR reclaim priced on deduplicated GiB layers; advisory when unanalyzable (no B1 leak) — `services/adapters/containers.py:138-150,224-251`
  - [MEDIUM] `commitment_analysis` coupling is baseline-input (`ctx.fargate_rightsizing_monthly`), not a second counted line; active-commitment demotion via `demote_recs_in_place` — `services/adapters/containers.py:152-188`
  - [LOW] ECR `describe_repositories` paginated (un-paginated survives only as paginator-unavailable fallback) — `services/containers.py:343-348`
- **NEW: 1**
  - **LOW | `SPOT_SAVINGS_FACTOR = 0.70` is dead code** | `services/adapters/containers.py:20` | The constant is declared with a docstring ("Applied to the rightsized on-demand base when a rec is explicitly a Fargate->Spot move") but is NOT referenced anywhere in `containers.py`; the rightsizing path uses `quantify_fargate_rightsizing` (exact current->target delta). The Fargate->Spot lever is unimplemented. Honest in effect (no fabricated Spot dollar) but misleading dead weight. | Lessons class **C9** (grep for `factor`/`* 0.` multipliers against a price) — here unused, so it is dead, not fabricating. | Prose fix: either wire a defensible Fargate->Spot delta (published Fargate Spot rate, interruptible-workload-gated) or remove the constant + docstring.

### batch

- **Banner:** STALE — VERIFIED ACCURATE. The adapter is now advisory-only. CONFIRMED-already-fixed: 7. NEW: 0.
- **CONFIRMED-already-fixed: 7**
  - [CRITICAL] Adapter is now advisory-only — no fabricated counted dollar (the "only-Graviton-counts" string-vs-counted desync and the `×730` fabricated-hours problem both eliminated) — `services/adapters/batch.py:23-71`
  - [HIGH] `is_fargate` detection fixed (reads `computeResources.type` in `{FARGATE, FARGATE_SPOT}`) — `services/batch_svc.py:39-48`
  - [MEDIUM] Dead `BATCH_COMPUTE_FALLBACK_MONTHLY` constant no longer reachable from batch scan path — `services/adapters/batch.py`, `services/batch_svc.py`
  - [HIGH] Silent failures classified via `record_aws_error` — `services/batch_svc.py:110-116`
  - [N/A] No CoH/CO source for Batch (correctly absent) — `core/scan_orchestrator.py`
  - [MEDIUM] Render path: per-rec fallback intact (advisory recs render while contributing $0) — `services/adapters/batch.py:60`
  - [MEDIUM] Spot+Graviton latent stacking moot (advisory-only) — `services/adapters/batch.py:46-48`
- **NEW: 0.** (Out-of-scope: a repo-wide grep for the `BATCH_COMPUTE_FALLBACK_MONTHLY` constant name was not performed by C2 — if it lingers outside the batch files it is harmless dead weight; flagged for follow-up.)

### apprunner

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 1.
- **CONFIRMED-already-fixed: 6**
  - [CRITICAL] Shim now emits a real idle-service rec (no longer zero recs / dead pricing loop); priced at `mem_gb × APP_RUNNER_MEM_GB_HOURLY × 730 × multiplier` — `services/apprunner.py:78-159`, `services/adapters/apprunner.py:37-85`
  - [HIGH] CloudWatch dimension bug fixed (real `ServiceName`+`ServiceID`; abstains on no-data rather than false-positive) — `services/apprunner.py:33-75`
  - [HIGH] Fabricated 160-hour assumption removed (only `APP_RUNNER_MEM_GB_HOURLY` and `HOURS_PER_MONTH` remain; `RIGHTSIZING_SAVINGS_RATE=0.12` GONE) — `services/adapters/apprunner.py:20-21`
  - [MEDIUM] PAUSED-service concern moot — only RUNNING + 0-request flagged — `services/apprunner.py:107-109`
  - [MEDIUM] `describe_service` failure no longer swallowed; config parsed safely (no silent 2GB default) — `services/apprunner.py:120-130`, `services/adapters/apprunner.py:46-65`
  - [MEDIUM] Region scaling applied exactly once — `services/adapters/apprunner.py:66`
- **NEW: 1**
  - **[REFUTED-IN-VERIFICATION]** ~~LOW | `list_services` is still un-paginated~~ — the App Runner `ListServices` API doc states: "If you don't specify `MaxResults`, the request retrieves **all available results in a single response**." No service is dropped; the claimed impact does not exist (and botocore ships no apprunner paginator). Original text (retained for the record): **LOW | `list_services` is still un-paginated** | `services/apprunner.py:100` | `list_services()` is a single call with no paginator/NextToken loop; App Runner `ListServices` does paginate via `NextToken`, so an account with more than the default page size silently drops later services (including potentially a 0-request service). Bounded (App Runner deployments are usually small). | Universal catalogue "Coverage gated to a hardcoded allowlist"; prompt Phase 4.8. | Prose fix: switch to `apprunner.get_paginator("list_services")` (or loop on `NextToken`), mirroring the paginated ECS/ECR calls in `services/containers.py`.

---

## C3 — db-reserved

### rds

- **Banner:** none (latest-live-audit findings header only). CONFIRMED-already-fixed: 5. NEW: 0.
- **CONFIRMED-already-fixed: 5**
  - [HIGH] `reconcile_snapshot_savings` numeric lockstep (caps `EstimatedMonthlySavings` AND `EstimatedSavings` together; prevents the +$719.60 stale-field overstatement) — `services/rds_logic.py:176-188`
  - [HIGH] No-CE-actual demote branch sets `Counted=False` AND `numeric=0.0`; upper bound -> `PotentialMonthlySavings` — `services/rds_logic.py:200-207`
  - [HIGH] CoH > CO > heuristic authority dedup keyed by `normalize_rds_arn`/`coh_rds_key`; single highest-savings winner per key; snapshot prefixes preserved — `services/rds_logic.py:34-58,217-301`
  - [HIGH] CoH dropped-type (E2) for `RdsDbInstance` + `RdsDbCluster` -> `rds` bucket — `services/adapters/rds.py:112-114`, `core/scan_orchestrator.py:112-113`
  - [HIGH] Aurora cluster cross-tab overlap (rds H1) — `ctx.rds_covered_instance_ids` published; Aurora suppresses covered members — `services/adapters/rds.py:167-179`, `services/adapters/aurora.py:671-678,243-245`
- **NEW: 0.** Live-audit header items (R-1, R-2) are fixed; structural invariants (R-3, R-4, R-5) hold.

### aurora

- **Banner:** STALE — VERIFIED ACCURATE (surgical refresh 2026-08-08). CONFIRMED-already-fixed: 6. NEW: 1.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] ACU fallback `0.06` -> `0.12` (matches us-east-1 Aurora Serverless v2 list rate) — `core/pricing_engine.py:327,1013`
  - [HIGH] I/O-Optimized storage premium `0.025` -> `0.125` + ~30% instance premium; both legs region-priced; net `optimized_premium = storage_premium + instance_premium` — `services/adapters/aurora.py:32,440-474,543,552`
  - [HIGH] I/O-metric semantics — VolumeReadIOPs/VolumeWriteIOPs are request-count metrics, summed correctly; storage leg fail-safe (skips when `VolumeBytesUsed` unavailable) — `services/adapters/aurora.py:502-522`
  - [HIGH] Serverless v2 ACU headroom assumption demoted to `$0` advisory with explicit AuditBasis — `services/adapters/aurora.py:373-437`
  - [HIGH] Cross-adapter RDS overlap — covered set from BOTH CoH bucket and `ctx.rds_covered_instance_ids` — `services/adapters/aurora.py:671-678,243-245`
  - [HIGH] All three checks now emit structured AuditBasis; I/O-Optimized mode threaded into instance pricing — `services/adapters/aurora.py:259-596,727-734`
- **NEW: 1**
  - **MEDIUM | CloudWatch helpers swallow AccessDenied/throttle silently (Class E1)** | `services/adapters/aurora.py:95-121` (`_get_cloudwatch_avg`), `:124-149` (`_get_cloudwatch_sum`), `:152-173` (`_get_cloudwatch_avg_max`) — each ends `except Exception: pass` / `return None` | A CW `AccessDenied`/throttle returns `None`, which callers treat as "no data": serverless-v2 skips (`:400`), io-tier skips (`:505-506`), rightsizing skips (`:270-274`) WITHOUT surfacing the permission gap. Functionally safe (never overstates) but hides the gap. Enumeration helpers ARE fixed (route through `record_aws_error` at `:52-55,72-73`) — these three CW helpers are the remnant. | Lessons class **E1**; `record_aws_error` helper at `services/_aws_errors.py:47-54`. | Prose fix: in each of the three helpers replace `except Exception: pass` with `except Exception as e: record_aws_error(ctx, e, service="aurora", context=f"cloudwatch:GetMetricStatistics {metric} failed")`; thread `ctx` into the helper signatures (callers already have `ctx`).

### dynamodb

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 2.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] Config-dimension savings with no usage evidence (default 0.30 / over-prov 0.40) — table_analysis demoted to `$0` advisory; over-provisioned counted only when `MetricsAvailable` AND `LowUtilization` AND `current - target` delta positive (factors GONE) — `services/adapters/dynamodb.py:36-54,97-107,126-162`
  - [HIGH] Over-provisioned + Reserved double-count on one table — reserved demoted; `counted_tables` set ensures one counted rec per table — `services/adapters/dynamodb.py:113-124`
  - [HIGH] Reserved capacity demoted to `Counted=False` advisory; active-commitment demotion via `demote_recs_in_place` — `services/adapters/dynamodb.py:117-124,198-203`, `services/dynamodb.py:85`
  - [HIGH] GSI throughput summed (`_sum_gsi_throughput`); per-GSI rightsized target via `GlobalSecondaryIndexName` dimension — `services/dynamodb.py:100-124,243-264,547-552`
  - [HIGH] CoH ARN->table-name parsing (plain-table + GSI-index); orchestrator buckets `DynamoDBTable -> dynamodb` — `services/adapters/dynamodb.py:172-191`, `core/scan_orchestrator.py`
  - [LOW] "Enable monitoring" `$0` nudges are `Counted=False`; count hygiene excludes them from `total_recs` — `services/adapters/dynamodb.py:164-167,207-211`
- **NEW: 2**
  - **LOW [DOWNGRADED-IN-VERIFICATION from MEDIUM — scan-performance, not cost-fidelity; no dollar can be wrong in either direction] | `reads_fast_mode` not declared (Class fast-mode)** | `services/adapters/dynamodb.py:57-69` (declares `requires_cloudwatch: bool = True` but NOT `reads_fast_mode`); shim CW reads at `services/dynamodb.py:142-159,390-407,590-607` are NOT gated on `ctx.fast_mode` | A `--fast` scan still pays for per-table (and per-GSI) `get_metric_statistics` calls. No counted dollar is fabricated either way; this is a performance/cost-of-scan optimization. Compare RDS (`services/adapters/rds.py:47`) and Aurora (`services/adapters/aurora.py:622`) which both declare `reads_fast_mode`. | Lessons E1 corollary ("CW reads not gated on ctx.fast_mode"). | Prose fix: add `reads_fast_mode: bool = True` to `DynamoDbModule`; thread `ctx.fast_mode` into `get_dynamodb_table_analysis`/`get_enhanced_dynamodb_checks`; when True, skip the `get_metric_statistics` calls and set `MetricsAvailable=False` (which routes the over-provisioned rec to a `$0` advisory). Mirror RDS/Lambda pattern.
  - **LOW | On-demand `EstimatedMonthlyCost` unit semantics** | `services/dynamodb.py:411-419` (`(avg_r × _ON_DEMAND_RCU_PER_REQUEST + avg_w × _ON_DEMAND_WCU_PER_REQUEST) × 730`) | The unit/semantics question is real (consumed-capacity metric × per-request-unit rate × 730h), but impact is contained: `EstimatedMonthlyCost` flows only into `dynamodb_table_analysis`, which the adapter DEMOTES to `Counted=False` + `$0.0` (D-1). Advisory-only; no counted-$ defect. | AWS DynamoDB on-demand pricing (https://aws.amazon.com/dynamodb/pricing/on-demand/). | Prose fix (optional): document in the AuditBasis that `EstimatedMonthlyCost` is an indicative advisory estimate assuming sustained 730h average consumption. Not a counted-$ fix.

### redshift

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 2.
- **CONFIRMED-already-fixed: 6**
  - [CRITICAL] Orphaned CoH bucket consumed (`is_renderable_coh_rec`); `coh_keys` spans all CoH recs; commitment demotion via `demote_coh_by_commitment` — `services/adapters/redshift.py:69-96`
  - [HIGH] RI + rightsizing stack on one cluster — every heuristic lever demoted to `$0` advisory; only CoH is counted — `services/adapters/redshift.py:20-22,88-127`
  - [HIGH] RI counted, not advisory — RI/Serverless categories demoted to `Counted=False` + `RI_ADVISORY_SAVINGS`; AuditBasis documents ~30% 1-yr No-Upfront — `services/adapters/redshift.py:26-29,103-116`
  - [HIGH] Reduction factor vs exact delta on rightsizing — 0.24 factor GONE; cluster-rightsizing is a `$0` advisory — `services/adapters/redshift.py:118-127`
  - [HIGH] Counted ≠ rendered desync — adapter single-sources card string; counted == rendered — `services/adapters/redshift.py:88-127`
  - [MEDIUM] Serverless = `$0` counted but `$0` rendered (was $150) — Serverless Optimization in ADVISORY_CATEGORIES — `services/adapters/redshift.py:21,103-116`
- **NEW: 2**
  - **MEDIUM | Serverless silent-failure: bare `except Exception: pass` (Class E1)** | `services/redshift.py:122-123` (wraps the entire `redshift-serverless` `list_workgroups` block) | A `redshift-serverless:ListWorkgroups` `AccessDenied`/throttle is indistinguishable from "no serverless workgroups" — serverless recs vanish with NO `ctx.warn`/`ctx.permission_issue`. The outer block at `:125-126` DOES route through `ctx.warn` but only for provisioned `redshift`. Functionally safe (serverless recs are advisory) but violates E1. | Lessons class **E1**. | Prose fix: replace `except Exception: pass` with `except Exception as e: record_aws_error(ctx, e, service="redshift", context="redshift-serverless:ListWorkgroups failed")` (import from `services._aws_errors`). Mirror the aurora `_describe_aurora_clusters` fix.
  - **LOW [DOWNGRADED-IN-VERIFICATION from MEDIUM — the stated mechanism is wrong: `ctx.client()` is a lazy caching factory and `required_clients` has no orchestrator consumer, so the undeclared client works at runtime; residual value is declaration hygiene only] | `required_clients()` omits `redshift-serverless`** | `services/adapters/redshift.py:40-42` (returns `("redshift",)`); shim calls `ctx.client("redshift-serverless")` at `services/redshift.py:98` | The undeclared client may be unavailable (orchestrator only pre-instantiates declared clients); when it is, the bare `except` at `:122-123` swallows the error silently (compounds RS-9 above). No counted-$ impact (serverless recs are advisory). | Lessons class **E1** (missing declaration is the upstream cause of the silent no-op). | Prose fix: change `services/adapters/redshift.py:42` to `return ("redshift", "redshift-serverless")`. Pair with the RS-9 fix so an actual permission error surfaces via `record_aws_error`.
- **Documented in-code (not a NEW finding):** RA3 managed storage unpriced TODO at `services/adapters/redshift.py:60` (`# TODO: RA3 node types charge managed storage at $0.024/GB/month`) — LOW; advisory-only under-costing; CoH covers it for counted recs.

---

## C4 — cache-search

### elasticache

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 8. NEW: 0.
- **CONFIRMED-already-fixed: 8**
  - [CRITICAL] CoH orphan bucket consumed (`is_renderable_coh_rec`, `coh_key`, `normalize_resource_id`) — `services/adapters/elasticache.py:155-166`, `services/_coh_dedup.py:43-69`, `core/scan_orchestrator.py:114`
  - [HIGH] `cache.` prefix correct (it IS the canonical SKU); engine-pin disambiguates 6 shared-`instanceType` SKUs — `core/pricing_engine.py:1589-1611`, `services/adapters/elasticache.py:191-193`
  - [HIGH] Graviton priced for `num_nodes` (defaults to 1 only when shim omits — conservative under-count) — `services/adapters/elasticache.py:185,200,206`
  - [HIGH] Reduction-factor levers GONE; Graviton/Underutilized now exact two-price deltas; Valkey 0.20 retained (real ~20% Redis discount) — `services/adapters/elasticache.py:174-294`
  - [HIGH] Idle/downsize gated on memory (DatabaseMemoryUsagePercentage + Evictions); fail-closed on unreadable metric — `services/elasticache.py:235-253`, `services/adapters/elasticache.py:250-291`
  - [HIGH] Counted == rendered; single-sourced `EstimatedSavings` string from counted dollar — `services/adapters/elasticache.py:335-353`
  - [MEDIUM] `reads_fast_mode=True` declared; CW reads gated in fast-mode with `ctx.warn` — `services/adapters/elasticache.py:124-125`, `services/elasticache.py:115-198`
  - [MEDIUM] CloudWatch inner failure routes via `record_aws_error` — `services/elasticache.py:259-269`
- **NEW: 0.**

### opensearch

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 9. NEW: 1.
- **CONFIRMED-already-fixed: 9**
  - [CRITICAL] CoH orphan bucket consumed; CoH-authoritative dedup in place — `services/adapters/opensearch.py:321-331,425-426`, `core/scan_orchestrator.py:115`
  - [HIGH] Idle Domain priced (instance + EBS, not the dead `rate_by_category` zero path); irreversible-DELETE safety gate on `IdleCorroborated` — `services/adapters/opensearch.py:343-362,432-436`
  - [HIGH] Render shows priced dollar (single-source) — `services/adapters/opensearch.py:497-511`
  - [HIGH] gp3 storage constant + savings factor fixed (`GP3_PRICE_PER_GB_MONTH = 0.122`, `GP2_PRICE_PER_GB_MONTH = 0.135`; real gp2->gp3 delta) — `services/adapters/opensearch.py:21-22,363-378`
  - [HIGH] Flat reduction factors GONE; Graviton/Underutilized are exact two-price deltas — `services/adapters/opensearch.py:90-112,262-284`
  - [HIGH] Cross-source / idle-vs-storage double-count deduped (`best_instance` + `idle_deleted_domains` mutex) — `services/adapters/opensearch.py:438-477`
  - [HIGH] Commitment demotion on BOTH CoH (`demote_coh_by_commitment`) AND local levers (`demote_covered_in_place`) — `services/adapters/opensearch.py:329,484`
  - [HIGH] Extended Support surcharge measured from `*-OpenSearchExtendedSupport` CE usage type (30/7 scaled); separate additive SourceBlock — `services/adapters/opensearch.py:159-259,513-575`
- **NEW: 1**
  - **LOW | CloudWatch inner/describe failures logger-only (Class E1)** | `services/opensearch.py:229-234` (per-domain inner `except Exception as e: logger.warning(...)`); outer scan guard at `:236-237` DOES use `ctx.warn` | A per-domain `describe_domain`/CW throttle silently removes that domain's idle/underutilized signal — the permission gap does not surface as `permission_issue`. No fabricated saving. The elasticache sibling WAS fixed (`services/elasticache.py:259-269`); opensearch was not. | Lessons class **E1**. | Prose fix: route the two inner `except` paths at `services/opensearch.py:229` and `:233` through `record_aws_error(ctx, ...)` (AccessDenied/Unauthorized/OptInRequired -> `permission_issue`, else `ctx.warn`), exactly as `services/elasticache.py:259-269` does.

### msk

- **Banner:** STALE — VERIFIED ACCURATE. Adapter is advisory-only. CONFIRMED-already-fixed: 7. NEW: 2.
- **CONFIRMED-already-fixed: 7**
  - [HIGH] Dead live-price filter fixed (`computeFamily`+`productFamily`+`operation=RunBroker`, NOT `instanceType`); fallback premium corrected `MSK_BROKER_OVER_EC2 = 2.19` (was 1.4x, ~36% low) — `core/pricing_engine.py:1524-1548,772-807`
  - [HIGH] Counted ≠ rendered desync removed; every rec is `$0 Counted=False` advisory via `_to_advisory_rec` — `services/adapters/msk.py:86-97,111-152`
  - [HIGH] Blanket 0.30 reduction factor GONE; `realizable_monthly_savings = 0.0` with `unmeasured_inputs` — `services/adapters/msk.py:22-83,111-138`
  - [HIGH] Phantom storage default 100 GB / num_brokers default 3 GONE; storage priced from real `VolumeSize` or omitted — `services/msk.py:47-67`, `services/adapters/msk.py:60-69`
  - [HIGH] Storage rec no longer counted-but-unpriced (`counted_recs` excludes `Counted is not False`) — `services/adapters/msk.py:144`
  - [HIGH] Tab gate for advisory-only service (tab renders) — `services/adapters/msk.py:140-152`
  - [HIGH] Advisory-leak (B1) absent (`_to_advisory_rec` forces `EstimatedMonthlySavings=0.0`) — `services/adapters/msk.py:86-97`
- **NEW: 2**
  - **LOW | `kafka:ListClusters` AccessDenied misclassified as generic `ctx.warn` (Class E1)** | `services/msk.py:94-95` (outer `except Exception as e: ctx.warn(...)`); inner `list_clusters_v2` block swallowed by bare `except Exception: pass` at `:91-92` | An `AccessDenied`/`UnauthorizedOperation` on `kafka:ListClusters` reports as a generic warning; the permission gap is not surfaced. The v2 swallow is moot today (serverless finding removed at `:88-90`) but stays a latent E1. No fabricated saving (advisory-only). | Lessons class **E1**. | Prose fix: wrap the outer except at `services/msk.py:94` with `record_aws_error(ctx, str(e), "msk", "kafka")` (matching the elasticache pattern), and either drop the now-dead `list_clusters_v2` block or route its except through `record_aws_error` if retained.
  - **LOW | Confidence mislabel: `enhanced_checks` inherits "Metric Backed" (config heuristic)** | `reporter_phase_b.py:2761-2792` (SOURCE_TYPE_MAP has no `("msk", "enhanced_checks")` entry) + `:2795` (`_GENERIC_SOURCE_TYPES["enhanced_checks"] = "Metric Backed"`) | MSK's checks are pure config heuristics (`"large" in instance_type`, `volume_size > 1000`) with NO CloudWatch read (`services/adapters/msk.py:107-109` declares only `("kafka",)`, no `requires_cloudwatch`). The rendered card carries a misleading "Metric Backed" badge. Cosmetic / report-integrity only. | Lessons class **F4** (the S3 precedent at `reporter_phase_b.py:2778`). | Prose fix: add `("msk", "enhanced_checks"): "Audit Based"` to SOURCE_TYPE_MAP just under the s3 override at `reporter_phase_b.py:2778`; refresh reporter snapshots with `SNAPSHOT_UPDATE=1`.

---

## C5 — storage+data-movement

### s3

- **Banner:** none (surgical refresh). CONFIRMED-already-fixed: 6. NEW: 0.
- **CONFIRMED-already-fixed: 6**
  - [MEDIUM] CoH orphan resolved by removal (`s3` not in `_HUB_SERVICES`; `S3Bucket` lands in `unbucketed_types` and surfaces the honest "dropped type" warning) — `core/scan_orchestrator.py:64-70`
  - [HIGH] `_DEDICATED_CATEGORIES` filter prevents double-count (Storage Class Optimization, Static Website Optimization excluded from `other_recs`) — `services/adapters/s3.py:23-26,83-87`
  - [HIGH] Count hygiene (advisory `$0` never inflates headline); F1 hardening tags every enhanced rec; F2 render-noise floor drops sub-1-GB $0 advisories — `services/adapters/s3.py:84,101-121`
  - [CRITICAL] Evidence-gated savings (no fabricated cold-class $; `_assess_bucket_coldness` returns "cold" only on zero-GET evidence) — `services/adapters/s3.py:90`, `services/s3.py:823-846`
  - [HIGH] Home-region pricing (no scan-region mispricing; `pricing_multiplier` deliberately NOT re-applied on the regional-multiplier-dict fallback) — `services/s3.py:776`
  - [MEDIUM] Silent-failure classification (`_is_static_website_bucket` routes via `record_aws_error`; `_route_bucket_error`/`_is_access_denied` route 403 to `permission_issue`) — `services/s3.py:719-740`
- **NEW: 0.**

### file_systems

- **Banner:** none (surgical refresh). CONFIRMED-already-fixed: 6. NEW: 0.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] EFS IA rate surgical fix `$0.016` -> `$0.025` (`FALLBACK_EFS_GB_MONTH_BY_CLASS["Infrequent Access"] = 0.025`) — `core/pricing_engine.py:261-267`
  - [CRITICAL] NET (not gross) EFS lifecycle saving (`_efs_ia_access_rate` nets the IA per-GB access charge; gross helper is advisory-only) — `services/efs_fsx.py:322-323,377`
  - [HIGH] `dedupe_counted` (one counted finding per fs_id, highest-wins) — `services/adapters/file_systems.py:58-59`
  - [HIGH] Count hygiene + L4 (sums the float `_savings`, not re-parsed string) — `services/adapters/file_systems.py:65-68`
  - [HIGH] Region scaling NOT double-applied on engine path (multiplier only on fallback) — `services/efs_fsx.py:114-179`
  - [MEDIUM] Advisory-leak (B1) absent — `services/adapters/file_systems.py:60`
- **NEW: 0.**

### transfer

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 4. NEW: 2.
- **CONFIRMED-already-fixed: 4**
  - [CRITICAL] String-vs-numeric desync — dollar single-sourced; both `EstimatedMonthlySavings` and `EstimatedSavings` from the same number; shim no longer bakes `0.30 × 730` — `services/adapters/transfer.py:103-126`, `services/transfer_svc.py:59-61`
  - [HIGH] Stopped-server fabricated saving — STOPPED/OFFLINE -> `Counted=False` + `$0.0` + AuditBasis; re-pricing skipped — `services/adapters/transfer.py:73-96`, `services/transfer_svc.py:105-122`
  - [HIGH] "remove all-but-one protocol" fabricated quantity — counted only when `PerProtocolUsageEvidence is True` AND `RemovableProtocols` positive int; advisory otherwise — `services/adapters/transfer.py:98-148`
  - [LOW] Data-transfer note rate `$0.09` -> `$0.04` each way; ProtocolHours documented region-flat (no `pricing_multiplier`) — `services/transfer_svc.py:94-103`, `services/adapters/transfer.py:30-36,60-62`
  - (Plus) [MEDIUM] `requires_cloudwatch=True` + `reads_fast_mode=True` declared; CW read gated on `ctx.fast_mode` — `services/adapters/transfer.py:20-25`, `services/transfer_svc.py:68`
- **NEW: 2**
  - **MEDIUM | Outer `except` uses bare `ctx.warn`, never `record_aws_error`/`permission_issue` (Class E1)** | `services/transfer_svc.py:124-125` (`except Exception as e: ctx.warn(f"Could not analyze Transfer Family resources: {e}", "transfer")`) | An `AccessDenied`/`UnauthorizedOperation` on `transfer:ListServers` records as a generic warn, not a `permission_issue`; operator is not told it is an IAM fix. No counted-$ fabrication (shim returns empty on outer-failure). Sibling shims `services/glue.py`/`services/dms.py` both route through `record_aws_error`; transfer is the holdout. | Lessons class **E1**. | Prose fix: replace `services/transfer_svc.py:124-125` with `record_aws_error(ctx, e, service="transfer", context="Could not analyze Transfer Family resources")` so AccessDenied classifies as `permission_issue`.
  - **MEDIUM | Inner CloudWatch `except` swallows all errors with no `ctx` record (Class E1)** | `services/transfer_svc.py:98-103` (`except Exception: rec["DataTransferCostNote"] = ("CloudWatch unavailable — ...")`) | A throttled/AccessDenied `cloudwatch:GetMetricStatistics` produces no `ctx` record at all; a CW permission gap is invisible in `permission_issues[]`. The note itself is never counted, so no `$` fabrication. NOTE the F-trap: the normal empty-datapoints path SHOULD stay silent (E1 exempts it); only the genuine exception path needs classifying. | Lessons class **E1**. | Prose fix: narrow the bare `except Exception:` to classify via `record_aws_error(ctx, e, service="transfer", context=f"CW BytesUploaded/BytesDownloaded for {server_id}")`, keeping the per-rec note for the empty-datapoints case.

### dms

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 8. NEW: 0.
- **CONFIRMED-already-fixed: 8**
  - [CRITICAL] Single-AZ vs Multi-AZ SKU ambiguity fixed (pinned by exact usagetype suffix; `×730`; no `MaxResults=1` over two SKUs) — `core/pricing_engine.py:1122-1185,1922-1938`, `services/adapters/dms.py:148-149,217,230`
  - [CRITICAL] Flat 0.35 reduction factor GONE; `_downsize_delta` with `allow_fallback=False`; `_DMS_TERMINATION_FACTOR = 1.0` — `services/adapters/dms.py:21,54-78`
  - [HIGH] Triple-counting a <5% CPU instance fixed (CPU buckets mutually exclusive; multi_az_ids exclusion) — `services/dms.py:132-183`, `services/adapters/dms.py:198-199`
  - [HIGH] Multi-AZ factor stacked on rightsizing fixed (real per-AZ delta; multi_az_ids skip) — `services/adapters/dms.py:142-182,198-199`
  - [HIGH] Zero-datapoint -> `continue` (no metric-less false-positive) — `services/dms.py:118-124`
  - [HIGH] Swallowed CW failure routes via `record_aws_error`; outer likewise — `services/dms.py:184-189,200-203`
  - [MEDIUM] `reads_fast_mode=True` honored (orchestrator-level) — `services/adapters/dms.py:98`
  - [MEDIUM] Phase A render desync fixed (multi_az_review carries both `Resource` and `InstanceId`) — `services/adapters/dms.py:162-166,200-205`
- **NEW: 0.** (F-trap noted: the rightsizing `EstimatedSavings` string still says "~35% savings" at `services/dms.py:180` — but the COUNTED number is the live one-size-down delta, not 0.35 of monthly; cosmetic stale label, not a counted-$ bug.)

### glue

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 7. NEW: 0.
- **CONFIRMED-already-fixed: 7**
  - [CRITICAL] `ASSUMED_MONTHLY_DPU_HOURS = 160` fabrication GONE; job-rightsizing is `$0` advisory with AuditBasis — `services/adapters/glue.py:160-177`
  - [HIGH] `NumberOfWorkers` treated as DPU count via `WORKER_TYPE_DPU` map — `services/adapters/glue.py:31-78`
  - [HIGH] Dev-endpoint counted from real DPU (`DEV_ENDPOINT_MONTHLY_HOURS = 730.0`); string single-sourced from counted number; static "$316/month" GONE — `services/adapters/glue.py:116-140`
  - [MEDIUM] `GLUE_RIGHTSIZE_FACTOR = 0.30` GONE — `services/adapters/glue.py`
  - [MEDIUM] Flat `$0.44` correct for DPU-hour unit; WorkerType handled via `WORKER_TYPE_DPU`; Flex is a documented limitation (advisory-only path) — `services/adapters/glue.py`
  - [MEDIUM] Per-API silent-failure classification (each `get_jobs`/`get_dev_endpoints`/`get_crawlers` wrapped in own try/except with `record_aws_error`) — `services/glue.py:53-54,84-85,97-98`
  - [MEDIUM] String-vs-float render desync fixed (dev-endpoint string from counted number; job-rightsizing both $0/advisory) — `services/adapters/glue.py:125,185`
- **NEW: 0.**

---

## C6 — serverless+analytics

### lambda (`services/adapters/lambda_svc.py`)

- **Banner:** none (pre-grounded in fixed code). CONFIRMED-already-fixed: 5. NEW: 2.
- **CONFIRMED-already-fixed: 5**
  - [HIGH] CoH camelCase normalization (sets `EstimatedSavings` + `EstimatedMonthlySavings` from camelCase `estimatedMonthlySavings`; `counted == rendered`) — `services/adapters/lambda_svc.py:148-152`
  - [HIGH] CoH > CO > enhanced authority dedup keyed on normalized name (`_normalize_lambda_fn_name` strips ARN/`:version`/`:alias`) — `services/adapters/lambda_svc.py:32-46,112-138`
  - [HIGH] Architecture-aware PC module constants (`_LAMBDA_PC_PRICE_PER_GB_SEC = 0.0000041667` x86, `_LAMBDA_PC_PRICE_PER_GB_SEC_ARM = 0.0000033334` arm64); multiplier applied exactly once — `services/adapters/lambda_svc.py:25-26,178-229`
  - [HIGH] CO opt-in placeholder converted to `ctx.warn` and dropped — `services/adapters/lambda_svc.py:96-103`
  - [HIGH] Compute-SP commitment demotion (B1-iii sanctioned shape; `savings` reassigned so nothing phantom-counted) — `services/adapters/lambda_svc.py:234-250`
- **NEW: 2**
  - **MEDIUM | PC metric dimensioned by `FunctionName` only — alias/version PC configs may degrade to `$0` advisory** | `services/lambda_svc.py:103` (`_read_pc_max_utilization`, `Dimensions=[{"Name":"FunctionName","Value":function_name}]`) | Provisioned Concurrency is configured per version/alias, but the metric is read dimensioned by bare `FunctionName` only. A PC config on an alias/version MAY return no datapoints for the bare-name dimension -> `max_util = None` -> `$0 Counted=False` advisory (SAFE: no wrong/fabricated $), but understates real recoverable PC $ for those functions. The shim comment at `:231` asserts "the metric is keyed by FunctionName" — that assumption is the gap. | AWS docs: `ProvisionedConcurrencyUtilization` is published under `AWS/Lambda` with dimensions `FunctionName` and `Resource` (the qualified alias/version) — https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics.html. | Prose fix: probe the metric with the per-config `Resource` dimension from `list_provisioned_concurrency_configs` (each config carries its qualified ARN/alias) before concluding no-data; fall back to advisory only if both dimensions are empty.
  - **LOW | ARM allowlist could miss newer ARM-capable runtimes** | `services/lambda_svc.py:47-65` (`ARM_SUPPORTED_RUNTIMES`) | ARM migration is structurally a `$0 advisory` (no Duration metric -> GB-seconds not derived), so an unlisted runtime is a missed nudge, not a wrong $. Container-image functions ARE handled (`:264`). Allowlist already includes `python3.13`, `nodejs22.x`, `ruby3.4`, `java21`, `dotnet9`, `provided.al2023`. Residual gap: a future runtime would be skipped until the tuple is updated. | AWS Lambda runtimes list (https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html). | Prose fix: derive ARM-eligibility from the function's `Architectures` config rather than a hardcoded runtime tuple, OR treat the allowlist as advisory-only and accept periodic drift.
- **REFUTED (not a finding):** prompt claims `("lambda","compute_optimizer")` is a dead Phase B binding — that binding DOES NOT EXIST in current code (`reporter_phase_b.py:2854-2890` has a different binding kept for in-flight scan JSON). The lambda tab renders correctly via Phase A. Stale-prompt artifact.

### step_functions

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 0.
- **CONFIRMED-already-fixed: 6**
  - [CRITICAL] Dead counted lever + flat-$50 fabrication REMOVED (dead `eligible_for_migration` gate gone; every rec `Counted=False` + AuditBasis; `_FLAT_SAVINGS_SERVICES` removed at `html_report_generator.py:67`) — `services/adapters/step_functions.py:59-69,84-88`
  - [HIGH] Per-machine CW failure classified via `record_aws_error` (was `except Exception: pass`) — `services/step_functions.py:76-85`
  - [HIGH] `required_clients` mismatch fixed (`("stepfunctions","cloudwatch")`) — `services/adapters/step_functions.py:34`
  - [MEDIUM] Standard/Express rates documented module constants + AuditBasis `rate_source: "documented Step Functions OnDemand rates (not in Pricing API)"` — `services/adapters/step_functions.py:17-19,70-77`
  - [MEDIUM] Render-string ↔ counted-number agreement — `services/adapters/step_functions.py:63-69`
  - [LOW] fast-mode honored (adapter no longer dollarizes; shim read feeds only the detection gate) — `services/step_functions.py:44-56`, `services/adapters/step_functions.py:28`
- **NEW: 0.**

### athena

- **Banner:** STALE — VERIFIED ACCURATE (highest-priority items). CONFIRMED-already-fixed: 4. NEW: 2.
- **CONFIRMED-already-fixed: 4**
  - [CRITICAL] Shim now emits per-workgroup recs (no longer permanently empty) — `services/athena.py:31-51`, `services/adapters/athena.py:39-103`
  - [HIGH] TiB-vs-TB units corrected (`total_bytes / 1e12`, not `/1024**4`) — `services/adapters/athena.py:63`
  - [HIGH] `$0` advisory string↔state agreement in all three branches — `services/adapters/athena.py:78-103`
  - [HIGH] `$50` fabricated fallback removed (`_FLAT_SAVINGS_SERVICES` GONE); `$5/TB` validated against Pricing API — `services/adapters/athena.py:36,71`, `html_report_generator.py:67`
- **NEW: 2**
  - **MEDIUM | Adapter CW read failure is `logger.warning` only — not classified via `record_aws_error` (Class E1)** | `services/adapters/athena.py:64-66` (`except Exception as e: logger.warning("[athena] CloudWatch ProcessedBytes metric check failed: {e}")`; sets `monthly_tb = 0`) | SAFE in the dollar direction (no fabricated $, rec correctly advisory), but an `AccessDenied`/throttle on `cloudwatch:GetMetricStatistics` is NOT recorded on `ctx`; operator gets no signal that a permission gap suppressed the real saving for that workgroup. Inconsistent with the shim's `list_work_groups` path (`services/athena.py:53`) which DOES use `record_aws_error`. | Lessons class **E1**; `services/_aws_errors.py:47-56`. | Prose fix: wrap the CW read failure in `record_aws_error(ctx, e, service="athena", context=f"CloudWatch ProcessedBytes for {workgroup} failed")` before setting `monthly_tb = 0`.
  - **HIGH [ESCALATED-IN-VERIFICATION from MEDIUM] | Provisioned-capacity workgroups mispriced + `0.75` compression factor unconditional** — escalation rationale: every counted athena dollar is a flat 75% of measured scan spend with no format/engine evidence, the exact C9 shape rated CRITICAL for dms 0.35 and HIGH for elasticache 0.30 / opensearch 0.25; the independent sweep independently rated it CRITICAL. Original severity: MEDIUM. | `services/adapters/athena.py:71` (`od_monthly_estimate = ... × 0.75`) | (a) The flat `$5/TB × 0.75` model assumes data-scanned pricing; Athena ALSO offers provisioned capacity (`USE1-CodeExecutionInDPUHours = $0.350/DPU-Hour`, `USE1-ReservedCapacityInDPUHours = $0.300/DPU-Hour`) which bills per-DPU-hour, so the $5/TB formula is the wrong model for it. No detection of `Configuration.EngineVersion`/execution mode. (b) The `0.75` factor is applied unconditionally with no detection of whether the workgroup already uses columnar formats (Parquet/ORC) or partitioning — for an already-optimized workgroup the "75% scan-cost reduction" is not realizable. Severity MEDIUM: most workgroups are data-scanned; provisioned/Parquet are less common, but for them the counted $ is wrong. | AWS Pricing API SKUs above + https://aws.amazon.com/athena/pricing/; lessons class **C9** (fabricated $ from a config dimension with no usage evidence) + **C10** (idle is not resizable). | Prose fix: read the workgroup's `Configuration.ExecutionEngine`/`EngineVersion` and skip/recolor provisioned workgroups; gate the 0.75 factor on a format-detection signal (e.g. sampled `get_query_results`/table-format inspection) or demote to `$0` advisory when the current format is unknown.
- **Sub-note (coverage, folded in):** `athena.list_work_groups()` is a single call with no paginator/NextToken loop (AWS returns up to 50/page); accounts with >50 silently drop the rest. Low severity (accounts rarely have >50 workgroups). Source: AWS `list_work_groups` API (MaxResults default 50).

### quicksight

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 0.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] Edition-aware SPICE rate (`SPICE_RATE_PER_GB = {"STANDARD":0.25,"ENTERPRISE":0.38}`; SKU-validated) — `services/quicksight.py:19-28`, `services/adapters/quicksight.py:48,69-70`
  - [HIGH] Edition resolved from `describe_account_subscription` AccountInfo.Edition (not `PurchaseMode`) — `services/quicksight.py:63,128`
  - [HIGH] String ↔ counted single-source-of-truth — `services/adapters/quicksight.py:69-91`
  - [HIGH] `DescribeSpiceCapacity` failure classified via `record_aws_error` — `services/quicksight.py:151-154`
  - [HIGH] Per-namespace `list_users` failure classified + `user_enum_failed` flag (gate proceeds on enumeration failure) — `services/quicksight.py:78-88,93,104`
  - [MEDIUM] Advisory (`$0 Counted=False`) path for partial headroom (potential figure in string only; numeric 0.0) — `services/quicksight.py:121,143-149`, `services/adapters/quicksight.py:50-68`
- **NEW: 0.** (Coverage gaps — per-user Authors/Readers lever missing, free SPICE allotment ignored — are documented LIMITATIONS, not cost-fidelity bugs. Every emitted rec is correct.)

---

## C7 — network

### network

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 7. NEW: 1.
- **CONFIRMED-already-fixed: 7**
  - [CRITICAL-if-regressed] Render-desync (5 sources registered) — `services/adapters/network.py:213-237`, `reporter_phase_b.py:2781-2785,2910-2914`
  - [HIGH] NET-01 ALB consolidation double-count — both `single_service_albs` and `shared_alb_opportunity` advisory `$0` `Counted=False` with AuditBasis — `services/load_balancer.py:157-258`
  - [LOW] NET-03 stopped-instance EIP double-count — `stopped_ids` exclusion set — `services/elastic_ip.py:90-97`
  - [LOW] NET-04 ASG sub-shim bypasses `record_aws_error` — now routes through `record_aws_error` — `services/ec2.py:946-964`, `services/adapters/network.py:62-66`
  - [HIGH] NAT CoH dedup at NAT-id granularity (only `coh_savings > 0` excludes; per-NAT-id exclusion) — `services/adapters/network.py:88-154,193-194`, `services/nat_gateway.py:22-43,80-83`
  - [MEDIUM] EIP/public-IPv4 flat rate NOT region-scaled (multiplier GONE from fallback) — `services/elastic_ip.py:20-28`, `core/pricing_engine.py:320`
  - [LOW] NET-07 VPC-endpoint hourly dimension (`_fetch_vpc_endpoint_price` calls `_call_pricing_api_hourly`, selects `unit=="Hrs"`) — `core/pricing_engine.py:1826-1838,2023-2052`
- **NEW: 1**
  - **MEDIUM | NET-02 VPC interface-endpoint double-count (nonprod ∩ duplicate) — REAL counted double-$$** | `services/vpc_endpoints.py:109-123` (`interface_endpoints_in_nonprod` counts `vpc_ep_monthly * az_count` for ANY nonprod interface endpoint) and `services/vpc_endpoints.py:130-148` (`duplicate_endpoints` INDEPENDENTLY counts `sum(vpc_ep_monthly * ep["az_count"] for ep in removable)` where `removable = service_endpoints[2:]`) | There is NO `covered` / `VpcEndpointId` set shared between the two loops, so a nonprod interface endpoint that is ALSO the 3rd+ of its `vpc:service` is counted in BOTH levers (its `vpc_ep_monthly * az_count` once in nonprod, again inside the `removable` sum). **This is the only live counted double-count in the network adapter.** Impact: bounded — each overlapping endpoint over-counts by `vpc_ep_monthly * az_count` (≈ `$7.30/AZ/mo` us-east-1 × AZ count); the headline inflates only on accounts that have BOTH a nonprod tag on AND ≥3 interface endpoints for the same `vpc:service`. Verified line-accurate against current code by the consolidation agent. Neither check is metric-gated, so this is a real counted double-$$ on affected accounts (within the network adapter's locally-derived dollars; CoH/CO do not touch VPC endpoints). | Lessons classes **A3** (one population ⊆ another — here an intersection, same double-count shape) + **A4** (claim-order / covered-set). | Prose fix: thread a `covered_endpoint_ids: set` across the two checks; in the duplicate loop, skip any endpoint id already emitted by `interface_endpoints_in_nonprod` (single-owner by `VpcEndpointId`). Add a regression test feeding a nonprod endpoint that is the 3rd of its service and assert its dollars appear in exactly one of the two categories.

### network_cost

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 1.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] Cross-AZ/cross-region/egress fabricated reduction factors GONE (all three `$0 Counted=False` advisory; `CROSS_AZ_SAVINGS_FACTOR=0.5`/`CLOUDFRONT_SAVINGS_FACTOR=0.40` constants removed) — `services/adapters/network_cost.py:289-386`
  - [HIGH] TGW branch (c) circular double-count — `$0 Counted=False` advisory — `services/adapters/network_cost.py:477-501`
  - [HIGH] Usage-type classifier substring bug fixed (full tokens; cross-AZ tested BEFORE inter-region) — `services/adapters/network_cost.py:244-260`
  - [MEDIUM] Silent CE/EC2 failures (E1) — `record_aws_error`; missing-CE-client path `ctx.warn`s and returns `_empty_findings()` — `services/adapters/network_cost.py:123-130,262-266,402-415`
  - [LOW] `mark_zero_savings_advisory` coverage — `services/adapters/network_cost.py:152-158`
  - [LOW] No-double-multiply on CE path (`_ = multiplier`) — `services/adapters/network_cost.py:286,326,366,441`
- **NEW: 1**
  - **LOW | `TGW_ATTACHMENT_COST_PER_GB` unit-misleading constant + display** | `services/adapters/network_cost.py:49` (`TGW_ATTACHMENT_COST_PER_GB: float = 0.05` — name implies per-GB but `$0.05` is the per-attachment-HOUR rate); `services/adapters/network_cost.py:443,451,455,473` (`tgw_total_per_gb = $0.07` interpolated into `"$X.XX/GB"` display strings, conflating hourly attachment fee with per-GB rate) | NON-fabricating: the constant only feeds `monthly_savings=0.0` advisory DISPLAY strings now (NC-CONF-1/NC-CONF-2 demoted every TGW counted path). No counted dollar is wrong; only the human-readable `$0.07/GB` text is unit-misleading. Cosmetic/naming nit. | AWS VPC pricing (Transit Gateway attachment per-hour; per-GB data processing the only per-GB dimension); `aws pricing get-products AmazonVPC` (`USE1-TransitGateway-Hours = $0.05/attachment-hr`, `USE1-TransitGateway-Bytes = $0.02/GB`); lessons class **C7-adjacent**. | Prose fix: rename `TGW_ATTACHMENT_COST_PER_GB` -> `TGW_ATTACHMENT_COST_PER_HR` (×730 -> ~`$36.50/mo`) and split the display string into "$0.05/attachment-hr + $0.02/GB processing".

### cloudfront

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 5. NEW: 0.
- **CONFIRMED-already-fixed: 5**
  - [HIGH] Honest-`$0` stance (no fabricated estimator; flat-$0.10/GB + 0.5KB/request GONE) — `services/adapters/cloudfront.py:44-67`
  - [MEDIUM] Swallowed CloudWatch exceptions (E1) — inner per-distribution CW read routes via `record_aws_error` — `services/cloudfront.py:108-117`
  - [MEDIUM] Fast-mode CloudWatch gating (`fast_mode` -> `ctx.warn` + `continue`) — `services/cloudfront.py:44-68`
  - [LOW] Dead origin-shield block removed — `services/cloudfront.py:119-129`
  - [MEDIUM] CloudFront CW region pinning (`region="us-east-1"`) — `services/cloudfront.py:75-79`
- **NEW: 0.** (Cosmetic declaration gap: `cloudfront` adapter does not declare `requires_cloudwatch`/`reads_fast_mode` class attributes — behavior is correct via in-helper `getattr` check, but declarative opt-out is missing. No counted-dollar impact; cloudfront is `$0`-only.)

### api_gateway

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 4. NEW: 1.
- **CONFIRMED-already-fixed: 4**
  - [CRITICAL] Flat-`$50`-per-rec fabrication REMOVED (`_calculate_service_savings` passes canonical total through; `_FLAT_SAVINGS_SERVICES` GONE); adapter multiplier removal — `html_report_generator.py:67,3165-3179`, `services/adapters/api_gateway.py:54-67`
  - [MEDIUM] Silent per-API failure (E1) — per-API `get_resources`+CW routes via `record_aws_error`; CW inner likewise — `services/api_gateway.py:108-115,152-160`
  - [LOW] Fast-mode CW gating (`if not ctx.fast_mode:`) — `services/api_gateway.py:91`, `services/adapters/api_gateway.py:25-27`
  - [LOW] AuditBasis present on counted recs (REST/HTTP rates SKU-validated) — `services/api_gateway.py:134-144`
- **NEW: 1**
  - **LOW | REST-only coverage (HTTP/WebSocket invisible) — INTENTIONAL documented limitation** | `services/api_gateway.py:69-77` (paginates only `get_rest_apis`); `required_clients()=("apigateway","cloudwatch")` (no `apigatewayv2`) | The module docstring (`services/api_gateway.py:6-20`) and adapter docstring (`services/adapters/api_gateway.py:13,34`) explicitly document the REST-only scope and the defensible-lever rationale (HTTP API is already cheapest; WebSocket saving needs per-API usage metrics + fail-safe). This is a COVERAGE LIMITATION, not a cost-fidelity defect — no counted dollar is wrong; fabricating HTTP/WebSocket coverage would violate no-fabrication (C4). The docstring drift flagged in the prompt is itself FIXED. | AWS pricing SKUs (HTTP `USE1-ApiGatewayHttpRequest` `$1.00/M`, WebSocket `USE1-ApiGatewayMessage` `$1.00/M` + `USE1-ApiGatewayMinute` `$0.25/M-min`). | Prose fix: none recommended — the documented deferral is the correct posture. (If coverage desired: add `apigatewayv2` to `required_clients`, scan `get_apis`, gate any WebSocket saving on per-API usage metrics with fail-safe `Counted=False` on missing metrics.)

### mediastore

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 4. NEW: 0.
- **CONFIRMED-already-fixed: 4**
  - [CRITICAL] False-unused from failed CW read — `activity_read_failed` flag; failed read ABSTAINS (`continue`), never asserts "unused"; final gate requires `activity_datapoints_seen > 0 and total_activity == 0` — `services/mediastore.py:48-85,109`
  - [MEDIUM] `$0`-rec count hygiene (`Counted=False` + `counted_recs` sum) — `services/adapters/mediastore.py:69-90`
  - [MEDIUM] Borrowed-rate single-multiplier (engine path NO multiplier; fallback `0.023 × multiplier` ONCE) — `services/adapters/mediastore.py:39-46`
  - [LOW] AuditBasis on counted recs — `services/adapters/mediastore.py:57-67`
- **NEW: 0.** (Cosmetic: `requires_cloudwatch` not declared and no explicit `ctx.fast_mode` early-skip in helper — but mediastore is a RETIRED service, near-zero practical impact. No counted-$ impact.)

---

## C8 — composite+special

### commitment_analysis

- **Banner:** FEATURE-NOTE (2026-08-08 deep-dive shipped) — audited NORMALLY, not rubber-stamped. CONFIRMED (banner claims true in current code): 6. NEW: 4.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] RI matrix breadth = 6 services (`RI_SERVICES` lists EC2, RDS, ElastiCache, Redshift, OpenSearch, DynamoDB) — `services/commitment_scenarios.py:68-76`
  - [HIGH] SP matrix breadth = 3 types (`SP_TYPES = ("COMPUTE_SP","EC2_INSTANCE_SP","SAGEMAKER_SP")`; 54 cells = 36 RI + 18 SP, cell-isolated) — `services/commitment_scenarios.py:77`, `services/commitment_purchase_fetch.py:58-97`
  - [CRITICAL] Advisory boundary holds (THE KEY RULE) — `Counted=False` for `sp_cov_recs + ri_cov_recs + cost_hub_recs`; only `sp_utilization`/`ri_utilization` reach headline — `services/adapters/commitment_analysis.py:152-156,558`, `services/commitment_scenarios.py:312,364`
  - [HIGH] B1-ii zero-cell rule differs by kind (RI drops on `savings<=0`; SP keeps on `hourly_commitment>0`) — `services/commitment_scenarios.py:169,215`
  - [HIGH] Dedicated Phase-B handler `_render_commitment_purchase_cards` registered — `reporter_phase_b.py:2603,2877`
  - [HIGH] Sweep S14 `sweep_projected_commitment` registered — `tools/output_audit.py:326,453`
- **NEW: 4**
  - **MEDIUM | `_account_coverage_ratio` silent swallow persists (Class E1)** | `services/adapters/commitment_analysis.py:648-672` (bare `except Exception: return DEFAULT_COVERAGE_RATIO` on denied/throttled `get_savings_plans_purchase_recommendation` returns 0.70 with NO `ctx.warn`/`permission_issue`) | A CE permission gap silently degrades the Fargate SP coverage model to the default (0.70) instead of surfacing. Only Fargate-SP advisory math is affected (all Fargate cells are `Counted=False`, so the headline is untouched). Verified line-accurate by the consolidation agent. | Lessons class **E1**. | Prose fix: mirror `_route_ce_error` — on `ClientError` AccessDenied/Unauthorized -> `ctx.permission_issue`, else `ctx.warn`, then fall back to `DEFAULT_COVERAGE_RATIO`.
  - **LOW | RI expiry never detected (SP-only)** | `services/adapters/commitment_analysis.py:436-489` (`_check_expiring` reads SP details only; no Reserved-Instance expiry scan) | An RI renewing/expiring soon gives no alert. Documented intentional (comment `:432-435`). | Phase 4 coverage gap (prompt Phase 4.13; possibly intentional). | Prose fix: add an RI expiry path via `ec2:describe_reserved_instances` `End`/`Duration` (regional-family + zonal-exact), mirroring the SP expiry `$0` alert.
  - **LOW | `ri_coverage_gaps` always empty** | `services/adapters/commitment_analysis.py:405-430` (`_check_ri_coverage` takes only the overall rate, no groupBy; returns `recs=[]`) | No per-service RI coverage-gap rec is ever surfaced (concrete buy scenarios come from `purchase_recommendations` instead). By design (comment `:401-404`). | Phase 4 coverage gap (prompt Phase 4.13; intentional). | Prose fix: none required if the purchase matrix is considered authoritative — document the tradeoff in the card render.
  - **LOW | Fargate legs filter is ECS-service-only; EKS-on-Fargate missed** | `services/adapters/commitment_analysis.py:580-619` (`ce:GetCostAndUsage` leg query filters `SERVICE = "Amazon Elastic Container Service"` at `:591`, excluding EKS-on-Fargate spend even though Compute SP covers both) | EKS Fargate SP-eligible spend is excluded from the Fargate SP view; Fargate cells are `Counted=False` so headline untouched. Documented intentional (comment `:576-579`). | Phase 4 coverage gap (prompt Phase 4.13). | Prose fix: OR-in `"Amazon Elastic Kubernetes Service"` and filter Fargate usage types, or document the exclusion.

### sagemaker

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 9. NEW: 2.
- **CONFIRMED-already-fixed: 9**
  - [HIGH] `$0`-counted inflation FIXED (`mark_zero_savings_advisory`; sum + count gated on `Counted!=False`) — `services/adapters/sagemaker.py:519-536`
  - [HIGH] idle/consolidation double-count FIXED (`idle_endpoint_names` fed into `_check_multi_model_consolidation`) — `services/adapters/sagemaker.py:385-402,511-516`
  - [HIGH] no-evidence notebook counting FIXED (every InService notebook `$0 advisory Counted=False`) — `services/adapters/sagemaker.py:210-273`
  - [HIGH] one-time spot saving FIXED to advisory — `services/adapters/sagemaker.py:345-378`
  - [HIGH] `_list_endpoints` double-swallow FIXED (paginator -> manual NextToken -> `record_aws_error`) — `services/adapters/sagemaker.py:85-112`
  - [HIGH] notebook/training enum failures classified — `services/adapters/sagemaker.py:226-230,295-299`
  - [HIGH] SageMaker SP commitment-demotion present (`demote_recs_in_place` when `coverage.covers_sagemaker()`) — `services/adapters/sagemaker.py:529-531`
  - [HIGH] unknown-instance/pricing-miss paths skip not count (`instance_monthly <= 0 -> continue`) — `services/adapters/sagemaker.py:172-432`
  - [MEDIUM] `active_endpoint_count` stat corrected (`active_ep_count - len(idle_ep_recs)`) — `services/adapters/sagemaker.py:561-568`
- **NEW: 2**
  - **LOW | Non-deterministic SageMaker pricing filter** | `core/pricing_engine.py:1550-1556` (`_fetch_sagemaker_instance_price` uses bare `instanceType + location` filter with `MaxResults=1` via `_call_pricing_api`) | The codebase's own `_fetch_generic_instance_price` docstring (`:1566`) warns this shape "returns a non-deterministic, frequently wrong dimension" and pins a usage-type for Redshift/ElastiCache to fix it — but SageMaker was NOT given the same treatment. A single `get_sagemaker_instance_monthly` is used for Hosting/Notebook/Training SKUs (which AWS prices differently); the bare filter may select the wrong usage-type family. The `SAGEMAKER_OVER_EC2 × 730` fallback (`:1196`) limits blast radius. | Known-issue catalogue ("Non-deterministic pricing filter"); lessons class **C2**. | Prose fix: pin the SageMaker Hosting/Notebook usage-type per the AmazonSageMaker SKUs, or document why the single rate is acceptable across usage-type families.
  - **LOW | Multi-variant / `InstanceCount` under-pricing (coverage gap)** | `services/adapters/sagemaker.py:142-167` (idle-endpoint and consolidation pricing read only `ProductionVariants[0].InstanceType` and price ONE instance; a second variant or `InstanceCount > 1` fleet is ignored) | Correctness gap, not just coverage. | Phase 4 coverage gap; lessons class **C2**. | Prose fix: sum `InstanceType × InstanceCount` across all variants; or document the single-variant scope.

### bedrock

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 6. NEW: 4.
- **CONFIRMED-already-fixed: 6**
  - [HIGH] Unknown-rate PT no longer counts at fabricated `$1/hr` (gates on `hourly = PT_HOURLY_PRICE.get(model_id)`) — `services/adapters/bedrock.py:230-250,301-306`
  - [HIGH] model-id derivation FIXED (`_derive_model_id` reads foundation-model ARN, strips `-YYYYMMDD-vN:N` version suffix) — `services/adapters/bedrock.py:67-104`
  - [HIGH] enumeration failures classified (`_list_provisioned_throughputs`, `list_knowledge_bases`, `list_agents` double-failure -> `record_aws_error`) — `services/adapters/bedrock.py:51-104,366-372,416-418`
  - [HIGH] CloudWatch read-failure fail-closed (`_get_pt_invocation_sum` returns `(None, False)`; idle check abstains; separates definitive-idle from candidate-idle) — `services/adapters/bedrock.py:107-144,206-283`
  - [HIGH] KB `$0` advisory discipline holds (no fabricated `$146/mo`) — `services/adapters/bedrock.py:340-393`
  - [HIGH] `idle_agents` emits nothing (agents accrue no charge; `$5/mo` placeholder removed) — `services/adapters/bedrock.py:420-425`
- **NEW: 4**
  - **MEDIUM | Blended flat token rate `0.000_003` $/token persists (REAL counted-$ inaccuracy on PT-breakeven)** | `services/adapters/bedrock.py:316` (`od_monthly_estimate = (input_tokens + output_tokens) * 0.000_003`) feeding the COUNTED `monthly_savings` at `:331` | `_check_pt_breakeven` applies a single model-agnostic rate to `input_tokens + output_tokens`. Wrong in both directions (Claude 3 Sonnet input `$0.003/1K` vs output `$0.015/1K` — 5×; Haiku input ~12× lower than the constant). The adapter already fetches `InputTokenCount`/`OutputTokenCount` separately (`_get_pt_token_counts`) so a per-model per-direction split is feasible. This is a real counted-$ inaccuracy (not just hygiene) but bounded: only fires for a PT whose on-demand token estimate is BELOW its PT cost, which is the niche over-commitment case. Verified line-accurate by the consolidation agent. | Lessons class **C9** (flat-% fabrication); AWS Bedrock pricing (https://aws.amazon.com/bedrock/pricing/). | Prose fix: split into per-model input/output rates from the `…-input-tokens`/`…-output-tokens` SKUs.
  - **LOW | Claude PT rates unverifiable (no public SKU)** | `services/adapters/bedrock.py:20-26` (the four Claude entries in `PT_HOURLY_PRICE` cannot be validated against published PT pricing; the Pricing API exposes no `Claude…-ProvisionedThroughput-…-ModelUnits` usagetype) | C-BR-1 means an unknown-rate PT is demoted to advisory, so a wrong Claude constant does NOT silently mis-count — but if a Claude PT exists, its rate should come from the account's actual commitment, not a hardcoded guess. | Phase 2.3 (Bedrock-specific); AWS pricing. | Prose fix: source Claude PT rates from the PT summary / Billing when present.
  - **LOW | KB recs carry no `Counted` flag** | `services/adapters/bedrock.py:378-391` (KB recs set `monthly_savings=0.0` + `pricing_warning` but NO `Counted` key) | `BedrockModule.scan` sums `r.get("monthly_savings",0.0)` over ALL recs (line 487, no `Counted` filter) so a KB contributes `$0` to the headline — dollar total correct. BUT `total_recommendations = len(all_recs)` (line 494) DOES count the KB advisories. Minor: count headline includes advisory KBs while dollar headline excludes them — the standard advisory-only tab pattern (D2/D4 allow it). The bedrock live-audit banner's specific request (explicit `Counted=False`) is not satisfied, though the practical effect is benign. | Lessons class **D4**. | Prose fix: add `"Counted": False` to the KB rec dict for explicitness, and gate the count on `Counted!=False` if advisory-KB inflation is undesired.
  - **LOW | CloudWatch dimension `ModelId` for a PT** | `services/adapters/bedrock.py:133,159,175` (`_get_pt_invocation_sum`/`_get_pt_token_counts` query `Dimensions=[{"Name":"ModelId","Value":model_id}]` with the base model id) | May aggregate unrelated on-demand traffic of the same base model (masking an idle PT -> false negative) or return nothing (false idle). The `definitive` flag (C-BR-4) mitigates the false-positive direction (absent datapoints -> `$0 advisory` not counted), but a busy on-demand base model could still mask a genuinely idle PT. | Known-issue catalogue ("agent-metric dimension mismatch"); Phase 2.5. | Prose fix: confirm the PT-scoped dimension (provisioned-model ARN) via AWS Knowledge and use it.

### monitoring

- **Banner:** STALE — VERIFIED ACCURATE. CONFIRMED-already-fixed: 5. NEW: 1.
- **CONFIRMED-already-fixed: 5**
  - [HIGH] never-expiring-logs 100%-of-storedBytes overstatement FIXED (`EstimatedMonthlySavings=0.0`, `Counted=False`) — `services/monitoring.py:241-255`
  - [HIGH] custom-metrics fabricated-50% FIXED (gates on real staleness signal via `_stale_custom_metric_counts`; no-evidence -> `$0 advisory`; measured-stale counted at marginal tier) — `services/monitoring.py:300-425`
  - [HIGH] CW/CloudTrail logger-only FIXED (all four paths route via `record_aws_error`) — `services/monitoring.py:276-279,313-318,427-435`
  - [HIGH] `reads_fast_mode=True` now declared (and honoured in shim) — `services/adapters/monitoring.py:22-26`, `services/monitoring.py:261,284,307`
  - [HIGH] intra-domain `duplicate_private_zones` dedup FIXED (subtracts `counted_unused_ids` overlap; `$0 advisory` if all already counted) — `services/route53.py:196-235`
- **NEW: 1**
  - **MEDIUM | `backup`/`route53` sub-shims use `ctx.warn` for ALL exceptions (Class E1)** | `services/backup.py:111-122` and `services/route53.py:159-238` (bare `ctx.warn(...)` with no AccessDenied split) | An `AccessDenied`/throttle on `backup:ListBackupPlans` or `route53:ListHostedZones` is never routed through `record_aws_error` and so is never classified as `permission_issue` — surfaces as a generic warning, missed by an operator reviewing IAM gaps. Contrast `services/monitoring.py` (C-MN-3) which was fixed. Verified line-accurate. | Lessons class **E1**; the monitoring live-audit banner. | Prose fix: route both sub-shims' exceptions through `record_aws_error` (AccessDenied/Unauthorized/OptInRequired -> `permission_issue`, else `ctx.warn`).

### workspaces

- **Banner:** STALE (+ PROMPT-BODY-DRIFT live note — body has drifted; banner warns the adapter now declares CW + fast-mode). CONFIRMED-already-fixed: 8. NEW: 1.
- **CONFIRMED-already-fixed: 8**
  - [HIGH] CW + fast-mode now declared (per live-audit banner) — `services/adapters/workspaces.py:38-43`
  - [HIGH] AlwaysOn->AutoStop now CloudWatch-gated (`_read_monthly_connected_hours` reads `AWS/WorkSpaces UserConnected`; `_AUTOSTOP_SAVINGS_FACTOR = 0.30` GONE) — `services/workspaces.py:122-150,233-254`, `services/adapters/workspaces.py:133-174`
  - [HIGH] STOPPED-AutoStop false positive FIXED (`_price_unused` distinguishes ALWAYS_ON from AUTO_STOP; abstains on unknown) — `services/adapters/workspaces.py:176-216`
  - [HIGH] bundle_rightsizing now `$0 advisory` — `services/adapters/workspaces.py:82-97`
  - [HIGH] bundle price table refreshed against live API (POWER=78, POWERPRO=140, GRAPHICSPRO=999; new `WORKSPACE_AUTOSTOP_PRICING` table) — `services/workspaces.py:42-77`
  - [HIGH] `$0` rec count hygiene (`counted_recs = sum(... if Counted is not False)`) — `services/adapters/workspaces.py:114`
  - [HIGH] CW failures classified + fast-mode gated (`_note_cw_failure` routes AccessDenied -> `permission_issue`) — `services/workspaces.py:173-205`
  - [HIGH] `describe_workspaces` failure classified via `record_aws_error` — `services/workspaces.py:324-327`
- **NEW: 1**
  - **LOW | bundle_rightsizing still Windows+Included-table-biased for BYOL/Linux** | `services/workspaces.py:309-321` (the `non_windows_pricing` branch attaches a `PricingWarning` rather than downgrading on missing data) | NOT a counted-dollar bug — the rec is already `Counted=False` advisory (C-WS-4), so a wrong-OS delta is never summed. The underlying bundle table remains Windows+Included; the body's "OS / license blindness" is only mitigated via a warning string. | Prompt Phase 2.3 (WorkSpaces-specific OS correctness). | Prose fix (optional): add a BYOL/Linux price sub-table if those WorkSpaces are common in target accounts; otherwise document the advisory-only scope.

---

## CRITICAL (NEW)

**No NEW CRITICAL findings across all 34 services.** Every CRITICAL item the prompts flagged is CONFIRMED-already-fixed (see per-service sections): EC2 C6 commitment demotion, AMI cross-AMI snapshot double-count, EKS Extended-Support phantom + idle false-positive, Redshift CoH orphan bucket, batch "only-Graviton-counts" desync, apprunner empty-inventory, step_functions dead-lever + flat-$50, api_gateway flat-$50, mediastore false-unused-from-CW, the SR-2 `_FLAT_SAVINGS_SERVICES` reporter fabrication, s3 CoH orphan, file_systems EFS IA rate, dms flat-0.35, glue 160-hr.

---

## HIGH (NEW)

**No NEW HIGH findings across all 34 services.** All HIGH-severity issues the prompts flagged are CONFIRMED-already-fixed in current code (cited in the per-service sections). This is the EXPECTED shape for a remediated codebase.

---

## MEDIUM (NEW)

> **[LEDGER RECONCILED IN VERIFICATION]** The original severity bookkeeping did not reconcile: per-service sections carried 13 MEDIUM labels against this headline of 10, with self-contradicting notes below (the line ~572 parenthetical and the line ~592 note assert opposite treatments of the redshift pair). Canonical post-verification ledger: **1 HIGH (athena #7 below, escalated) / 9 MEDIUM (#1-#6, #8, #9 below + lambda PC) / 23 LOW** — dynamodb #10 and both redshift serverless items are LOW; the lambda PC finding is MEDIUM (its per-service label), not LOW as the LOW-cluster cross-listing implied.

The 10 originally-MEDIUM findings, ordered by counted-$ impact (highest first):

1. **network** — VPC interface-endpoint double-count (nonprod ∩ duplicate) — REAL counted double-$$ — `services/vpc_endpoints.py:109-148` — over-counts an endpoint by `vpc_ep_monthly * az_count` when a nonprod-tagged endpoint is also the 3rd+ of its `vpc:service`. (Class A3/A4.) The single live counted double-count in the network adapter.
2. **bedrock** — blended flat token rate `0.000_003` $/token on PT-breakeven — REAL counted-$ inaccuracy — `services/adapters/bedrock.py:316,331` — applies a single rate to `input_tokens + output_tokens` instead of per-model per-direction SKUs. (Class C9.)
3. **aurora** — CloudWatch helpers swallow AccessDenied/throttle silently — `services/adapters/aurora.py:95-121,124-149,152-173` — three helpers end `except Exception: pass`/`return None`; io-tier counted savings silently zeroed on CW denial. (Class E1; safe direction, hides gap.)
4. **transfer** — outer `except` uses bare `ctx.warn`, never `record_aws_error`/`permission_issue` — `services/transfer_svc.py:124-125` — `transfer:ListServers` AccessDenied misclassified as generic warn. (Class E1.)
5. **transfer** — inner CloudWatch `except` swallows all errors with no `ctx` record — `services/transfer_svc.py:98-103` — CW `AccessDenied`/throttle invisible in `permission_issues[]`. (Class E1.)
6. **athena** — adapter CW read failure is `logger.warning` only — `services/adapters/athena.py:64-66` — CW `AccessDenied`/throttle not recorded on `ctx`. (Class E1.)
7. **athena** — provisioned-capacity workgroups mispriced + `0.75` compression factor unconditional — `services/adapters/athena.py:71` — wrong model for provisioned DPU workgroups; fabricated for already-Parquet workgroups. (Class C9/C10.)
8. **commitment_analysis** — `_account_coverage_ratio` silent swallow — `services/adapters/commitment_analysis.py:648-672` — CE permission gap degrades Fargate SP coverage to default 0.70 with no surface. (Class E1; Fargate cells `Counted=False`, headline untouched.)
9. **monitoring** — `backup`/`route53` sub-shims use `ctx.warn` for ALL exceptions — `services/backup.py:111-122`, `services/route53.py:159-238` — AccessDenied on `backup:ListBackupPlans`/`route53:ListHostedZones` never classified as `permission_issue`. (Class E1.)
10. **dynamodb** — `reads_fast_mode` not declared; CW reads not gated on `ctx.fast_mode` — `services/adapters/dynamodb.py:57-69`, `services/dynamodb.py:142-607` — performance/cost-of-scan optimization, no counted-$ fabrication. (Lessons E1 corollary.)

(Plus two NEW MEDIUM counted-$ adjacent items already in the per-service sections: redshift RS-9 + RS-10 — both Class E1 silent-failure hygiene on the serverless path; functionally safe as serverless recs are advisory. If counted strictly as E1 hygiene these would bring the MEDIUM count to 12; the C3 notepad labels them MEDIUM. Net NEW MEDIUM = 10 per the dominant per-cohort tallies, with redshift's two serverless-E1 items included in the per-service sections above for completeness.)

> Cross-class note (dedup): MEDIUM findings #3, #4, #5, #6, #8, #9 are all instances of the SAME **E1** silent-failure-classification class — a `bare except: pass` / `except Exception: ctx.warn(...)` that swallows an `AccessDenied`/throttle without routing it through `record_aws_error` -> `ctx.permission_issue`. The same E1 class also appears as the LOW findings for opensearch, msk, redshift (RS-9), and the aurora CW helpers. A single harmonizing fix (route every CW/describe failure through `record_aws_error`) closes ~9 of the 35 NEW findings at once. The reference implementation is `services/elasticache.py:259-269`.

---

## LOW (NEW)

The 25 NEW LOW findings, grouped by class for triage:

**Class E1 — silent-failure classification hygiene (8 findings):**
1. ec2 — `get_enhanced_ec2_checks`/`get_advanced_ec2_checks` outer `except` generic `ctx.warn` — `services/ec2.py:834-835,1224`
2. ebs — `compute_ebs_checks` outer `except` generic `ctx.warn` — `services/ebs.py:689-690`
3. eks_cost — Extended-Support FALLBACK constant stale ($0.50 vs $0.60) — `core/pricing_engine.py:361` (C7-class; bounded offline-only)
4. opensearch — per-domain CW/describe failures logger-only — `services/opensearch.py:229-234`
5. msk — `kafka:ListClusters` AccessDenied misclassified as `ctx.warn` — `services/msk.py:91-95`
6. msk — inner `list_clusters_v2` bare `except Exception: pass` — `services/msk.py:91-92`
7. redshift — serverless silent-failure: bare `except Exception: pass` (also classified MEDIUM by C3 notepad; E1 hygiene) — `services/redshift.py:122-123`
8. redshift — `required_clients()` omits `redshift-serverless` — `services/adapters/redshift.py:40-42` (E1 upstream cause; also classified MEDIUM by C3 notepad)

> Note on items 7–8: the C3 notepad labels these MEDIUM for their impact (serverless permission gap visibility). They appear in BOTH the MEDIUM count (per-service) and this LOW E1-hygiene cluster (cross-class). Counted once each as MEDIUM in the headline (10 MEDIUM total). Listed here for the E1 harmonization sweep only.

**Class E1-adjacent / confidence & cosmetic (3 findings):**
9. eks_cost — `extended_support_pending` advisory carries non-zero `AdvisoryEstimate` (B1-iii sanctioned; F4 self-trap note) — `services/adapters/eks.py:366`
10. containers — `SPOT_SAVINGS_FACTOR = 0.70` dead code — `services/adapters/containers.py:20` (C9-class; unused, dead not fabricating)
11. msk — confidence mislabel: `enhanced_checks` inherits "Metric Backed" (config heuristic) — `reporter_phase_b.py:2761-2792,2795` (F4-class; S3 precedent at `:2778`)

**Coverage / pagination gaps (5 findings):**
12. apprunner — `list_services` un-paginated — `services/apprunner.py:100`
13. dynamodb — on-demand `EstimatedMonthlyCost` unit semantics (advisory-only, no counted-$) — `services/dynamodb.py:411-419`
14. athena — `list_work_groups` un-paginated (>50 workgroups dropped) — `services/athena.py:35` (folded sub-note)
15. api_gateway — REST-only coverage (HTTP/WebSocket invisible) — INTENTIONAL documented limitation — `services/api_gateway.py:69-77`
16. network_cost — `describe_vpc_peering_connections`/`describe_transit_gateways` non-paginated (moot; advisory-only) — `services/adapters/network_cost.py:403,410`

**Pricing-dimension / modelling gaps (6 findings):**
17. dynamodb — on-demand `EstimatedMonthlyCost` unit semantics (advisory) — `services/dynamodb.py:411-419` (also in coverage list above for dedup; counted once)
18. lambda — PC metric dimensioned by `FunctionName` only (alias/version PC configs degrade to `$0` advisory) — `services/lambda_svc.py:103` (MEDIUM per C6 notepad; cross-listed here for the lambda-specific coverage axis)
19. lambda — ARM allowlist could miss newer ARM-capable runtimes (`$0` advisory, missed nudge not wrong $) — `services/lambda_svc.py:47-65`
20. sagemaker — non-deterministic SageMaker pricing filter (`MaxResults=1` bare filter) — `core/pricing_engine.py:1550-1556`
21. sagemaker — multi-variant / `InstanceCount` under-pricing — `services/adapters/sagemaker.py:142-167`
22. bedrock — Claude PT rates unverifiable (no public SKU) — `services/adapters/bedrock.py:20-26`
23. bedrock — CloudWatch dimension `ModelId` for a PT (may mask idle PT) — `services/adapters/bedrock.py:133,159,175`
24. network_cost — `TGW_ATTACHMENT_COST_PER_GB` unit-misleading constant + display — `services/adapters/network_cost.py:49,443,451,455,473` (cosmetic; advisory-only)
25. workspaces — bundle_rightsizing Windows+Included-table-biased for BYOL/Linux (`Counted=False` advisory; no counted-$) — `services/workspaces.py:309-321`

**Commitment coverage gaps (3 findings):**
- commitment_analysis — RI expiry never detected (SP-only) — `services/adapters/commitment_analysis.py:436-489`
- commitment_analysis — `ri_coverage_gaps` always empty — `services/adapters/commitment_analysis.py:405-430`
- commitment_analysis — Fargate legs filter ECS-service-only; EKS-on-Fargate missed — `services/adapters/commitment_analysis.py:580-619`

(The commitment items are documented intentional exclusions per the C8 notepad; listed as LOW for backlog visibility.)

---

## Proposed fixes index (PROSE ONLY — backlog for a follow-up implementation run)

Each entry is keyed to service + `file:line` and is a prose fix proposal. **No code is applied here.** Grouped by the harmonizing sweep that would close the most findings per unit of work.

### Sweep A — Route every CW/describe failure through `record_aws_error` (closes 9 NEW findings)

Reference implementation: `services/elasticache.py:259-269` and `services/_aws_errors.py:47-56` (classifies `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` -> `ctx.permission_issue`, else `ctx.warn`).

- **aurora** `services/adapters/aurora.py:95-121,124-149,152-173` — in each of `_get_cloudwatch_avg`/`_get_cloudwatch_sum`/`_get_cloudwatch_avg_max`, replace `except Exception: pass`/`return None` with `except Exception as e: record_aws_error(ctx, e, service="aurora", context=f"cloudwatch:GetMetricStatistics {metric} failed")`; thread `ctx` into the helper signatures.
- **opensearch** `services/opensearch.py:229,233` — route the two inner `except` paths through `record_aws_error(ctx, ...)`.
- **msk** `services/msk.py:91-95` — wrap the outer `ctx.warn` with `record_aws_error(ctx, str(e), "msk", "kafka")`; either drop the now-dead `list_clusters_v2` block (`:83-92`) or route its except through `record_aws_error`.
- **redshift** `services/redshift.py:122-123` — replace `except Exception: pass` with `except Exception as e: record_aws_error(ctx, e, service="redshift", context="redshift-serverless:ListWorkgroups failed")`.
- **redshift** `services/adapters/redshift.py:42` — change `required_clients()` to `return ("redshift", "redshift-serverless")`.
- **transfer** `services/transfer_svc.py:124-125` — replace outer `ctx.warn` with `record_aws_error(ctx, e, service="transfer", context="Could not analyze Transfer Family resources")`.
- **transfer** `services/transfer_svc.py:98-103` — narrow the inner bare `except Exception:` to classify via `record_aws_error(ctx, e, service="transfer", context=f"CW BytesUploaded/BytesDownloaded for {server_id}")`, keeping the per-rec note for the empty-datapoints case.
- **athena** `services/adapters/athena.py:64-66` — wrap the CW read failure in `record_aws_error(ctx, e, service="athena", context=f"CloudWatch ProcessedBytes for {workgroup} failed")` before setting `monthly_tb = 0`.
- **commitment_analysis** `services/adapters/commitment_analysis.py:648-672` — on `ClientError` AccessDenied/Unauthorized -> `ctx.permission_issue`, else `ctx.warn`, then fall back to `DEFAULT_COVERAGE_RATIO` (mirror `_route_ce_error`).
- **monitoring** `services/backup.py:111-122` and `services/route53.py:159-238` — route both sub-shims' exceptions through `record_aws_error` (AccessDenied/Unauthorized/OptInRequired -> `permission_issue`, else `ctx.warn`).
- (ec2/ebs outer-`except` items at `services/ec2.py:834-835,1224` and `services/ebs.py:689-690` are the same class; wrap in `try/except ClientError as ce:` routing `UnauthorizedOperation`/`AccessDenied` to `ctx.permission_issue`.)

### Sweep B — Add pagination (closes 3 NEW findings)

- **apprunner** `services/apprunner.py:100` — switch to `apprunner.get_paginator("list_services")` (or loop on `NextToken`).
- **athena** `services/athena.py:35` — add `get_paginator`/NextToken loop on `list_work_groups` (AWS MaxResults default 50).
- (network_cost peering/TGW pagination at `services/adapters/network_cost.py:403,410` is moot while advisory-only; lower priority.)

### Sweep C — Real counted-$ fixes (the two MEDIUM that affect dollars)

- **network** `services/vpc_endpoints.py:109-148` — thread a `covered_endpoint_ids: set` across `interface_endpoints_in_nonprod` and `duplicate_endpoints`; in the duplicate loop skip any `VpcEndpointId` already emitted by the nonprod loop (single-owner by `VpcEndpointId`). Add a regression test feeding a nonprod endpoint that is the 3rd of its service and assert its dollars appear in exactly one category.
- **bedrock** `services/adapters/bedrock.py:316` — replace the blended `0.000_003 $/token` with per-model input/output rates from the `…-input-tokens`/`…-output-tokens` SKUs (Claude 3 Sonnet input `$0.003/1K`, output `$0.015/1K`; Haiku ~12× lower input than the constant). The adapter already fetches `InputTokenCount`/`OutputTokenCount` separately.

### Sweep D — Pricing/dimension modelling gaps (closes 5 NEW findings; bounded counted-$ impact)

- **athena** `services/adapters/athena.py:71` — read the workgroup's `Configuration.ExecutionEngine`/`EngineVersion` and skip/recolor provisioned workgroups; gate the `0.75` factor on a format-detection signal (sampled `get_query_results`/table-format inspection) or demote to `$0` advisory when the current format is unknown.
- **sagemaker** `core/pricing_engine.py:1550-1556` — pin the SageMaker Hosting/Notebook usage-type per the AmazonSageMaker SKUs (mirror the Redshift/ElastiCache `_fetch_generic_instance_price` usage-type pin).
- **sagemaker** `services/adapters/sagemaker.py:142-167` — sum `InstanceType × InstanceCount` across all `ProductionVariants`, not just `[0]`.
- **lambda** `services/lambda_svc.py:103` — probe the PC metric with the per-config `Resource` dimension from `list_provisioned_concurrency_configs` before concluding no-data; fall back to advisory only if both dimensions are empty.
- **eks_cost** `core/pricing_engine.py:361` — bump `FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY` from `0.50` to `0.60` (other-region fallbacks re-derive via `self._fallback_multiplier`).

### Sweep E — Cosmetic / report-integrity (closes 4 NEW findings; zero counted-$ impact)

- **msk** `reporter_phase_b.py:2778` — add `("msk", "enhanced_checks"): "Audit Based"` to SOURCE_TYPE_MAP (under the s3 override); refresh reporter snapshots with `SNAPSHOT_UPDATE=1`.
- **network_cost** `services/adapters/network_cost.py:49,443` — rename `TGW_ATTACHMENT_COST_PER_GB` -> `TGW_ATTACHMENT_COST_PER_HR` (×730 -> ~`$36.50/mo`); split the display string into "$0.05/attachment-hr + $0.02/GB processing".
- **containers** `services/adapters/containers.py:20` — remove the dead `SPOT_SAVINGS_FACTOR = 0.70` constant + docstring (or wire a defensible Fargate->Spot delta if that lever is wanted).
- **bedrock** `services/adapters/bedrock.py:378-391` — add `"Counted": False` to KB rec dicts for explicitness; gate `total_recommendations` on `Counted!=False` if advisory-KB inflation is undesired.

### Not recommended (intentional documented limitations — leave as-is unless scope changes)

- api_gateway REST-only coverage — `services/api_gateway.py:69-77` (documented deferral; fabricating HTTP/WebSocket coverage would violate no-fabrication).
- dynamodb on-demand `EstimatedMonthlyCost` unit semantics — `services/dynamodb.py:411-419` (advisory-only; optional AuditBasis clarification).
- lambda ARM runtime allowlist — `services/lambda_svc.py:47-65` (derive from `Architectures` config OR accept periodic drift).
- bedrock Claude PT rates unverifiable — `services/adapters/bedrock.py:20-26` (source from account PT summary / Billing when present).
- commitment_analysis RI expiry / `ri_coverage_gaps` / EKS-on-Fargate — `services/adapters/commitment_analysis.py:405-619` (documented intentional; add only if those views are wanted).
- workspaces BYOL/Linux bundle table — `services/workspaces.py:309-321` (add a sub-table only if those WorkSpaces are common).
- athena `0.75` compression factor without format detection — covered in Sweep D if strict accuracy is wanted; otherwise leave as advisory-leaning.

---

## Methodology & validation notes

- **Adapter-FIRST via `codegraph_explore`** (the `.codegraph/` index exists at repo root) — every finding cites the CURRENT on-disk adapter `file:line`, re-read at audit time. The consolidation agent independently spot-checked the two MEDIUM counted-$ candidates (network VPC double-count at `services/vpc_endpoints.py:109-148`; bedrock token rate at `services/adapters/bedrock.py:316`) plus the commitment_analysis MEDIUM (at `services/adapters/commitment_analysis.py:648-672`) against current code — all three citations are line-accurate and retained (no downgrades).
- **A1–F5 sweep** (from `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`) applied to BOTH each adapter AND the draft findings. F1–F5 self-trap checks: no finding relies on a CoH camelCase/PascalCase misread (F1); no grouped-rendering false-positive (F3); no CO nested-options misread (F5); no advisory-projection mistaken for a leak (F4/B1-iii). The EKS `extended_support_pending` `AdvisoryEstimate`, the lambda commitment-demoted `AdvisoryEstimate`, the bedrock KB advisories, and the DynamoDB reserved-capacity demotion are all sanctioned projection/B1-iii shapes — flagged as documentation notes, not B1 leaks.
- **AWS-doc graceful chain:** where live AWS APIs were unreachable in the audit sandbox (no creds), each pricing claim cites the on-disk validated constant (with its own date+SKU citation in-code) + the public AWS doc URL + the lessons class. Pricing API probes returned empty for several services (Step Functions `AmazonStates` service code returns no rows; SageMaker bare filter is non-deterministic per the codebase's own warning); for those the AWS public pricing page is the authoritative source and the adapters' `rate_source: "documented ... rates (not in Pricing API)"` AuditBasis note is accurate.
- **Honesty invariant:** the expected outcome (MOST STALE-banner services yield CONFIRMED-already-fixed) is confirmed. Every STALE-banner service is CONFIRMED-dominant. No STALE-banner service exhibits a HIGH/CRITICAL NEW anomaly. The two NEW MEDIUM counted-$ findings (network VPC, bedrock token rate) sit on services whose counted dollars are a small fraction of the report total, on bounded conditions, and were independently verified — they are real but localized, not systemic.

## Out-of-scope observations (NOT fixed; for follow-up)

- **`services/adapters/CLAUDE.md` doc drift:** cloudfront "Fixed $25/rec" row, api_gateway "keyword-based" label, sagemaker Live-Pricing table, bedrock module-constant pricing, and workspaces dead `get_instance_monthly_price` claim are all stale vs current code. DOC-ONLY, out of audit fix scope.
- **Declaration gaps (cosmetic, no counted-$ impact):** `cloudfront` and `mediastore` adapters do not declare `requires_cloudwatch`/`reads_fast_mode` despite reading CW; `mediastore` helper has no explicit `ctx.fast_mode` early-skip. Behavior is correct via in-helper guards.
- **Repo-wide dead-constant grep:** the batch `BATCH_COMPUTE_FALLBACK_MONTHLY` constant name was not repo-wide grepped by C2 — if it lingers outside the batch files it is harmless dead weight; confirming its full removal is a follow-up.
- **AMI `region_snapshot_footprint_gib` / C11 share-cap** (C1 notepad): the function exists but C1 did not trace whether the AMI adapter's caller applies the billed-pool share-cap to the AMI counted total, or only to EBS/Snapshots. Possible C11 under-attribution question, not a counted-$ bug in the adapter itself.
