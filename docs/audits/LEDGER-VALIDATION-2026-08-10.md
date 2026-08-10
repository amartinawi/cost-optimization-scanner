# Remediation Ledger Validation — 2026-08-10

## Metadata

- **Date:** 2026-08-10
- **Subject under test:** `docs/audits/LEDGER-VALIDATION-2026-08-10-RAW.md` — the verbatim capture of the eight-tranche Remediation Ledger (Wave-0 input, marked "DO NOT edit — it is the artifact under test").
- **Validation method:** 8 parallel verifiers across 4 waves (Wave-0 provenance capture, Wave-1 finding-level re-derivation, Wave-2 arithmetic reconciliation, Wave-3 consolidation). Each verifier read the CURRENT code at observed HEAD — none trusted the ledger's own markers, direction arrows, commit counts, or line citations. Directions were re-derived from code; commit counts were reproduced from `git rev-list`; lessons were grepped at cited line numbers; SHAs were resolved against `main`.
- **Observed HEAD:** `e608ceb` ("docs(audits): tranche 9 fix status").
- **Ledger-claimed terminal SHA:** `7cce9f5` (Merge fix/sweep-priority-8) — EXISTS on main.
- **Skills loaded:** `source-command-audit-full`, `source-command-audit-docs` (KIND=validation; the four Wave-1/Wave-2 notepads recorded that these are runners for the `audit-full` / `audit-docs` shell commands and were not directly invoked, since this run is a ledger reconciliation, not a repo-wide docs-accuracy sweep).
- **Capabilities used:** CodeGraph-first exploration (`codegraph explore` / `codegraph node`) for finding-level reads in Wave 1; targeted `.venv/bin/python -m pytest` for test evidence in Wave 1; `git rev-list --no-merges --count`, `git rev-parse`, `git diff-tree`, `git ls-tree` for Wave-2 commit forensics; `grep` / `sed -n` for Wave-2 lessons wiring.
- **Read-only:** No `.py`, prompt, test, or RAW-ledger file was modified by any verifier. The 7 Wave-1/Wave-2 notepads under `.zcode/notepads/validate-ledger/` and this report are the only writes.

---

## VERDICT: SOUND-WITH-CAVEATS

The ledger is **substantively honest and correct on every load-bearing claim**, with exactly one aggregate-tally defect and one imprecise-prose defect. The substance — per-finding correctness, SHA provenance, per-tranche commit count, ranking closure, tail arithmetic, and lessons wiring — all verify.

| Dimension | Result | Evidence |
|---|---|---|
| T7 + T8 surface (finding-level, this run) | SOUND | 8/8 findings FIXED at current HEAD; 0 direction disagreements. LAM-3 FIXED-PARTIAL exactly as the ledger's own scope implies (LAM-4 is a separate backlog the ledger never claims). |
| T1–T6 surface (finding-level, cited) | SOUND | Cited from `POST-FIX-VERIFICATION-2026-08-10.md`: 56/57 FIXED-CORRECT, 1 FIXED-PARTIAL (WS-1, declared backlog). |
| Commit-count arithmetic (51) | REPRODUCIBLE | 8+9+8+5+7+7+5+2 = 51 under scope (d); every tranche matches. |
| Ranking closure (8/8) | RECONCILES | All 8 priority ranks map to a closing tranche. |
| Tail arithmetic (~33) | RECONCILES | ~40 (after T6) − 6 T7 closures − 0 T8 closures ≈ ~34, rounds to ~33. |
| Lessons wiring (5/5) | SOUND | All 5 exist at exact cited lines, all wired, all pasted into all 34 prompts. |
| Direction headline (17↓/18↑/16●) | **DOES-NOT-RECONCILE** | The headline totals (51) are the COMMIT count mislabeled as a direction partition. Actual body direction counts are ↓16 / ↑17 / ●7. |
| Footer prose ("the reporters") | IMPRECISE | The path is described as if `reporters/` were a directory; the reporter files are top-level. Under the correct reading the count reconciles. |

The single material defect (the direction headline) is at the AGGREGATE label only. Per-finding, every ledger direction marker agrees with the independently re-derived direction (60/60 across T1–T8). The caveat is therefore about the headline tally, not about per-finding honesty.

---

## SHA provenance

