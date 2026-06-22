# Per-Service Cost-Audit Prompt

A reusable prompt for auditing **one** AWS service adapter at a time in a fresh
session. Replace `<SERVICE>` with the target (e.g. `s3`, `rds`, `lambda`,
`ebs`, `dynamodb`, `elasticache`, `network`, …) and paste the whole thing.

It encodes every failure class found during the EC2 audit so each new service
gets the same scrutiny: silent failures, inaccurate $, double counting, missed
opportunities, coverage gaps, render desync, and undefended numbers.

---

## PROMPT (copy from here)

You are auditing the **`<SERVICE>`** adapter of this AWS cost-optimization
scanner. Scope is **strictly cost**: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the `<SERVICE>` path: `services/adapters/<SERVICE>.py`,
   its legacy shim `services/<SERVICE>.py`, the helpers they import
   (`services/_savings.py`, `services/advisor.py`), `core/contracts.py`,
   `core/pricing_engine.py`, `core/scan_orchestrator.py`, and the reporter
   (`reporter_phase_a.py`, `reporter_phase_b.py`, `html_report_generator.py`).
2. List **every** cost check the adapter emits, and for each give: trigger
   condition, the data source (Cost Optimization Hub / Compute Optimizer /
   CloudWatch / describe-API / pricing API), the savings formula, and any
   reduction factor or constant. Note which sources the adapter aggregates.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each savings figure, re-derive it from the live AWS Pricing API and
   confirm it matches. Check specifically:
   - **Region-correct** pricing (not us-east-1 hardcoded).
   - **OS / edition / engine correct** — e.g. EC2 priced by real
     `PlatformDetails` (Windows ≠ Linux), RDS by engine, not a default.
   - **License model** — Windows/SQL/BYOL use the right `licenseModel` SKU;
     pricing filters must be deterministic (pinned filters, not `MaxResults=1`
     over multiple matching SKUs).
   - **Spot / discounted** resources aren't priced at on-demand.
   - Reduction factors/heuristics are calibrated and labelled, not arbitrary.
4. Confirm the savings basis is **defensible from the report alone**: each
   finding should record what was priced (OS/rate/metric window) — if you can't
   tell from the data how the number was derived, that's a finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** can one resource match multiple checks and have its
   savings stacked beyond 100% of its cost? (e.g. idle + rightsize + migrate.)
6. **Cross-source:** Cost Optimization Hub re-surfaces Compute Optimizer
   findings, and heuristic checks re-detect the same resources. Verify dedup by
   resource id with a clear authority order (CoH > Compute Optimizer >
   heuristics) and that `core/result_builder.py` doesn't just blindly sum.
7. **Cross-adapter:** does this service's check overlap another adapter's domain
   (e.g. a volume/snapshot/IP counted in two tabs)? Single-responsibility wins.

### Phase 4 — Coverage (works for ALL resource types, not a subset)
8. Are checks gated to hardcoded families/types/states that silently exclude
   valid resources? (EC2 prev-gen was t2-only; instance-store was a 5-family
   allowlist.) Confirm the scan paginates and covers every resource type, size,
   state, and lifecycle under the service.
9. Are whole resource classes skipped (e.g. tag-based exclusions, only-running,
   only-provisioned)? Confirm each skip is intentional and documented.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, and `return 0.0`/`return []`
    fallback. Does any swallow an error without `ctx.warn` / `ctx.permission_issue`?
    (CloudWatch throttling, pricing-API miss → $0, missing IAM.)
11. Does the pricing engine fall back to `0.0` and still emit a finding? A
    finding with `$0`/blank savings is a bug — it must be skipped or recorded.
12. **Cost Optimization Hub dropped types:** check `scan_orchestrator`'s
    `type_map` / `_HUB_SERVICES`. Does AWS return a `currentResourceType` for
    this service that has **no bucket** (silently dropped) or a bucket **no
    adapter consumes**? Run a full-region scan and watch for the
    "recommendation type(s) had no service bucket" warning.
