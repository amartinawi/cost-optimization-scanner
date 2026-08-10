# Post-Fix Verification Report — 2026-08-10

## Metadata

- **Date:** 2026-08-10
- **Scope:** Independent verification of the 6 fix tranches (T1-T6) plus the Pass-3 baseline plus a re-validation of the original 35 NEW findings.
- **HEAD verified:** `f2300a0` ("docs(audits): tranche 7 fix status - the under-count slice of the tail"), which sits atop the `8a7f7b0` target cited across the sweep findings. Each verifier re-read current code from disk and did NOT trust the fix-status narratives.
- **Methodology:** 8 parallel verifiers (one per notepad). Each verifier read the CURRENT adapter + test source for its scope, re-cited AWS docs / botocore shapes / live SKUs where rate-card correctness mattered, executed the targeted test files, and ran the regression snapshot gate (`tests/test_regression_snapshot.py` + `tests/test_reporter_snapshots.py`). No fix-status claim was trusted without an independent file:line read.
- **Read-only:** No `.py`, prompt, or test file was modified by any verifier. Only this report (and the 8 notepads it consolidates) was written.

## Executive Summary

Across the 8 verification notepads the verifiers inspected:

| Scope | Findings verified |
|---|---:|
| Pass-3 baseline (verify-pass3) | 6 |
| Tranche-1 (verify-tanche1) | 8 |
| Tranche-2 (verify-tanche2) | 10 (incl. the `93a3037` blocker) |
| Tranche-3 (verify-tranche3) | 12 |
| Tranche-4 (verify-tranche4) | 6 |
| Tranche-5 (verify-tranche5) | 8 (incl. NET-E age gate T5-1) |
| Tranche-6 (verify-tranche6, reconstructed) | 7 |
| Original-35 re-validation (verify-original35) | 35 |
| **Total findings verified** | **92** |

**Tranche verdict tally (Pass-3 + T1 + T2 + T3 + T4 + T5 + T6 = 57 findings):**

| Verdict | Count |
|---|---:|
| FIXED-CORRECT | 56 |
| FIXED-PARTIAL | 1 (WS-1 — exactly as declared in the sweep backlog; only `GENERALPURPOSE_4XLARGE` priced, remaining ~13 tiers + the G6/GR6 GPU family still $0) |
| NOT-FIXED | 0 |
| FIXED-REGRESSION | 0 |

**Aggregate regression count: 0.** The regression snapshot suite (`test_regression_snapshot.py` + `test_reporter_snapshots.py`) was reported green by every verifier that ran it, with 136 passing tests consistently. Targeted test totals reported per verifier: Pass-3 (subset), T1 (110 high-fix tests), T2 (146 + 11 isolated + 136 snapshot), T3 (commitment/dynamodb/lambda/workspaces/file_systems/ec2/reporter-sr1/sr2 all green), T4 (212 target tests), T5 (90 targeted + 136 snapshot), T6 (158 across 7 files + 11 finding-specific + 136 snapshot).

**Original-35 re-validation:** 4 closed, 26 open-in-backlog, 2 refuted, 2 downgraded, 1 partially/superseded. **Zero silent drops** — every OPEN item maps to an explicit backlog ID, a documented-intentional exclusion, or the cosmetic-tail bucket in the original sweep.