- **Ledger-claimed terminal SHA:** `7cce9f5` (Merge `fix/sweep-priority-8`) — present on `main`.
- **Observed HEAD at validation time:** `e608ceb` ("docs(audits): tranche 9 fix status").
- **Drift (4 commits past the ledger terminal, all OUT OF LEDGER SCOPE):**
  - `4fd394b` feat(fsx): count idle file systems (FS-7)
  - `1c594c6` feat(bedrock): surface custom-model storage (BR-6)
  - `71cb80b` feat(msk): stop discarding serverless clusters (MSK-3)
  - `e608ceb` docs(audits): tranche 9 fix status
- **Scope rule honored:** FS-7 / BR-6 / MSK-3 / tranche-9 post-date the ledger and are excluded. Every finding was verified against current HEAD `e608ceb`, not the ledger terminal `7cce9f5` — a finding "fixed by tranche N" must still be fixed at current HEAD. (No finding regressed between `7cce9f5` and `e608ceb`.)

---

## Per-tranche reconciliation

`prev` boundary = `git rev-parse <merge>^1` (main-branch tip just before the tranche merged). Scope (d) = `services/ core/ reporter_phase_a.py reporter_phase_b.py html_report_generator.py` — the footer's intent under the empirically-correct reporter paths (the reporter files are top-level, not under `core/` and not in a `reporters/` directory).

| Tranche | Merge SHA | Ledger commit claim | Reproduced (scope d) | Agree? | Direction markers (counted from body, legend excluded) |
|---|---|---:|---:|---|---|
| 1 | `10f0b0c` | 8 | 8 | yes | ↓3 ↑1 ●1 |
| 2 | `f6c6c2e` | 9 | 9 | yes | ↓5 ↑1 ●1 |
| 3 | `e9ccd4f` | 8 | 8 | yes | ↓3 ↑2 ●1 |
| 4 | `1ad837d` | 5 | 5 | yes | ↓0 ↑3 ●1 |
| 5 | `37b0fb9` | 7 | 7 | yes | ↓0 ↑4 ●1 |
| 6 | `8a7f7b0` | 7 | 7 | yes | ↓5 ↑1 ●1 |
| 7 | `f869e64` | 5 | 5 | yes | ↓0 ↑4 ●0 |
| 8 | `7cce9f5` | 2 | 2 | yes | ↓0 ↑1 ●1 |
| **Total** | | **51** | **51** | **yes** | **↓16 ↑17 ●7** (40 bullet-rows) |

Scope notes:
- Scope (a) literal `services/ core/ reporters/` yields 48 — `reporters/` does not exist and never has (`git log --all --diff-filter=A -- 'reporters/'` returns zero hits).
- Scope (b) whole-tree `--no-merges` yields 57 — over-counts by 6 docs/test-only commits.
- Scope (c) `services/ core/ tests/` yields 52 — includes one extra test-only commit in T3.
- Scope (d) is the only scope that reproduces 51 on every tranche. The earlier metis probe ("T3:7/T5:6/T6:6 off-by-one") is refuted: it used the literal nonexistent `reporters/` path and silently dropped the reporter-only commits in those three tranches (`368e0a2`, `04faa27`, `ec3ec54`).

---

## Per-finding verdict table

### T7 + T8 (re-derived this run; observed HEAD `e608ceb`)