13. Are opt-in placeholders or "enable X" nudges emitted as `$0` recommendations
    that inflate the count? They should be warnings, not findings.

### Phase 6 — Reporting (one tab, counted == rendered)
14. Confirm all of the adapter's sources render under a **single** `<SERVICE>`
    tab, each with a registered renderer (no source silently unrendered).
15. **Counted == rendered:** every render-time filter (substring drops,
    `MigrateToGraviton`/`Optimized`/RI filters, `_filter_*`) must also be
    reflected in the counted savings/total — otherwise the tab shows a headline
    the visible cards don't account for. Verify the per-tab total equals the sum
    of rendered findings, and the headline equals the sum of per-service totals.
16. Confirm no finding is counted in `total_recommendations`/
    `total_monthly_savings` but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to this service and pass the JSON through
    `tools/scan_doctor.py <json> --service <SERVICE>`. Triage every:
    silent failure, `$0`/missing-savings finding, and resource appearing in >1
    source. Reconcile the headline against the per-source sum.
18. For any duplication claim, prove it: show the same resource id in two
    sources. For any accuracy claim, show the AWS Pricing API value next to the
    scanner's.

### Deliverable
- The complete check list (Phase 1.2).
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS API value), and the dollar/coverage
  impact. Separate **confirmed bugs** from **known limitations / tradeoffs**.
- A fix plan. Implement only after I approve; when you do: add unit tests
  (pure decision logic extracted and tested without AWS), keep the regression
  gate green (`pytest tests/test_regression_snapshot.py
  tests/test_reporter_snapshots.py`), refresh reporter snapshots only when the
  change is intentional, and record an audit basis (rate/OS/metric) on each
  finding so the number is defensible.

### Known issue catalogue to check against (found in prior audits)
- Savings priced as Linux for non-Linux OS; BYOL priced as license-included.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Same resource counted by Cost Hub + Compute Optimizer + heuristic.
- Two name-pattern checks matching the same resource (overlapping keywords).
- `$0` "enable monitoring"/opt-in placeholder counted as a recommendation.
- Render-time substring/category drops desyncing counts from the table.
- A whole consolidated/aggregated report duplicating per-item findings.
- Coverage gated to a hardcoded family/type allowlist.
- Cost Optimization Hub resource type with no bucket → silently dropped.
- CloudWatch / Cost Explorer permission failure printed but not recorded.
- Heuristic that assumes a target (e.g. "shrink to 20GB") with no usage evidence.
- Cross-adapter overlap (same volume/IP/snapshot in two tabs).
- **Reduction factor instead of exact price delta**: a rightsizing/migration saving computed as `price × factor` rather than `(current_price − target_price)`. Prefer the real delta to a concrete recommended target (migration map, one size smaller); validated factors can be off 2–3× (EC2 m4→m6i was 2.8× too high).
- **Single-signal utilization**: idle/rightsizing from CPU alone. Corroborate with always-available signals (NetworkIn/Out) and, when present, agent metrics (memory) — but only to *suppress* false positives, never to invent findings.
- **Agent-metric dimension mismatch**: CloudWatch-agent metrics (CWAgent mem/disk) published under more dimensions than InstanceId; a get_metric_statistics by InstanceId alone silently returns nothing. Use list_metrics to discover the real dimension set.
- **Managed-fleet double count**: per-instance heuristics applied to Auto Scaling Group members (which are sized via launch template / ASG Compute Optimizer). Add ASG members to the dedup `covered` set.
- **License model**: BYOL detected from `PlatformDetails` ("Windows BYOL") priced at the license-included rate (overstated). Map to the BYOL `licenseModel` SKU.
- **Unquantifiable interruptibility**: never recommend Spot implicitly — require an explicit operator tag (workload is interruptible) and price the on-demand−Spot delta from `describe_spot_price_history`.
- **Scheduling**: non-prod 24/7 instances → quantify an off-hours stop schedule from the real monthly cost × a documented off-fraction (not a bare nudge).

## PROMPT (end)