**VERDICT: FIXES_SOUND.** All 6 tranches plus the Pass-3 baseline landed correctly, every targeted test passes, the regression snapshot suite is green across every verifier, and zero regressions were introduced. The single highest-value OPEN item is the **athena 0.75 factor (#20 / ATH-1)** — escalated twice independently (verification HIGH, sweep CRITICAL) and untouched by tranches 1-6 — which is the recommended P1 next action.

## Per-Tranche Sections

### Pass-3 Baseline (6 findings — verify-pass3.md)

Paragraph: Six highest-stakes Pass-3 false-negative fixes (CRITICAL/HIGH severity). All six are present at HEAD, close the cited defect correctly, pin the exact defect path with a test, and introduce no regression in sibling branches. HEAD at read was `5cd1110`, a docs-merge atop the `8a7f7b0` target; all six files were re-read at the current HEAD.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| EC-1 elasticache Graviton phantom (CRITICAL) | FIXED-CORRECT | `services/adapters/elasticache.py:211` | `tests/test_elasticache_high_fixes.py::test_graviton_lever_advisory_when_target_unpriceable` | Sibling downsize branch keeps its priceable-target guard; no asymmetry re-introduced |
| RDS-1 CE headroom spent multiple times (HIGH) | FIXED-CORRECT | `services/commitment_coverage.py:156,249-271,554,520,239-247,720-727` | `tests/test_commitment_coverage.py::test_c6_headroom_ledger_shared_across_demotion_calls` (+ `test_c6_rds_and_aurora_share_one_ce_pool`, `test_c6_coh_and_local_gates_share_ledger`) | `take_headroom` returns True for `gross <= 0` (no manufactured-headroom regression); `_spent` ordering invariant is documented but not enforced — see residual risks |
| RDS-2 snapshot cap = entire region (HIGH) | FIXED-CORRECT | `services/rds_logic.py:122-248` (cap at L180-183, demote at L215-245) | `tests/test_rds_audit_fixes.py::test_reconcile_caps_when_actual_below_upper` + 3 sibling tests | Removed early-return guard is intentional (was the cited overstatement path), not a regression |
| RDS-4 engine-scoped RI gate dead (MEDIUM) | FIXED-CORRECT | `services/adapters/rds.py:172` (engine_of accepts both casings) | `tests/test_rds_audit_fixes.py::test_enhanced_engine_scoped_ri_does_not_demote_other_engine` | Aurora sibling (`aurora.py:742`) consistent; CoH gate stays engine-agnostic (errs toward over-demotion, safe) |
| S3-1 coldness gate empty-datapoints fails open (HIGH) | FIXED-CORRECT | `services/s3.py:895-1022` (`_assess_bucket_coldness`) | `tests/test_s3_adapter.py::test_zero_gets_without_corroboration_is_unknown` + 4 sibling tests | Warm verdict still fires on real GET activity; corroboration loop only runs on zero-GET path |
| S3-2 Standard->IA ignores 128 KB minimum (HIGH) | FIXED-CORRECT | `services/s3.py:827-845` + use site `:1343-1385` | `tests/test_s3_adapter.py::test_ia_object_size_gate` | Gate is scoped to the Standard->IA lever only; bucket-wide average approximation is a known residual over-counting edge (conservative direction), not a regression |

### Tranche-1 (8 findings — verify-tanche1.md)

Paragraph: Eight CRITICAL/HIGH fixes spanning sagemaker idle-endpoint, network_cost CE filter, bedrock CW id + PT rate table, opensearch surcharge/storage, apprunner memory, aurora Graviton. All 8 present, correct, tested (110 high-fix tests passed), 0 regressions. HEAD `5cd1110b`.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| SM-1 sagemaker idle-endpoint lever dead (CRITICAL) | FIXED-CORRECT | `services/adapters/sagemaker.py:59-125,174-313,256-277` | `tests/test_sagemaker_high_fixes.py::test_busy_endpoint_is_not_flagged_idle_with_variant_dimensions` + 6 siblings | Test fake uses real CW exact-match semantics — a future edit dropping VariantName would break the test, not silently pass |
| NC-1 network_cost CE filter matched nothing (CRITICAL) | FIXED-CORRECT | `services/adapters/network_cost.py:211-292,294-528` | `tests/test_network_cost_high_fixes.py::test_transfer_spend_query_is_not_service_constrained` + 2 siblings | Every transfer/TGW lever now `Counted=False`, `monthly_savings=0.0`; classifier strict, no catch-all |
| BR-1 bedrock CW id unstripped (CRITICAL) | FIXED-CORRECT | `services/adapters/bedrock.py:75-124` (`_derive_model_id` + `_rate_key`) | `tests/test_bedrock_idle_pt.py::test_invocation_query_uses_full_versioned_model_id_dimension` + Titan/rate-key tests | CW id and rate key now sourced from two separate functions — original strip-once-used-everywhere defect eliminated |
| BR-2 bedrock PT rate table unverified (CRITICAL) | FIXED-CORRECT | `services/adapters/bedrock.py:20-27,209-327` | `tests/test_bedrock_idle_pt.py::test_explicit_zero_datapoint_is_advisory_with_indicative_figure` + 2 siblings | PT rate table quarantined UNVERIFIED; every consumer is `Counted=False` (see residual risk: future-copy hazard) |
| OS-1 opensearch surcharge region filter (HIGH) | FIXED-CORRECT | `services/adapters/opensearch.py:182-294` (region gate L214-236) | `tests/test_opensearch_high_fixes.py::test_extended_support_ce_read_is_region_scoped` + 3 siblings | Fail-closed when `ctx.region` missing — `warn + return (0.0, {})`; mirrors commitment headroom read pattern |
| OS-2 opensearch storage not x data nodes (HIGH) | FIXED-CORRECT | `services/adapters/opensearch.py:387-411,423-453` | `tests/test_opensearch_high_fixes.py::test_storage_delta_multiplies_data_node_count` + 2 siblings | Shim carries `InstanceCount` from `ClusterConfig.InstanceCount` (verified via end-to-end test) |
| AR-1 apprunner memory "2048" as 2048 GB (HIGH) | FIXED-CORRECT | `services/adapters/apprunner.py:57-82` | `tests/test_apprunner_high_fixes.py::test_bare_mb_memory_priced_as_mb_not_gb` + 4 siblings | Unknown/non-finite units raise and abstain to $0 advisory |
| AUR-C aurora Graviton map hardwired to Graviton2 (HIGH) | FIXED-CORRECT | `services/aurora_logic.py:30-101` + `services/adapters/aurora.py:317-374` + `core/pricing_engine.py:652-734` | `tests/test_aurora_high_fixes.py::test_graviton_target_matches_the_source_generation` + 3 siblings | See Tranche-6 self-caught strict-namespace blocker — verified isolated (T6) |

### Tranche-2 (10 findings — verify-tanche2.md)

Paragraph: Ten HIGH fixes plus the self-caught `93a3037` blocker. 10/10 present and correct, 0 regressions. The blocker — a demoted enhanced EC2 rec was being DELETED instead of rendered — was caught by the verifier and fixed mid-tranche; it is the strongest signal that the fix process was honest. HEAD `8a7f7b0` (blocker commit `93a3037` reachable).

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| LS-1 lightsail region-scales flat rates (HIGH) | FIXED-CORRECT | `services/adapters/lightsail.py:58-143` | `tests/test_lightsail_high_fixes.py::test_scan_h4_static_ip_is_flat_not_region_scaled` + bundle test | `pricing_multiplier` still plumbed through shim but ignored by adapter on every counted path |
| CF-1 cloudfront advisory never Counted=False (HIGH) | FIXED-CORRECT | `services/adapters/cloudfront.py:62-87` | `tests/test_cloudfront_high_fixes.py::test_advisory_recs_carry_counted_false_and_zero_string` + adapter test | Dedicated-IP SSL carve-out (`_COUNTED_CATEGORY`) survives the sweep; dead CF-2/CF-5 percentage strings overwritten |
| EC2-2 ASG CO recs bypass commitment gate (HIGH) | FIXED-CORRECT | `services/adapters/ec2.py:348-357` (`_co_type` accessor) | `tests/test_commitment_coverage.py::test_ec2_adapter_demotes_sp_covered_asg_co_recs` | Accessor reads `currentInstanceType` then falls back to `current_config.instanceType` (ASG-normalized) |
| EC2-3 dedicated-tenancy flat 30% (HIGH) | FIXED-CORRECT | `services/ec2.py:722-754` | `tests/test_ec2_audit_fixes.py::test_dedicated_tenancy_is_zero_advisory` | The 0.30 constant at `services/ec2.py:34` is dead for the counted path (cosmetic; flagged in residual risks) |
| EC2-3 blocker (commit `93a3037`) — demoted rec DELETED not rendered | FIXED-CORRECT | `services/adapters/ec2.py:259-260,281-289,382` | `tests/test_ec2_audit_fixes.py::test_dedicated_advisory_survives_the_adapter` (adapter-level — the gap that let the blocker ship) | `enhanced_recs` now partitions on `Counted` exactly like `advanced_recs`; advisory survives rendering with its CW evidence + "Metric Backed" badge |
| EC2-4 deny ASG:Describe* raises counted (HIGH) | FIXED-CORRECT | `services/adapters/ec2.py:109-143,234,291-311` | `tests/test_ec2_high_fixes.py::test_asg_enumeration_failure_demotes_heuristics` | Denied `autoscaling:DescribeAutoScalingGroups` can only LOWER the counted total, never raise it |
| EKS-1 idle-cluster corroboration wrong tag (HIGH) | FIXED-CORRECT | `services/adapters/eks.py:486-538` (`_count_owned_ec2_nodes` probes 4 tag variants incl. Karpenter's `eks:eks-cluster-name` added by `93a3037`) | `tests/test_eks_high_fixes.py::test_eks1_capacity_found_via_aws_cluster_name_tag_demotes_idle` | Prompt cited `:503`; actual probe spans L499-538 — line drift only, fix is correct |
| EKS-2 failed NG/Fargate enum -> counted delete (HIGH) | FIXED-CORRECT | `services/adapters/eks.py:540-573,700-723,162` | `tests/test_eks_high_fixes.py::test_eks2_failed_nodegroup_enumeration_never_counts_idle` | `None == 0` is False so failed enumeration never marks cluster candidate-idle; layered defense — `_build_idle_rec` also requires exact `owned_node_count == 0` |
| CN-1 containers rightsizing CI-gated (HIGH) | FIXED-CORRECT | `services/containers.py:416-436` (no CI gate anywhere) | `tests/test_containers_high_fixes.py::test_no_container_insights_gate` | Adapter has zero CI references; only descriptive text remains (cosmetic) |
| MON-1/MON-2 monitoring stale-metric inverted (HIGH) | FIXED-CORRECT | `services/monitoring.py:331-393` | `tests/test_monitoring_high_fixes.py::test_no_get_metric_data_probe_remains` + 3 siblings | Inverted lever DELETED; replaced by proportional-spend advisory gated on `CW_CUSTOM_METRIC_SPEND_ADVISORY_FLOOR` ($10/mo — blocker fix `93a3037`); residual L85 `IncomingBytes` GetMetricData is the legitimate log-group probe (different purpose) |

### Tranche-3 (12 findings — verify-tranche3.md)

Paragraph: Twelve findings covering commitment, dynamodb, lambda, workspaces, file_systems, and the reporter Counted gate. 11 of 12 fully fixed/correct/gated; WS-1 is PARTIAL exactly as declared in the sweep backlog. The verifier also self-caught a non-blocker: the lambda renderer (`LAM-2`) was claimed as missing a `PHASE_B_HANDLERS` entry, but it is dispatched via an explicit `elif service_key == "lambda"` branch upstream of the dispatch table — non-issue. Botocore shape verifications were performed against the live `boto3.session.Session()` service model. HEAD `5cd1110`.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| H3 commitment `_check_expiring` wrong CE key | FIXED-CORRECT | `services/adapters/commitment_analysis.py:471-539` | `tests/test_commitment_utilization.py::test_expiring_sp_emitted_from_describe_savings_plans` + no-client test | botocore-verified: `savingsplans#SavingsPlan` has `end` (not `EndDateTime`); recs born `Counted=False` |
| H4 RI waste reads nonexistent `TotalAmortizedCost` | FIXED-CORRECT | `services/adapters/commitment_analysis.py:391-416` | `tests/test_commitment_fargate_sp.py::test_ri_waste_uses_measured_unused_cost_over_amortized_fee` + 2 siblings | botocore-verified: `ReservationAggregates` has `RICostForUnusedHours` + `TotalAmortizedFee`, NOT `TotalAmortizedCost`; `waste <= 1.0` continue guards counted-$0 |
| H5 fixtures encode nonexistent fields | FIXED-CORRECT | `tests/fixtures/recorded_aws_responses/commitment_analysis/*.json` (both fixtures rewritten) | snapshot + commitment suites green | Live botocore sub-shape verified: `SavingsPlansUtilization`, `SavingsPlansAmortizedCommitment`, `SavingsPlansSavings` all match fixture exactly |
| DDB-A DynamoDB CoH recs DISCARDED | FIXED-CORRECT | `services/adapters/dynamodb.py:170-235` | `tests/test_dynamodb_high_fixes.py::test_scan_coh_rec_survives_on_a_table_with_a_local_advisory_row` + 4 siblings | Local levers on covered tables demoted to advisory (CoH wins); fail-closed branch surfaces unresolvable-ARN rec as advisory with `PricingWarning` |
| DDB-C guard | FIXED-CORRECT | `services/adapters/dynamodb.py:9,181` (`is_renderable_coh_rec` import + invocation) | `tests/test_dynamodb_high_fixes.py::test_scan_drops_non_renderable_coh_recs` | Co-located with DDB-A fix |
| LAM-1 Lambda PC savings escape Compute-SP gate | FIXED-CORRECT | `services/adapters/lambda_svc.py:294-327` | `tests/test_lambda_audit_fixes.py::test_pc_savings_demoted_under_an_active_compute_sp` + non-Compute-SP test | Both `cost_hub_recs`/`co_recs` AND `enhanced_recs` demoted under Compute SP |
| LAM-2 counted PC dollar unrenderable | FIXED-CORRECT | `services/adapters/lambda_svc.py:241-247` + `reporter_phase_b.py:1765-1816` (Counted branch at L1808) | `tests/test_lambda_audit_fixes.py::test_pc_counted_string_matches_the_counted_dollar` + 2 reporter-sr2 tests | Verifier confirmed the "no PHASE_B_HANDLERS entry" blocker is a non-issue — dispatched via explicit `elif service_key == "lambda"` at `reporter_phase_b.py:1453` |
| WS-3 missing `ComputeTypeName` defaults to STANDARD | FIXED-CORRECT | `services/adapters/workspaces.py:22-27,79-80,139-143` | `tests/test_workspaces_high_fixes.py::test_ws3_missing_compute_type_abstains` + shim test | Empty-string compute type abstains with $0 advisory (was fabricated $35) |
| WS-1 14 of 23 ComputeTypes unpriced | FIXED-PARTIAL (per finding) | `services/workspaces.py:42-61` (`GENERALPURPOSE_4XLARGE: 295.0` added; comment L58-61 documents remainder) | `tests/test_workspaces_high_fixes.py::test_ws1_general_purpose_4xlarge_is_priced` | G6/GR6/G6F GPU family + the other GENERALPURPOSE sizes still $0 (now correctly abstained via WS-3 path, not fabricated). Backlog |
| FS-1 idle EFS all-Standard-rate | FIXED-CORRECT | `services/efs_fsx.py:316-355` | `tests/test_file_systems_adapter.py::test_idle_delete_prices_each_storage_class` + 2 siblings | botocore-verified: `ValueInStandard`/`ValueInIA`/`ValueInArchive` are the documented members |
| FS-6 FSx SSD->HDD not executable + no throughput gate | FIXED-CORRECT | `services/efs_fsx.py:556-599` | `tests/test_file_systems_adapter.py::test_ssd_to_hdd_is_advisory` | Counted-eligible branch FORCIBLY demoted (capacity gate passes -> rec appended to `advisory` with PotentialMonthlySavings) |
| Reporter Counted `_render_ec2_advanced_checks` ignores Counted | FIXED-CORRECT | `reporter_phase_b.py:2062-2112` + helper at `:82-127` | `tests/test_ec2_high_fixes.py::test_advanced_checks_renderer_excludes_demoted_recs` + sum-only test | Shared `_grouped_text_savings_line` helper skips `Counted is False` recs; sums numeric or parsed-from-free-text |

### Tranche-4 (6 findings — verify-tranche4.md)

Paragraph: Six findings including the rank-7 CoH type routing + storage suppression guard + type_map hygiene. 6/6 FIXED-CORRECT, 0 regressions. The verifier re-cited AWS docs for the TR-2 metric names and confirmed they match exactly. HEAD `f2300a0`.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| TR-2 Transfer CW metric names (HIGH) | FIXED-CORRECT | `services/transfer_svc.py:128-172` + `services/adapters/transfer.py:111` | `tests/test_transfer_high_fixes.py::test_data_transfer_note_uses_real_metric_names` | AWS-doc re-cited: `AWS/Transfer` publishes `BytesIn`/`BytesOut`; datapoint-presence gate at L155 + adapter double-gate on `IdleEvidence is True` means denied CW cannot mint the counted dollar |
| GL-1 Glue get_dev_endpoints unpaginated ($$) | FIXED-CORRECT | `services/glue.py:56-77` (paginator preferred, manual NextToken fallback L68-75) | `tests/test_glue_high_fixes.py::test_dev_endpoints_paginated` + `test_dev_endpoints_next_token_fallback` | Both code paths terminate when NextToken is missing/empty |
| NET-B Zero-listener rec :.0f-truncated ($$) | FIXED-CORRECT | `services/load_balancer.py:244-258` | `tests/test_network_lb_high_fixes.py::test_idle_listener_lb_still_counts` | String + numeric derive from one `lb_monthly` value; PotentialMonthlySavings preserves figure under age-demote |
| NET-E ALB/NLB zero registered targets (HIGH, new counted lever) | FIXED-CORRECT | `services/load_balancer.py:25-110,260-296,401-443` | 9 tests in `test_network_lb_high_fixes.py` | Tri-state helper fails CLOSED at every ambiguity; classic-ELB missing-key abstains; age gate `_MIN_IDLE_AGE_DAYS=7`; fast-mode skips describe_target_groups (call-count guard) |
| rank-7 CoH type routing — 4 real types (HIGH) | FIXED-CORRECT | `core/scan_orchestrator.py:139-140,174-175` | `tests/test_orchestrator.py::test_coh_storage_types_route_to_rds` + `test_coh_reserved_capacity_types_route_to_commitment_analysis` + bucket-existence test | Storage recs route to "rds"; reserved-capacity types route to "commitment_analysis"; both buckets in `_HUB_SERVICES` |
| rank-7 blocker (`b38ae99`) — storage rec suppression guard | FIXED-CORRECT | `services/_coh_dedup.py:43-56` + `services/adapters/rds.py:182-194` + `services/adapters/aurora.py:677-688` + `services/rds_logic.py:289-292` + `services/commitment_coverage.py:336-354,532-555` | `tests/test_coh_storage_routing.py` (8 tests) | All THREE suppression paths skip storage recs (storage recs carry no instance class -> never demoted by `demote_coh_by_commitment`) |
| rank-7 type_map hygiene (`9696799`) | FIXED-CORRECT | `core/scan_orchestrator.py:112-176` | `tests/test_orchestrator.py::test_coh_type_map_keys_are_real_resource_types` + `test_coh_type_map_has_no_dead_cluster_keys` | Test reads live botocore `shapes.ResourceType.enum` (case-sensitive) — a typo would fail the test |

### Tranche-5 (8 findings — verify-tranche5.md)

Paragraph: Eight findings including 5 new counted levers (CF-4, AG-3, TR-1, MSK-1, NET-E age gate) plus 3 defect fixes. The verifier specifically called out that both render-side blockers claimed-fixed in the tranche narrative were ACTUALLY fixed in HEAD code (verified by reading the live branch, not the commit message). 8/8 PASS, 0 regressions. HEAD `f2300a0`.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| CF-4 CloudFront dedicated-IP custom SSL (counted lever) | FIXED-CORRECT | `services/cloudfront.py:37-101,128,156-166` + `reporter_phase_a.py:131-140` (render blocker) | `tests/test_cloudfront_dedicated_ip_ssl.py::test_dedicated_ip_distribution_is_counted` + 3 siblings | Per-cert grouping via ACM/IAM/Certificate keying (unknown collapses to ONE `__unidentified__` group); FALLBACK $600/mo verified against live SKU `QUEZ7XDZJJXURBU7` |
| AG-3 API Gateway REST stage caches idle (counted lever) | FIXED-CORRECT | `services/api_gateway.py:107-192` + `reporter_phase_a.py:225-235` (render blocker) | `tests/test_api_gateway_stage_cache.py::test_metric_uses_the_stage_scoped_dimension_pair` + 4 siblings | Standard `ApiName`+`Stage` dimension pair; demote-on-unknown; FALLBACK cache-hourly table keys are CacheClusterSize enum verbatim |
| TR-1 idle ONLINE Transfer servers (counted lever) | FIXED-CORRECT | `services/transfer_svc.py:128,155` + `services/adapters/transfer.py:106-142` | `tests/test_transfer_idle_servers.py::test_empty_series_means_nobody_connected` + `test_a_series_of_zeros_means_the_server_is_in_use` (polarity guard) | Polarity CORRECT: counts datapoints (PRESENCE) not sum; recommends stop (reversible), not delete |
| TR-3 Protocols from DescribeServer not ListServers | FIXED-CORRECT | `services/transfer_svc.py:37-58,85` | `tests/test_transfer_idle_servers.py::test_offline_server_costs_no_describe_call` | Verified against botocore: `ListedServer` has no `Protocols` member |
| MSK-1 idle MSK clusters (counted lever) | FIXED-CORRECT | `services/msk.py:34-87` + `services/adapters/msk.py:154-180` | `tests/test_msk_idle_clusters.py::test_all_brokers_reporting_zero_is_idle` + 4 siblings | Per-broker dimension (`Cluster Name` + `Broker ID`); polarity OPPOSITE of Transfer (correct); abstains on empty series |
| MSK-5 NumberOfBrokerNodes from API not defaulted | FIXED-CORRECT | `services/msk.py:110-115` | `tests/test_msk_idle_clusters.py::test_missing_broker_count_abstains_instead_of_assuming_three` + broker-count-drives-price test | Comment documents the prior fabricated-3x-multiplier defect |
| MON-3 CloudWatch Logs ingestion class (advisory lever) | FIXED-CORRECT | `services/monitoring.py:226,224-225,54-108,50,175-187,44-45` | `tests/test_monitoring_log_ingestion.py::test_ingestion_is_priced_at_the_standard_minus_ia_delta` + 3 siblings | Advisory only (Counted=False); single batched GetMetricData; largest-first + 500-query cap + truncation WARN |
| NET-E age gate T5-1 (LB < 7 days old) | FIXED-CORRECT | `services/load_balancer.py:70,73-84,87-110,245-259,273-296,422-423,260` | `tests/test_network_lb_high_fixes.py::test_young_lb_with_no_listeners_is_advisory_not_counted` + 4 siblings | BOTH idle branches gated (zero-listener + zero-target) + classic ELB; young AND unreadable -> $0 advisory with PotentialMonthlySavings; fast-mode skips zero-target lever |

### Tranche-6 (7 findings, reconstructed — verify-tranche6.md)

Paragraph: Seven findings. The original notepad write failed and was reconstructed from the agent return; the verdict confidence is therefore slightly thinner than the others but the per-finding citations are intact. Includes TWO self-caught blockers: the AUR-C strict-namespace cache poisoning hazard and the AUR-G grouped-render figure-masking hazard. 7/7 FIXED-CORRECT, 0 regressions. HEAD `f2300a0`.

| Finding | Verdict | file:line verified | Test | Regression note |
|---|---|---|---|---|
| NET-A/D/F duplicate-endpoint counted dollar | FIXED-CORRECT | `services/vpc_endpoints.py:99-152` | finding-specific tests (11 green across the 7 files) | Shared covered-id set; single owner per `VpcEndpointId`; threshold = any endpoint past the first; lever is $0 advisory carrying its ceiling. The previously-untested counted duplicate-endpoint dollar now has a test |
| GL-4 Glue no-DPU-default | FIXED-CORRECT | `services/adapters/glue.py:48-151` | finding-specific | No-DPU-default abstain (same class as WS-3/MSK-5); unrecognized WorkerType abstains. (GL-3 deliberately not changed — READY dev endpoints counted with no idle evidence, per fix-status narrative) |
| DMS-4 DMS unpriced recs demoted properly | FIXED-CORRECT | `services/adapters/dms.py:300-326` | finding-specific | No Counted flag, no $0 numeric disagreement, no B3 prose |
| DMS-1 DMS config-only Multi-AZ list decoupled | FIXED-CORRECT | `services/dms.py:111-119` | finding-specific | Config-only Multi-AZ list decoupled from the CPU `continue` — a dev Multi-AZ instance at normal utilization now produces its ~$204/mo per-AZ delta rec |
| NET-C dev/test NAT advisory-only | FIXED-CORRECT | `services/nat_gateway.py:153-193` | finding-specific | Advisory-only dev/test NAT (no longer full-base-counted off Environment tag alone) |
| AUR-C strict-namespace (self-caught blocker) | FIXED-CORRECT | `core/pricing_engine.py:704-737` + `services/adapters/aurora.py:328-333` | namespace-isolation behavioral test (warms cache, asserts strict returns 0.0) | Separate cache keys `rds_instance` vs `rds_instance_real`; both aurora legs pass `allow_fallback=False`. The lenient RDS-adapter lookup cannot poison the strict Aurora Graviton probe. Behavioural test (not literal-key-string assertion) — captured as lesson C12 in `_LIVE_AUDIT_LESSONS.md` |
| AUR-G grouped-render figure-masking (self-caught blocker) | FIXED-CORRECT | `reporter_phase_b.py:112-127` (`_grouped_text_savings_line`) + `:59-79` (`_advisory_line`) | grouped-render coverage | Per-rec path sums the masked figure across the group; both AdvisoryEstimate and PotentialMonthlySavings covered |

## Original-35 Re-validation Section

Source: `RUN-FINDINGS-2026-08-09.md`. Re-validation date 2026-08-10, HEAD `f2300a0`. Method: spot-check file:line for every FIXED claim; grep for OPEN backlog items; cross-reference against the sweep's tranches 1-7. Read-only.

### Top tally

| Status | Count | Notes |
|---|---:|---|
| FIXED | 4 | #21 (NET-A tranche 6), #16 (transfer inner CW, tranche 4/5), #33 (bedrock KB Counted, tranche 1 BR-4), #34 (bedrock CW ModelId, tranche 1 BR-1) |
| OPEN — in backlog | 26 | Most E1-class hygiene items + the modeling/pricing gaps the sweeps surfaced but tranches 6/7 did not close |
| OPEN — SILENT DROP (FLAG) | **0** | All OPEN items map to either an explicit backlog ID (CN-2, AUR-H, RS-C, MSK-2.., etc.) or to a declared-intentional/cosmetic bucket in the original report |
| REFUTED (confirm still refuted) | 2 | #3 (EKS fallback $0.50), #6 (apprunner pagination) |
| DOWNGRADED (still open at lower sev) | 2 | #8 (dynamodb reads_fast_mode -> LOW), #11 (redshift required_clients -> LOW/cosmetic) |
| PARTIALLY ADDRESSED / SUPERSEDED | 3 | #26 (RI expiry — H3 fixed SP path; RI intentionally uncovered), #32 (Claude PT rates — table now annotated "unverified" via BR-2; still hardcoded), #34 (bedrock CW dim — original mechanism was backwards but underlying mismatch fixed via BR-1) |

Net: 35 findings = 4 closed + 26 open-in-backlog + 2 refuted + 2 downgraded + 1 partial (athena #20 still fully open at HIGH). Sum = 35.

### Per-finding table

Legend: FIXED = closed on a named tranche; OPEN-B = OPEN and in declared backlog; REF = refuted; DG = downgraded; PART = partially/superseded.

| # | service | original sev | current status | evidence |
|---|---|---|---|---|
| 1 | ec2 | LOW | OPEN-B | `services/ec2.py:850-851` (enhanced) & `:1239-1240` (advanced) still `except Exception as e: ctx.warn(...)`. Backlog: EC2-5..EC2-8 tail |
| 2 | ebs | LOW | OPEN-B | `services/ebs.py:689-690` still `ctx.warn` generic. Backlog: EB-2 |
| 3 | eks_cost | LOW | REF (confirm) | `core/pricing_engine.py:381` `FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY = 0.50`. Live SKU: $0.50 IS the surcharge; $0.60 is total — code correct, original fix would overstate ~$73/mo/cluster |
| 4 | eks_cost | LOW | OPEN-B (cosmetic) | `services/adapters/eks.py:367` emits `AdvisoryEstimate` on `extended_support_pending`. Cosmetic backlog |
| 5 | containers | LOW | OPEN-B (cosmetic) | `services/adapters/containers.py:20` `SPOT_SAVINGS_FACTOR: float = 0.70` declared, 0 usages in containers.py. Cosmetic-tail backlog. NOTE: sweep CN-1 is a DIFFERENT finding (tranche-2 fixed) |
| 6 | apprunner | LOW | REF (confirm) | `services/apprunner.py:100` still single `list_services()`; AWS docs confirm single-response contract; botocore ships no apprunner paginator. Refutation holds |
| 7 | aurora | MEDIUM | OPEN-B | `services/adapters/aurora.py:120,148,172` three CW helpers still `except Exception: pass` / `return None`. Backlog: AUR-H |
| 8 | dynamodb | MEDIUM->LOW | OPEN-B (DG) | `services/adapters/dynamodb.py:57-69` no `reads_fast_mode`. Downgrade to LOW stands (scan-performance) |
| 9 | dynamodb | LOW | OPEN-B (cosmetic) | `services/dynamodb.py:411-419` formula flows only into demoted `table_analysis` advisory |
| 10 | redshift | MEDIUM->LOW | OPEN-B (DG) | `services/redshift.py:122-123` bare `except Exception: pass` on `redshift-serverless` block. Backlog: RS-C |
| 11 | redshift | MEDIUM->LOW | OPEN-B (DG) | `services/adapters/redshift.py:42` still `return ("redshift",)`. Original mechanism wrong (`ctx.client()` is lazy caching factory). Cosmetic backlog |
| 12 | opensearch | LOW | OPEN-B | `services/opensearch.py:248,252` inner per-domain still `logger.warning`. Backlog: OS-10 |
| 13 | msk | LOW | OPEN-B | `services/msk.py:188-189` inner `list_clusters_v2` block bare `except Exception: pass`. Backlog: MSK-4. NOTE: MSK-1 (new idle-cluster lever) WAS added by tranche 5 — but the E1 hygiene gap remains |
| 14 | msk | LOW | OPEN-B | `reporter_phase_b.py:2803-2834` SOURCE_TYPE_MAP has no `("msk", "enhanced_checks")` entry. Cosmetic backlog |
| 15 | transfer | MEDIUM | OPEN-B | `services/transfer_svc.py:218-219` outer `ctx.warn(...)` still generic. Backlog: E1 cluster |
| 16 | transfer | MEDIUM | **FIXED** | `services/transfer_svc.py:183-192` inner CW now wraps via `record_aws_error(ctx, cw_err, service="transfer", context=...)`. Metric names corrected by tranche 4 TR-2 |
| 17 | lambda | MEDIUM | OPEN-B | `services/lambda_svc.py:134` `_read_pc_max_utilization` still `Dimensions=[{"Name":"FunctionName","Value":function_name}]` only. LAM-3 (tranche 7) added a separate `_read_invocation_count` discriminator but did NOT close the original dimension issue. Backlog: LAM-4 |
| 18 | lambda | LOW | OPEN-B | `services/lambda_svc.py:47-65` `ARM_SUPPORTED_RUNTIMES` tuple unchanged. Advisory-only; periodic drift accepted |
| 19 | athena | MEDIUM | OPEN-B | `services/adapters/athena.py:64-65` CW read failure still `logger.warning`, `monthly_tb = 0`, not routed through `record_aws_error`. Backlog: ATH-tail |
| **20** | **athena** | **MEDIUM->HIGH** | **OPEN-B (highest-value OPEN item)** | `services/adapters/athena.py:71` `rec_savings = monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75` UNCHANGED. No `ExecutionEngine`/`EngineVersion`/format detection anywhere in `services/adapters/athena.py` or `services/athena.py`. ESCALATED to HIGH by verification (sweep ATH-1 rated CRITICAL). NOT closed by tranches 6 or 7 — sits in MEDIUM/LOW tail. **The single biggest OPEN item.** |
| 21 | network | MEDIUM | **FIXED** | `services/vpc_endpoints.py:96-153` `nonprod_endpoint_ids: set` shared; single-owner-by-VpcEndpointId invariant enforced. Tranche 6 NET-A, commit `1b04068` |
| 22 | network_cost | LOW | OPEN-B (cosmetic) | `services/adapters/network_cost.py:49` `TGW_ATTACHMENT_COST_PER_GB = 0.05` still declared; all recs `monthly_savings=0.0` advisory. Cosmetic tail |
| 23 | network_cost | LOW | OPEN-B | `services/adapters/network_cost.py:428,435` single `describe_vpc_peering_connections`/`describe_transit_gateways` calls. Moot (advisory-only) |
| 24 | api_gateway | LOW | OPEN-B (intentional) | `services/api_gateway.py:7-18,199,220` REST-only scope documented as intentional; `apigatewayv2` deferred. NOT a defect |
| 25 | commitment_analysis | MEDIUM | OPEN-B | `services/adapters/commitment_analysis.py:721-722` `_account_coverage_ratio` still `except Exception: return DEFAULT_COVERAGE_RATIO`, no `ctx` record. Backlog: M-tail (E1) |
| 26 | commitment_analysis | LOW | **PART/SUPERSEDED** | H3 (tranche 3) rewrote `_check_expiring` for SP expiry; RI expiry remains intentionally unimplemented (documented). Mark PART — RI leg intentionally uncovered, not silently dropped |
| 27 | commitment_analysis | LOW | OPEN-B (intentional) | `services/adapters/commitment_analysis.py:437` `_check_ri_coverage` still returns `([], None)`. Documented intentional |
| 28 | commitment_analysis | LOW | OPEN-B (intentional) | `services/adapters/commitment_analysis.py:627,640` CE filter still `SERVICE = "Amazon Elastic Container Service"` only; EKS-on-Fargate exclusion documented |
| 29 | sagemaker | LOW | OPEN-B | `core/pricing_engine.py:1414` `_fetch_sagemaker_instance_price` still bare TERM_MATCH filter, MaxResults=1. Backlog: SM-tail (C2) |
| 30 | sagemaker | LOW | OPEN-B | `services/adapters/sagemaker.py:524-527` idle-endpoint pricing still `variants[0].get("InstanceType")` (one variant, one instance). SM-3 (tranche 1) fixed the consolidation path but NOT the idle-endpoint path. Backlog: SM-tail |
| 31 | bedrock | MEDIUM | OPEN-B | `services/adapters/bedrock.py:359` `od_monthly_estimate = (input_tokens + output_tokens) * 0.000_003` UNCHANGED, feeds counted `monthly_savings` at `:364`. BR-4 (tranche 1) made breakeven advisory but did NOT split per-model rates. Backlog: BR-5 |
| 32 | bedrock | LOW | **PART** | `services/adapters/bedrock.py:23-29` `PT_HOURLY_PRICE` still hardcoded. BR-2 (tranche 1) added "unverified" annotation and made proven-idle PTs advisory, but rates remain hardcoded. Original ask (source from Billing) NOT implemented. Mark PART — annotation lands, mechanism unchanged |
| 33 | bedrock | LOW | **FIXED** | `services/adapters/bedrock.py:445` KB rec dict now explicitly `"Counted": False`. Tranche 1 BR-4 |
| 34 | bedrock | LOW | **FIXED** (mechanism inverted) | `services/adapters/bedrock.py:106-124` `_derive_model_id` returns UNSTRIPPED id; `_rate_key()` strips separately. Tranche 1 BR-1. NOTE: original #34 finding's mechanism was BACKWARDS — real bug was OVER-stripping causing empty reads, not base-id aggregation. The fix is correct; original prose mis-described the direction |
| 35 | monitoring | MEDIUM | OPEN-B | `services/backup.py:111-122` three `except` paths still `ctx.warn(...)`; `services/route53.py:170,189,248-249` same. Backlog: MON-tail |

### Single highest-value OPEN item

**#20 athena 0.75 factor (`services/adapters/athena.py:71`, backlog ATH-1).** The factor still applies unconditionally to every counted athena dollar with zero format/engine evidence. It was escalated to HIGH by the verification notepad and the sweep independently rated it CRITICAL — yet tranches 6 and 7 (which targeted wrong-dollar and under-count slices respectively) did NOT touch line 71. This is the highest-value OPEN item in the entire audit and the recommended P1 next action.

### Zero silent drops confirmed

Every OPEN item maps to either (a) an explicit backlog ID in the sweep doc's "Remaining backlog" section (AUR-H, RS-C, OS-10, MSK-4, EC2-5..8, LAM-4, ATH-tail, BR-5, MON-tail, etc.), (b) a documented-intentional exclusion in the original report (#24, #27, #28), or (c) the original report's own "Sweep E — Cosmetic" bucket (#4, #5, #9, #14, #22, #23). The declared backlog tail plus the documented-intentional list covers all 26 OPEN items. No silent drops.

## Residual Risks Across All Tranches (Aggregate)

Grouped by severity. These are the "new risks" / "non-blocking observations" each verifier noted — things to watch, not blockers.

### MEDIUM (worth a defense-in-depth follow-up)

1. **RDS-1 `_spent` ledger depends on object identity (Pass-3).** The scan-scoped ledger works only if every adapter gate receives the SAME `CommitmentCoverage` instance. The `fetch_commitment_coverage` `dataclasses.replace()` is safe TODAY only because it runs before any adapter spends. The code documents this as an "ORDERING INVARIANT" (`commitment_coverage.py:875-881`) but does not enforce it — a future `replace(coverage, ...)` introduced anywhere after the scan starts would silently reset `_spent` to `{}` and reintroduce the double-spend with no failing test. Suggested hardening: freeze `_spent` into a separately-held mutable object, or assert identity at gate entrypoints.

2. **BR-2 bedrock PT rate table still in code (T1).** Correctly quarantined UNVERIFIED; every consumer is `Counted=False`. The structural risk: a future adapter author who copies the table into a new counted path would re-mint the BR-2 fabrication. A `# DO NOT USE FOR COUNTED $` sentinel or a CI grep guard would harden this.

3. **Athena 0.75 factor #20 (original-35).** Highest-value OPEN item — see above.

### LOW (cosmetic / fragility)

4. **S3-2 bucket-wide average approximation (Pass-3).** `_ia_object_size_ok` uses `SizeBytes / NumberOfObjects` across ALL storage classes because CloudWatch publishes no per-class object count. A bucket whose Standard bytes are small objects but whose Glacier bytes are large archives will pass the 128 KiB gate and count a Standard->IA delta that is partially unrealizable. Acknowledged in the docstring; conservative (over-counting) direction.

5. **RDS-2 share-cap relies on `backup_footprint` being supplied (Pass-3).** When `backup_footprint` is `None`, the rec is demoted (fail-closed — correct). But the demotion now fires whenever the caller forgets to thread it through `resolve_rds_findings`, even on accounts where CE backup actuals ARE available. The current rds adapter call site threads it, but the contract is sharper than before.

6. **EC2-3 0.30 constant left dead (T2).** `_EC2_POTENTIAL_SAVINGS["Dedicated Hosts"] = 0.30` at `services/ec2.py:34` is dead for the counted path (the dedicated branch ignores `dedicated_savings` for counting, only emits it as `AdvisoryEstimate`). Cosmetic — could mislead a future maintainer into thinking the 0.30 factor is still used.

7. **GL-1 manual NextToken fallback has no max-page cap (T4).** AWS API pagination is bounded by service quotas in practice, but a defensive `max_pages` would be cheap insurance. Not in tranche scope.

8. **Tranche-3 fixture `_shape_note` comments are not machine-checked (T3).** They document the previous wrong shape AND the real shape — excellent provenance — but a future regression could re-derive a wrong shape if a maintainer copies the `_shape_note` instead of re-probing botocore. Fragility note.

9. **AUR-C namespace-isolation test is behavioural (T6).** It warms the cache and asserts the strict lookup returns 0.0, rather than asserting the literal key string. Sufficient today; captured as lesson C12 in `_LIVE_AUDIT_LESSONS.md`.

10. **L85 `IncomingBytes` GetMetricData in `services/monitoring.py` (T2).** This is the legitimate log-group ingestion probe (different purpose from the inverted stale-metric probe that was deleted). A naive grep for `get_metric_data` would flag it — no action needed.

11. **Tranche-6 notepad reconstructed (T6).** The original tranche-6 notepad write failed and the notepad was reconstructed from the agent return. Verdict confidence is slightly thinner than the other 7 verifiers (no per-finding detail block); citations and the self-caught AUR-C/AUR-G blockers are intact.

12. **EKS-1 line drift (T2).** Prompt cited `:503`; actual probe spans L499-538 — fix is correct, only the line citation drifted under merge shift.

## What This Verification Does NOT Cover (Honest Limitations)

1. **The ~40 MEDIUM/LOW backlog items NOT fixed by tranches 1-6 were not re-verified as part of this report.** They remain open per the sweep doc's "Remaining backlog after tranche 7" section. The original-35 re-validation confirms 26 OPEN items map cleanly to backlog IDs, but the broader ~40-item backlog (which includes items beyond the original 35) is the next-fix-run scope, not this verification.

2. **Live AWS account scans were NOT re-run.** Verification was code-read + unit-test based. Several levers (notably the network_cost transfer query rewritten in NC-1) still need live-account validation that cannot be done from code alone. The NC-1 verifier specifically flagged this: the query shape is correct (REGION-scoped, no SERVICE constraint, `USAGE_TYPE` grouping, strict classifier, zero-result warn) but only a live scan will confirm the actual transfer rows surface as expected for a real account's billing.

3. **4 real CoH types still unbucketed (declared, not a defect).** Per the rank-7 tranche-4 work, `MemoryDBCluster`, `DocumentDBCluster`, `SageMakerEndpoint`, and `WorkSpaces` CoH types are not yet routed through `type_map`. This is declared in the backlog — it is not a defect, just unfinished scope.

4. **Tranche-6 confidence is slightly thinner** because the original notepad write failed and was reconstructed from the agent return (see residual risk #11). All 7 cited fix commits are present and the per-finding verdicts + file:line citations are intact, but there is no full per-finding detail block to cross-check.

5. **No new live SKU re-pricing was performed** beyond the spot re-cites already documented in pricing_engine.py comments and the verifier re-citations (e.g. CF-4 `QUEZ7XDZJJXURBU7`, AG-3 cache-hourly range, MSK broker pricing). Rates marked UNVERIFIED in code (BR-2 PT_HOURLY_PRICE) remain unverified.

## Recommended Next Actions (Prioritized)

- **P1 — athena 0.75 factor (#20 / ATH-1).** Highest-value OPEN item. Escalated twice independently (verification HIGH, sweep CRITICAL). Touches `services/adapters/athena.py:71` — the unconditional `* 0.75` should be gated on `ExecutionEngine` / `EngineVersion` / format detection (Spark workloads only; v3 engine has different unit economics). Untouched by tranches 1-6. This is the single most material saving-inaccuracy left in the codebase.

- **P2 — the ~40 MEDIUM/LOW backlog (one more fix tranche).** The sweep's "Remaining backlog after tranche 7" plus the 26 OPEN items in the original-35 re-validation are the next-fix-run scope. The E1-class hygiene cluster (transfer #15, athena #19, ec2 #1, ebs #2, aurora #7, msk #13, monitoring #35, commitment #25 — bare `except Exception` paths not routed through `record_aws_error`) is the natural bundle; LAM-4 (lambda FunctionName-only dimension, #17), SM-tail (sagemaker idle-endpoint multi-variant pricing, #30), and BR-5 (bedrock per-model token rates, #31) are the highest-value cost-fidelity items in the backlog.

- **P3 — live-account validation of the rewritten network_cost transfer query (NC-1).** Cannot be done from code alone. The query shape is correct (REGION-scoped, USAGE_TYPE-grouped, strict classifier, zero-result warn) but only a live scan will confirm real transfer rows surface as expected for a real account's billing. Same applies to a sample of the new counted levers (TR-1, MSK-1, NET-E) — their polarity and dimension-sets are correct in code, but a live scan is the only true end-to-end check.

- **P4 — 4 unbucketed CoH types (MemoryDBCluster, DocumentDBCluster, SageMakerEndpoint, WorkSpaces).** Declared in the backlog. Each needs a `type_map` entry plus a corresponding `_HUB_SERVICES` bucket if one does not exist. Not a defect — unfinished scope.

- **Defense-in-depth (non-blocking):** (a) freeze `RDS-1` `_spent` into a separately-held mutable or assert identity at gate entrypoints; (b) add a `# DO NOT USE FOR COUNTED $` sentinel or CI grep guard above the BR-2 bedrock PT rate table; (c) add a `max_pages` cap to the GL-1 manual NextToken fallback.

---

Report generated 2026-08-10. Source notepads: `.zcode/notepads/audit-services-deep/verify-pass3.md`, `verify-tanche1.md`, `verify-tanche2.md`, `verify-tranche3.md`, `verify-tranche4.md`, `verify-tranche5.md`, `verify-tranche6.md`, `verify-original35.md`.