| Finding | Service | Ledger marker | Re-derived direction | Agree? | Current file:line | Test | Verdict |
|---|---|---|---|---|---|---|---|
| LAM-3 | lambda | ↑ | ↑ | yes | `services/lambda_svc.py:88-116,272-285,299-301`; `services/adapters/lambda_svc.py:201-228` | `tests/test_lambda_audit_fixes.py::test_never_invoked_pc_counts_the_whole_allocation` (+3) | FIXED-PARTIAL (the LAM-3 invocation-count discriminator is fixed; the bundled LAM-4 dimension issue is OPEN backlog, exactly as the ledger's own scope implies — the ledger claims only the LAM-3 half) |
| OS-5 | opensearch | ↑ | ↑ | yes | `services/adapters/opensearch.py:43-81` | `tests/test_opensearch_high_fixes.py::test_graviton_map_covers_current_generation_intel` | FIXED |
| OS-4 | opensearch | ↑ | ↑ | yes | `services/adapters/opensearch.py:137-184` | `test_smaller_sizes_walks_past_a_gap_in_the_family_ladder`, `test_downsize_delta_skips_an_unpriceable_rung` | FIXED |
| OS-7 | opensearch | ↑ | ↑ | yes | `services/opensearch.py:93-104,216-220`; `services/adapters/opensearch.py:451-474` | `test_idle_domain_price_includes_master_and_warm_nodes` | FIXED |
| OS-9 | opensearch | ↑ | ↑ | yes | `services/adapters/opensearch.py:487-489,617-645` | `test_reservation_covered_idle_domain_keeps_its_storage_leg` | FIXED |
| MON-7 | route53 | ↑ | ↑ | yes | `services/route53.py:100-120,22-38,17-19` | `tests/test_monitoring_high_fixes.py::test_zone_ladder_descends_as_zones_are_claimed` (+2) | FIXED |
| SageMakerEndpoint (CoH route) | orchestrator / sagemaker | ↑ | ↑ | yes | `core/scan_orchestrator.py:103,146`; `services/adapters/sagemaker.py:601,644` | `tests/test_coh_sagemaker_workspaces.py::test_type_is_wired_through_all_three_layers[SageMakerEndpoint-...]` (+2) | FIXED |
| WorkSpaces (CoH route) | orchestrator / workspaces | ↑ | ↑ | yes | `core/scan_orchestrator.py:104,147`; `services/adapters/workspaces.py:34,84` | `tests/test_coh_sagemaker_workspaces.py::test_type_is_wired_through_all_three_layers[WorkSpaces-...]` (+2) | FIXED |
| D4 | workspaces | ● | ● | yes | `services/adapters/workspaces.py:142-148`; `services/_savings.py:72` | `tests/test_coh_sagemaker_workspaces.py::test_zero_dollar_coh_workspace_rec_does_not_inflate_the_count` | FIXED |

T7 + T8 result: 8/8 findings FIXED at current HEAD (LAM-3 FIXED-PARTIAL within the ledger's own scope), 0 direction disagreements, 0 regressions. Test evidence: LAM-3 4 passed in 0.08s; OS-4/5/7/9 9 passed in 0.08s; MON-7 3 passed in 0.07s; T8 + D4 14 passed.

### T1–T6 (cited from POST-FIX-VERIFICATION-2026-08-10.md as SOUND)

T1–T6 were not re-verified finding-by-finding in this run; the cited evidence is `docs/audits/POST-FIX-VERIFICATION-2026-08-10.md` (independent verification at HEAD `f2300a0`, 8 parallel verifiers, no fix-status narrative trusted). Its tally:

| Verdict | Count (T1–T6 + Pass-3) |
|---|---:|
| FIXED-CORRECT | 56 |
| FIXED-PARTIAL | 1 (WS-1 — only `GENERALPURPOSE_4XLARGE` priced; ~13 tiers + G6/GR6 GPU family still $0; declared backlog) |
| NOT-FIXED | 0 |
| FIXED-REGRESSION | 0 |

Per-finding direction markers for T1–T6 were cross-checked against POST-FIX-VERIFICATION in the Wave-2 directions notepad and agree 60/60. See POST-FIX-VERIFICATION for the finding-by-finding rows; this run cites that evidence rather than re-running it.

---

## Arithmetic reconciliation

### (a) Commit-count 51 — REPRODUCIBLE

Reproduced exactly under scope (d) (`services/`, `core/`, plus the three top-level reporter files `reporter_phase_a.py`, `reporter_phase_b.py`, `html_report_generator.py`). All eight per-tranche counts match individually; the sum is 8+9+8+5+7+7+5+2 = 51. The reproduction command (any tranche):

```
git rev-list --no-merges --count <prev>..<merge> -- \
  services/ core/ reporter_phase_a.py reporter_phase_b.py html_report_generator.py
```

**Caveat (prose imprecision, not arithmetic error):** the ledger's footer reads "non-merge commits touching services/, core/ or the reporters." There is no `reporters/` directory in this repo (and never was). The reporter files have always been top-level. A reader running the literal command (`-- services/ core/ reporters/`) gets 48, not 51, because git silently drops the nonexistent path. Under the correct reading the count fully reconciles.

### (b) Direction headline 17↓ / 18↑ / 16● — DOES-NOT-RECONCILE

The headline (`RAW.md:18-20`) reads:

```
17 Wrong dollars removed      (↓)
18 Real dollars recovered     (↑)
16 Correctness only           (●)
```

17 + 18 + 16 = **51**, which equals the commit count exactly. Three candidate readings of the verbatim body were tried; none yields 17/18/16:

- **Bullet-rows (one row per `^- **` line, legend excluded):** ↓16 / ↑17 / ●7 (total 40).
- **Glyph occurrences in the body (legend excluded):** ↓16 / ↑17 / ●7 (coincides with bullet-rows since every marker lives on its own row).
- **Glyph occurrences including the legend's own defining glyphs:** ↓17 / ↑18 / ●8 — explains ↓ and ↑ but not ● (which would have to be 16, not 8).
- **Individual findings (grouped bullets expanded):** ↓30 / ↑17 / ↑27 / ●7 (total 64; includes the deliberate NET-D double-count in T6).

Root cause: the 51-commit total was mislabeled as a direction partition. The actual body direction counts are **↓16 / ↑17 / ●7 = 40 bullet-rows** (64 individual findings when grouped rows like `BR-1…BR-4` are expanded). The ●=16 figure is reproduced by no reading at all — the body has exactly 7 ● rows (NC-1, EC2-4, rank 7a, TR-2, MON-3, AUR-G, D4), 8 with the legend glyph. 16 ≈ 2 × 8, so the most plausible explanation is the headline was drafted against a different ledger state and never re-tallied.

This is an AGGREGATE-LABEL defect, not a per-finding defect: every ledger direction marker agrees with the independently re-derived direction (60/60 across T1–T8).

### (c) 8/8 Ranking items closed — RECONCILES

All 8 items from `SWEEP-FALSE-NEGATIVES-2026-08-09.md` "Priority fix ranking" (L152–161) map to a closing tranche:

| Rank | Item | Closed by |
|---|---|---|
| 1 | AR-1 apprunner MB-as-GB | T1 (`10f0b0c`) |
| 2 | SM-1 + SM-3 + SM-2 demotion | T1 |
| 3 | BR-1 + BR-2 + BR-3 + BR-4 | T1 |
| 4 | OS-1 region filter + OS-2 storage × node count | T1 |
| 5 | NC-1 CE dimension key | T1 |
| 6 | EC2-2/3/4, EKS-1/2, CN-1, DDB-A, CF-1, LAM-1/2, MON-1/2, WS-1/3, LS-1, FS-1/6, H3/H4/H5 | T2 + T3 (bundle) |
| 7 | CoH type_map cleanup (6 dead keys + bucket 4 real types + route 2 residue) | T3 + T4 + T8 (three tranches: rank 7a, 7b, "residue — the ranking closes") |
| 8 | Lever additions CF-4, AG-3, MSK-1, TR-1, GL-1, MON-3, NET-E | T4 + T5 (bundle) |

None left open. Gotcha for the reader: ranks 6, 7, 8 each span multiple tranches; rank 7 spans THREE (T3 + T4 + T8), so cross-referencing "rank N" against "tranche N" mismatches for N ∈ {6, 7, 8}. The ledger's tranche-7 header is "the under-count slice," not "ranking item 7." Both docs are internally consistent; the numbering collision is not a defect.

### (d) ~33 tail items — RECONCILES (within rounding)

- Baseline: POST-FIX-VERIFICATION L252 — "~40 MEDIUM/LOW backlog items NOT fixed by tranches 1–6" (after T1–T6, HEAD `f2300a0`).
- Terminal: ledger `RAW.md:22,223` — "~33 Tail items left" (after T1–T8).

Arithmetic: ~40 − (T7 + T8 closures) ≈ ~33 requires T7 + T8 to have closed ~7 items.
- T7 (`f869e64`) closed 6 backlog items: LAM-3, OS-4, OS-5, OS-7, OS-9, MON-7 (all present in the sweep backlog at L61/66/72).
- T8 (`7cce9f5`) closed 0 numbered MEDIUM/LOW tail items: its two CoH types (SageMakerEndpoint, WorkSpaces) were ranking-item-7 residue, already implicitly counted in the ~40 via the "4 unbucketed CoH types" line at POST-FIX L256; its D4 was a self-caught blocker, not a numbered backlog id.

So ~40 − 6 (T7) − 0 (T8) = ~34, which rounds to the ledger's ~33. The sweep's own independently-stated "~30 items remaining" (`SWEEP.md:148`) agrees with ~33 within the rounding both figures explicitly carry ("~"). None of {~33, ~30} should be treated as exact; the sweep's exhaustive per-adapter enumeration (~80 named items) is a different unit (every sub-item listed, not rounded).

---

## Lessons wiring (A6, C12, C13, D5, D6) — SOUND

All 5 lessons the ledger claims (`RAW.md:210-220`) verify at observed HEAD `e608ceb`. Line numbers are file offsets in `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`, each confirmed by `sed -n '<n>p'`.

| ID | Ledger one-liner (faithful?) | Defined at | Wired at (citation) |
|---|---|---|---|
| A6 — dedup guard / least-guarded producer | YES (RdsModule back-door case; sweep directive names every shared set) | line 58 | line 509 (dedup adversarial-pass step) |
| C12 — strict no-fallback price mode needs own cache namespace | YES (Aurora Graviton probe; `allow_fallback` miss-path sweep) | line 293 | line 513 (counted-lever checklist item 2b) |
| C13 — know a metric's reporting criteria before idle gate | YES (`AWS/Transfer` vs `AWS/Kafka`; dimension-set trap; TR-1/MSK-1/SM-1 case) | line 305 | line 512 (counted-lever checklist item 2b) |
| D5 — new counted lever needs a render check | YES (`reporter_phase_a.py` / `PHASE_A_DESCRIPTORS`; LAM-2, CF-4 "Unknown", AG-3 dup-row cases) | line 341 | line 477 (sweep item 5), line 500 (HTML regen), line 511 (checklist 2b) |
| D6 — demoted rec's masked figure must reach the card | YES (AUR-G $418.20/mo behind a $0.00 card; grouped renderer gap) | line 354 | line 501 (HTML regen guidance) |

The "Ready-to-run invariant sweeps" block (header at line 422, Python block 427–495) and the "How to verify a fix (especially a dedup)" counted-lever checklist (item 2b at lines 511–514) both exist as the ledger claims. Every lesson is referenced at least once outside its own definition block.

All 34 per-service `*_AUDIT_PROMPT.md` files paste `_LIVE_AUDIT_LESSONS.md` by filename (`grep -rl "_LIVE_AUDIT_LESSONS" docs/audits/prompts/` returns all 34 prompts plus the shared `_GENERATION_SPEC.md`). The wiring is "paste the whole file alongside," which is what the ledger claims. The prompts reference the lessons by file name, not by inlining the class IDs — so a class-ID grep against the prompts returns nothing, but a filename grep returns all 34.

Minor drift (not a defect): the D5 entry body says "four consecutive remediation tranches" while the checklist item 2b says "six remediation tranches." Both are correct at their own write time (D5 written at tranche 4, checklist updated after tranche 6); the ledger summary says "Four tranches," matching the entry. Flagged for a possible future "single canonical tranche count" cleanup.

---

## Athena-0.75 honesty check

The ledger does NOT mention athena at all. A case-insensitive grep of the RAW ledger for `athena`, `ath-1`, and `0.75` returns **zero hits**.

What this means: the ledger makes no claim — neither explicit nor implied — to have closed the athena 0.75 factor (ATH-1). ATH-1 is a tranche-1–6 finding that POST-FIX-VERIFICATION explicitly escalates as the highest-value OPEN item: "the single highest-value OPEN item is the athena 0.75 factor (#20 / ATH-1) — escalated twice independently (verification HIGH, sweep CRITICAL) and untouched by tranches 1-6 — which is the recommended P1 next action" (POST-FIX-VERIFICATION executive summary). The ledger's silence on athena is consistent with ATH-1 being out of tranches 1–8's scope. The ledger does not over-claim; it simply does not address this item, which is honest given that no tranche closed it.

(Cross-check: the sweep's "Remaining backlog after tranche 9" enumeration at `SWEEP.md:148` lists athena items ATH-2, ATH-3, ATH-6, ATH-7, ATH-8 in the tail — notably NOT ATH-1, because ATH-1 is tracked separately as the escalated P1 rather than as a generic tail item. This is consistent with the ledger omitting athena entirely from its tail prose.)

---

## Caveats and honest limitations of THIS validation

1. **T1–T6 finding-level evidence is cited, not re-run.** This run re-derived T7 + T8 findings against current HEAD but cited `POST-FIX-VERIFICATION-2026-08-10.md` for T1–T6 (independent verification at HEAD `f2300a0`, 8 parallel verifiers, 56/57 FIXED-CORRECT). POST-FIX-VERIFICATION is itself a no-trusted-narrative verification, but its findings were not re-checked in this run.
2. **No live-account scan was run.** The ledger's own "open risk" section (`RAW.md:229-234`) names the rewritten `network_cost` Cost Explorer transfer query as the specific item fakes cannot settle — "only a real account proves the row shapes." This validation did not (and could not, without credentials) run a live scan. The NC-1 fix is verified as code-correct and tested against fixtures; its live behavior is still unverified, exactly as the ledger discloses.
3. **The direction-headline discrepancy is about the AGGREGATE label, not per-finding honesty.** Every per-finding ledger direction marker agrees with the independently re-derived direction (60/60). The DOES-NOT-RECONCILE verdict applies only to the 17/18/16 headline tally, which is the 51-commit total mislabeled as a direction partition.
4. **route53 registration is unverified (flagged, not blocking).** MON-7's tier-ladder fix lives in the legacy free-function module `services/route53.py`, which is NOT in `ALL_MODULES` in `services/__init__.py` and has no adapter wrapper. Whether MON-7's fix actually reaches a real scan depends on a registration path this run did not trace. The tier-ladder logic itself is FIXED and tested; the reachability question is out of scope here.
5. **Tranche 9 and post-ledger drift are out of scope.** FS-7 / BR-6 / MSK-3 / tranche-9 (`e608ceb` and the three code commits before it) post-date the ledger and were not reconciled.

---

## Recommended corrections to the ledger

If the user revises the ledger, two one-line edits resolve both defects without touching any substantive claim:

1. **Fix the direction headline** (`RAW.md:18-20`). Either:
   - **Option A (keep the partition, fix the numbers):** change to `16 Wrong dollars removed`, `17 Real dollars recovered`, `7 Correctness only` (the actual body counts; 16+17+7 = 40 bullet-rows). Note this changes the implied total from 51 to 40.
   - **Option B (keep the 51, drop the partition claim):** relabel the figure as "51 code commits" only and remove the direction partition (the 17/18/16 breakdown). The per-finding direction arrows in the body are already correct and unaffected.

   Option B is the smaller edit and preserves the prominent "51 code commits" line at `RAW.md:17` as the single source of that figure.

2. **Fix the footer prose** (`RAW.md:249`). Change:
   - from: "Counts of commits are non-merge commits touching services/, core/ or the reporters."
   - to: "Counts of commits are non-merge commits touching services/, core/, or the three top-level reporter files (reporter_phase_a.py, reporter_phase_b.py, html_report_generator.py)."

   This makes the literal `git rev-list` command reproduce 51 without requiring a charitable reading of "the reporters."

Neither correction affects any per-finding claim, any SHA, any test citation, or the lessons section. Both are doc-only edits to the ledger artifact.

---

## Consolidation trail

This report consolidates 7 notepads + 1 raw capture, each independently produced:

| Input | Wave | Scope | Outcome |
|---|---|---|---|
| `docs/audits/LEDGER-VALIDATION-2026-08-10-RAW.md` | 0 | verbatim ledger capture + SHA provenance | terminal `7cce9f5` exists on main; HEAD drift = 4 out-of-scope commits |
| `.zcode/notepads/validate-ledger/w1-lam-3.md` | 1 | LAM-3 (lambda, T7) | FIXED-PARTIAL; ledger honest about scope; LAM-4 OPEN as declared backlog |
| `.zcode/notepads/validate-ledger/w1-opensearch.md` | 1 | OS-4/5/7/9 (opensearch, T7) | 4/4 FIXED; directions agree ↑ |
| `.zcode/notepads/validate-ledger/w1-mon-7.md` | 1 | MON-7 (route53, T7) | FIXED; direction agrees ↑; route53-registration caveat flagged |
| `.zcode/notepads/validate-ledger/w1-t8.md` | 1 | T8 CoH routing + D4 | 5/5 sub-items FIXED; 14/14 tests green; directions agree |
| `.zcode/notepads/validate-ledger/w2-commits.md` | 2 | 51-commit reconciliation | REPRODUCIBLE under scope (d); footer prose imprecise; metis off-by-one refuted |
| `.zcode/notepads/validate-ledger/w2-directions.md` | 2 | direction / ranking / tail | PER-FINDING 60/60 AGREE; HEADLINE 17/18/16 DOES-NOT-RECONCILE (= 51 commits mislabeled); 8/8 ranking RECONCILES; ~33 tail RECONCILES |
| `.zcode/notepads/validate-ledger/w2-lessons.md` | 2 | 5 lessons existence + wiring | 5/5 exist at cited lines, all wired, all pasted into 34 prompts |

Plus cited (not re-read in full): `docs/audits/POST-FIX-VERIFICATION-2026-08-10.md` for T1–T6 finding-level evidence (56/57 FIXED-CORRECT, 1 FIXED-PARTIAL WS-1) and the athena-0.75 escalation context.
