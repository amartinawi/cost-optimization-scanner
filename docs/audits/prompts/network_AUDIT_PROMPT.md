# Network Adapter Cost-Audit Prompt

A deep, network-specific audit brief in the same structure as the Lambda / RDS /
EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* network code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

> This is the canonical prompt for the **`network`** adapter — `NetworkModule`
> in `services/adapters/network.py` (display "Network & Infrastructure"). It is
> DISTINCT from **`network_cost`** (`NetworkCostModule`, "Data Transfer", see
> `network_cost_AUDIT_PROMPT.md`); do not conflate the two. Both are live in
> `ALL_MODULES`. (This prompt was added 2026-06-27 to close a coverage gap — the
> module had no prompt in `docs/audits/prompts/` and was missed by the
> all-services audit; the canonical content lived only at
> `docs/NETWORK_AUDIT_PROMPT.md`.)

> **Current state (verify, don't assume).** Commit `90723e3`
> ("fix(network): render desync, ALB pricing, ASG/NAT dedup, silent failures")
> already addressed several historical issues: all five per-domain sources now
> have `PHASE_B_HANDLERS` entries (render-desync fixed), ALB is priced with its
> own SKU (not Classic), and sub-shim failures route through
> `record_aws_error`. **Re-verify each of these holds in the current code**
> rather than assuming it is still broken — and focus new effort on the
> double-count and metric-gating issues confirmed in the 2026-06-27 gap-audit
> (NET-01…NET-07, listed at the end).

---

## PROMPT (copy from here)

You are auditing the **`network`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. Treat the
**EC2** adapter (`services/adapters/ec2.py`) as the canonical model for
cross-source / ASG-member dedup and the `$0`-placeholder→warning pattern, the
**RDS** adapter (`services/rds_logic.py`) for advisory-category demotion, and the
recently-audited **Lambda** adapter (`services/adapters/lambda_svc.py`) as the
worked example for the `mark_zero_savings_advisory` pattern, rate-string
rejection, and the test style I expect (`tests/test_lambda_audit_fixes.py`,
`tests/test_rds_audit_fixes.py`).

### NOTE on structure (network is NOT shaped like EC2/RDS/Lambda)
- The network adapter is a **COMPOSITE** of five sub-shims, aggregated in
  `services/adapters/network.py` → `NetworkModule.scan` (≈98-160):
  - `get_elastic_ip_checks`   → `services/elastic_ip.py`
  - `get_nat_gateway_checks`  → `services/nat_gateway.py`
  - `get_vpc_endpoints_checks`→ `services/vpc_endpoints.py`
  - `get_load_balancer_checks`→ `services/load_balancer.py`
  - `get_auto_scaling_checks` → **`services/ec2.py`** (imported across domains —
    this is the cross-adapter overlap hot-spot, see Phase 3)
- There is **NO** `services/network.py` legacy shim. The five sub-shims ARE the
  helpers; treat each as a mini-adapter. The aggregation helpers
  (`_derive_severity`, `_annotate_severity`, `_safe_collect`, `_sum_savings`)
  live in `services/adapters/network.py` (≈40-86).
- The adapter emits **five per-domain SourceBlocks** (`elastic_ips`,
  `nat_gateways`, `vpc_endpoints`, `load_balancers`, `auto_scaling_groups`) —
  NOT a single `enhanced_checks` block. All five are registered in
  `PHASE_B_HANDLERS` → `_render_network_enhanced_checks` (reporter_phase_b.py
  ≈2484-2488) and tagged "Audit Based" in `SOURCE_TYPE_MAP` (≈2370-2374);
  `network` is in `_PHASE_B_SKIP_PER_REC` (no per-rec fallback). Confirm this
  wiring still holds (Phase 6).
- The `auto_scaling_groups` block is **advisory** (`Counted=False`): ASG
  rightsizing is owned by the EC2 tab. Confirm it contributes $0 counted.
- Network consumes **neither Cost Optimization Hub nor Compute Optimizer**. It is
  not in `scan_orchestrator._prefetch_advisor_data`'s `_HUB_SERVICES` / `type_map`,
  and pulls no CO helper. All savings are **local heuristics priced via
  `PricingEngine` (region-correct) with a hardcoded fallback constant per domain**.
  A "missing CoH/CO source" finding is NOT fair game here.
- Savings are carried as a human **string** in `EstimatedSavings` and converted
  to a number by `services/_savings.py:parse_dollar_savings`, which counts only a
  bare/`/month` dollar figure and **rejects per-unit rate strings** (`$0.01/GB`,
  `$0.045/GB`). `mark_zero_savings_advisory` then flags any rec that parses to
  `$0` as `Counted=False` (advisory). The counted total sums only `Counted=True`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `network.py` row. **Reconcile
    the doc against reality:** `core/pricing_engine.py` exposes
    `get_eip_monthly_price`, `get_nat_gateway_monthly_price`,
    `get_vpc_endpoint_monthly_price`, and the ELB family `get_alb_monthly_price`
    / `get_nlb_monthly_price` / `get_gwlb_monthly_price`. Note any method named in
    the doc that does not exist (e.g. the doc lists `get_clb_monthly_price()` but
    `load_balancer.py` never calls it — the Classic-ELB check emits a percentage
    string, no CLB price lookup), and any priced domain (Classic LB, NAT
    data-processing $/GB, public IPv4) that has **no** PricingEngine method and is
    therefore a hardcoded string constant. Flag every doc/code drift.
0b. Confirm module identity in `services/adapters/network.py`: `key="network"`,
    `cli_aliases=("network",)`, `display_name="Network & Infrastructure"`,
    `required_clients()=("ec2","elbv2","autoscaling","elb")`. Note it does NOT set
    `requires_cloudwatch` / `reads_fast_mode` even though sub-shims read CloudWatch
    (NAT `BytesOutToDestination`, LB LCU/NLCU) — flag if so.
0c. Network has **no AWS advisory source** (no CoH / CO). Savings are expected to
    be locally derived. Focus on pricing accuracy, the rate-string boundary, the
    intra-domain and cross-adapter (ASG) overlaps, the metric-gating of throughput
    heuristics, and render wiring.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the network path: `services/adapters/network.py` and all
   five sub-shims (`services/elastic_ip.py`, `services/nat_gateway.py`,
   `services/vpc_endpoints.py`, `services/load_balancer.py`, and
   `get_auto_scaling_checks` in `services/ec2.py`); the helpers they import
   (`services/_savings.py` — `parse_dollar_savings`, `mark_zero_savings_advisory`;
   `services/_aws_errors.py` — `record_aws_error`; the aggregation helpers in
   `services/adapters/network.py`); `core/contracts.py`, `core/pricing_engine.py`
   (the network methods), `core/scan_orchestrator.py`, `core/result_builder.py`,
   and the reporter (`reporter_phase_b.py:_render_network_enhanced_checks` ≈1132,
   `html_report_generator.py`).
2. List **every** cost check across all five sub-shims, and for each give:
   trigger condition, the data source (EC2/ELB describe-API, CloudWatch metric, or
   pure config heuristic), the exact `EstimatedSavings` string template, the
   constant/rate it embeds, and whether that string parses to a **counted** dollar
   or a **$0 advisory** (rate string / `$0.00 - requires CW …` / percentage). Map
   each check to its emitting SourceBlock. Known check inventory to confirm:
   - **elastic_ip**: unassociated EIP; EIP on stopped instance; multiple EIPs per
     instance (`(count-1)×eip`); in-use public-IP-should-be-private removal.
   - **nat_gateway**: multiple NAT same-AZ; cross-AZ consolidation; NAT in
     dev/test (sole-in-VPC); NAT-for-AWS-services (missing S3/DDB endpoint, $/GB);
     low-throughput NAT (CW-gated).
   - **vpc_endpoints**: interface endpoints in nonprod (`×az_count`); duplicate
     (>2 per vpc:service); missing S3/DDB gateway endpoint; unused/no-traffic
     (confirm whether these last categories are ever populated).
   - **load_balancer**: idle listeners (0 listeners); single-service ALB; shared
     ALB opportunity (`(standalone-2)×alb`); NLB-vs-ALB; old Classic ELB ("10-20%");
     zero-traffic ALB (confirm populated).
   - **ec2.get_auto_scaling_checks**: oversized ASG (advisory, `Counted=False`).

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** savings figure, re-derive it from the live AWS Pricing API
   and confirm it matches. Validate EACH domain constant separately:
   - **EIP**: `get_eip_monthly_price` vs the `3.65` fallback. In-use/auto-assigned
     public IPv4 is billed at **$0.005/hr ≈ $3.65/mo since 2024** — confirm the
     per-IP figure and the SKU (`AmazonVPC` / `PublicIPv4:InUseAddress`). An
     unattached-EIP saving AND an in-use-public-IP-removal saving are both
     legitimate; do not flag them as false positives, but confirm the free per-ENI
     IP on a running instance is not double-charged.
   - **NAT**: `get_nat_gateway_monthly_price` vs the fallback. Validate the base
     hourly ($0.045/hr ≈ $32.85/mo us-east-1); scrutinize any data-processing
     `+$/GB` addend and the cross-AZ `$0.045/GB` rate — confirm any `$/GB` figure
     is a **rate**, not a counted total.
   - **VPC endpoint**: `get_vpc_endpoint_monthly_price` vs the `7.30` fallback
     (Interface endpoint $0.01/hr/AZ ≈ $7.30/mo per AZ) — confirm the per-AZ
     multiplication (`×az_count`) is handled.
   - **Load balancer**: `get_alb_monthly_price` / `get_nlb_monthly_price` /
     `get_gwlb_monthly_price`. Confirm ALB is priced with its OWN
     `productFamily`/SKU (not Classic — the historical bug fixed in 90723e3);
     confirm NLB/GWLB use their own SKUs (not aliased to ALB). Confirm there is
     **no Classic LB rate** method and that the CLB migration check emits a
     percentage ("10-20%") that parses to $0 advisory and is not counted.
   - **Region scaling**: PricingEngine methods are region-correct; the **fallback
     constants are us-east-1** and embedded directly into the savings string.
     Confirm `ctx.pricing_multiplier` is applied on the fallback path (it likely is
     NOT) — a non-us-east-1 scan with `pricing_engine=None` would emit us-east-1
     dollars. Flag any missing region scaling on the fallback path.
   - **Reduction factors/heuristics** (ALB consolidation "to 2 ALBs", "10-20%")
     must be calibrated and labelled, not arbitrary; consolidation savings of
     `(count − N) × rate` must use a defensible floor N AND must not double-count
     against a per-resource check (see Phase 3 / NET-01).
4. Confirm the savings basis is defensible from the report alone. A NAT or LB
   saving that depends on throughput (cross-AZ GB, LCU) but is emitted as a flat
   base-rate total with no metric is a finding — prefer a CloudWatch-backed number
   or a $0 advisory. Record a structured **AuditBasis** (rate / region /
   metric-window / formula) on each counted finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-domain stacking / subset redundancy** — the confirmed hot-spots:
   - **Load balancer (NET-01, HIGH):** `single_service_albs` counts every
     single-listener ALB at full `alb_monthly` AND `shared_alb_opportunity`
     independently counts `(standalone_count-2)×alb_monthly` over the SAME
     standalone ALBs → the same ALBs counted twice; neither metric-gated.
   - **VPC endpoints (NET-02, MEDIUM):** a nonprod-tagged interface endpoint that
     is also the 3rd+ duplicate of its `vpc:service` is counted by BOTH
     `interface_endpoints_in_nonprod` AND `duplicate_endpoints`.
   - **Elastic IP (NET-03, LOW):** a stopped instance holding >1 EIP is counted by
     BOTH `eips_on_stopped_instances` AND `multiple_eips_per_instance`.
   Check every sub-shim for the same pattern (one check's population ⊆ another's);
   fix by removal / a per-resource `covered` set / advisory demotion, never a
   double-count.
6. **Cross-adapter (the big one):** `get_auto_scaling_checks` lives in
   `services/ec2.py` and is consumed by the **network** adapter, while the EC2
   adapter independently surfaces ASG rightsizing. Determine whether the SAME ASG
   (or its member instances) can be counted in BOTH the EC2 tab and the Network
   tab. The Network ASG block is advisory (`Counted=False`) — confirm that holds
   and that `core/result_builder.py` does not sum it into the headline.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls network resources into a synthetic tab, that an
   EIP/IP attached to an EC2 instance is not also counted by the EC2 adapter, and
   that NAT-hours (network) vs transfer-bytes (network_cost) stay single-owner with
   no shared resource double-counted across the two network adapters.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Are checks gated to hardcoded types/states that silently exclude valid
   resources? Confirm full **pagination** of `describe_addresses`,
   `describe_nat_gateways`, `describe_vpc_endpoints`, `describe_load_balancers`
   (elbv2 AND classic `elb`), `describe_auto_scaling_groups`. Confirm LB coverage
   includes **all types** (ALB / NLB / GWLB / Classic) across both the `elbv2` and
   `elb` clients.
9. Are whole classes skipped? IPv6-only / dualstack endpoints; **Gateway endpoints
   are free** and must NOT be flagged like Interface endpoints; NAT in different
   VPCs; ASGs with 0 desired capacity (no savings from a scaled-to-zero group).
   Also confirm the declared-but-never-populated categories
   (`unused_interface_endpoints`, `no_traffic_endpoints`, `zero_traffic_albs` —
   NET-05) are either implemented or removed, not left as dead descriptions.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`
    fallback:
    - `services/adapters/network.py:_safe_collect` — confirm it now routes sub-shim
      exceptions through `services/_aws_errors.record_aws_error`
      (`AccessDenied`/`UnauthorizedOperation`/`OptInRequired` → `ctx.permission_issue`,
      other → `ctx.warn`) rather than `logger.warning` only.
    - **ASG path (NET-04):** `services/ec2.get_auto_scaling_checks` catches its own
      exceptions with `ctx.warn` directly and **bypasses `record_aws_error`** — so
      an ASG-path AccessDenied is misclassified as a warn, out of step with the
      other four sub-shims. Confirm and fix.
    - Each sub-shim's own inner `except Exception as e:` — confirm none swallow
      describe-API or CloudWatch failures without recording them on `ctx`.
11. Does a pricing miss fall back to `0.0`/blank and still emit a finding? A `$0`
    counted finding is a bug — it must be advisory or skipped. Verify
    `mark_zero_savings_advisory` covers EVERY metric-gated / rate-string /
    percentage path (NAT data-processing $/GB, LB LCU, CLB "10-20%", VPC-endpoint
    $/GB) — none should be counted.
12. **CloudWatch gating / fast-mode:** sub-shims read CloudWatch (NAT
    `BytesOutToDestination`, LB LCU/NLCU). The adapter declares neither
    `requires_cloudwatch` nor `reads_fast_mode`. Confirm whether these reads are
    skipped under `ctx.fast_mode` and whether throttling/permission failures are
    recorded — mirror the Lambda fast-mode fix.
13. Are "enable X"/"requires metric" nudges emitted as `$0` recommendations that
    inflate the count? They must be advisory (`Counted=False`), rendered but
    excluded from counts.

### Phase 6 — Reporting (five sources, counted == rendered)
14. **Source-name vs handler mapping (verify it still holds):** the adapter emits
    five sources (`elastic_ips`, `nat_gateways`, `vpc_endpoints`, `load_balancers`,
    `auto_scaling_groups`). Commit `90723e3` registered ALL FIVE in
    `PHASE_B_HANDLERS` → `_render_network_enhanced_checks` (reporter_phase_b.py
    ≈2484-2488) even though `network` is in `_PHASE_B_SKIP_PER_REC`. **Confirm all
    five still resolve to a handler** (a source with no handler in a skip-per-rec
    service renders nothing, silently — the historical render-desync). If any of
    the five is unregistered, that is a CRITICAL regression.
15. **Counted == rendered:** `total_recommendations = len(all_recs)` (includes
    advisory) but `total_monthly_savings` sums only `Counted=True`. Confirm the
    per-tab headline shows the counted/advisory split, that
    `_render_network_enhanced_checks` renders advisory cards (visible but $0), and
    that the per-tab total equals the sum of the COUNTED rendered findings.
    Reconcile the executive-summary headline (`_calculate_service_savings` +
    reconciliation footnote) against the per-service totals. Confirm the renderer's
    displayed `EstimatedSavings` string does not desync from the parsed counted
    number.
16. Confirm no finding is counted in `total_recommendations`/`total_monthly_savings`
    but dropped from the table (or vice-versa), across all five domains and all
    `CheckCategory` groups.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to network:
    `.venv/bin/python cli.py <region> --scan-only network`
    then `.venv/bin/python tools/scan_doctor.py <json> --service network`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source/tab. Reconcile the
    headline against the per-source sum. Caveats: try a second region if the first
    is sparse; some accounts have only Classic LBs or only Gateway endpoints
    (exercise those branches); NAT/LB CloudWatch metrics may be empty (exercise the
    `$0 advisory` path). Use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC`.
18. For any duplication claim, prove it: show the same ALB / endpoint / EIP / ASG /
    instance in two checks or two tabs (canonical examples: NET-01 single-service +
    shared-ALB; an ASG in both the EC2 tab and the Network `auto_scaling_groups`
    block). For any accuracy claim, show the AWS Pricing API value (EIP / NAT /
    VPC-endpoint / ALB / NLB base rate) next to the scanner's constant.

### Deliverable
- The complete check list (Phase 1.2), per sub-shim, with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add a `tests/test_network_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  pure helpers directly (`parse_dollar_savings` boundaries, `_derive_severity`,
  `_safe_collect` error classification, the NET-01/02/03 dedup sets) and drive
  `NetworkModule.scan` with a `SimpleNamespace` ctx + monkeypatched sub-shims +
  fake boto3 clients/paginators. Cover every fix: ALB/VPC-endpoint/EIP dedup,
  ASG `record_aws_error` classification, fallback region scaling, hourly-SKU VPC
  endpoint pricing, fast-mode skip, render wiring, counted==rendered, advisory `$0`
  gating.
- For any heuristic that assumes throughput with no usage evidence (NAT cross-AZ
  GB, LB LCU), replace it with a real CloudWatch read or keep it a $0 advisory —
  respect `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for network first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `network.py` row in `services/adapters/CLAUDE.md` to match reality
  (e.g. drop the `get_clb_monthly_price()` claim; note the ASG block is advisory).
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Per-unit **rate string** (`$0.01/GB`, `$0.045/GB`, `$/hour`) counted as a monthly
  total — must be rejected by `parse_dollar_savings` → $0 advisory.
- Hardcoded constant embedded in a savings string (fallback 3.65 / NAT / 7.30 /
  ALB) not validated against the Pricing API or not region-scaled on the
  `pricing_engine=None` fallback path.
- **NLB/GWLB priced as ALB** / **Classic LB has no rate** — wrong or missing SKU
  (ALB-as-Classic was fixed in 90723e3; confirm it holds and NLB/GWLB use own SKUs).
- **Per-AZ resource billed once** (VPC interface endpoint, NAT) when it bills per AZ.
- **Public IPv4 billing**: in-use/auto-assigned public IPv4 IS billed ($3.65/mo) —
  removal savings are legit; don't flag as a false positive, but don't double-count
  the free per-ENI IP on a running instance.
- Same ASG / member instance counted in both the EC2 tab and the Network tab
  (cross-adapter overlap via the shared `get_auto_scaling_checks`).
- Sub-shim failure swallowed with `logger` only — classify `AccessDenied`/
  `OptInRequired` → `ctx.permission_issue`, other → `ctx.warn`.
- Metric-gated `$0` nudge (NAT data-processing, LB LCU, CLB "10-20%") counted
  instead of advisory.
- Adapter source name with no matching `PHASE_B_HANDLERS` key in a
  `_PHASE_B_SKIP_PER_REC` service → rendered as nothing.
- Render-time category drop desyncing the headline from the visible cards.
- Coverage gated to one LB client (`elbv2` only, missing Classic `elb`), a
  scaled-to-zero ASG flagged for savings, or a free Gateway endpoint flagged like a
  paid Interface endpoint.
- CloudWatch reads not `fast_mode`-gated and failures not recorded on `ctx`.

#### Network-specific items (confirmed in the 2026-06-27 gap-audit; verify each still reproduces)
- **NET-01 (HIGH):** ALB consolidation double-counted —
  `services/load_balancer.py:158-180` (`single_service_albs`, full `alb_monthly`
  per single-listener ALB) AND `204-228` (`shared_alb_opportunity`,
  `(standalone-2)×alb_monthly`) score the SAME ALBs; neither metric-gated.
  Demote `single_service_albs` to $0 advisory and keep consolidation advisory, OR
  count at most `(standalone-2)` ALBs once.
- **NET-02 (MEDIUM):** interface VPC endpoint counted twice when a nonprod
  endpoint is also a 3rd+ duplicate — `services/vpc_endpoints.py:111-125` +
  `132-150`. Make the two checks mutually exclusive via a `VpcEndpointId` `covered`
  set.
- **NET-03 (LOW):** stopped instance with multiple EIPs counted in both
  `eips_on_stopped_instances` and `multiple_eips_per_instance` —
  `services/elastic_ip.py:65-82` + `84-96`. Exclude already-emitted instance ids.
- **NET-04 (LOW):** ASG sub-shim bypasses `record_aws_error` (uses `ctx.warn`
  directly) — `services/ec2.py:911-913, 925-929` — so permission errors are
  misclassified; route through `record_aws_error` like the other four sub-shims.
- **NET-05 (LOW):** declared `unused_interface_endpoints` / `no_traffic_endpoints`
  (`services/vpc_endpoints.py:36-42`) and `zero_traffic_albs`
  (`services/load_balancer.py:73-83`) are never populated; implement a CW-gated
  idle check or remove the dead categories and the "Unused VPC endpoints"
  description.
- **NET-06 (LOW):** missing S3/DynamoDB gateway-endpoint advisory emitted by two
  sub-shims for the same VPC (`services/nat_gateway.py:125-148` vs
  `services/vpc_endpoints.py:71-93`) — consolidate into one owner.
- **NET-07 (LOW):** `core/pricing_engine.py:1539-1548` `_fetch_vpc_endpoint_price`
  not constrained to the hourly dimension — switch to `_call_pricing_api_hourly`
  or guard `unit=='Hrs'` so it always selects the hourly endpoint SKU.

## PROMPT (end)
