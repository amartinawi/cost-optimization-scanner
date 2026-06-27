# Network Adapter Cost-Audit Prompt

A deep, network-specific audit brief in the same structure as the Lambda /
RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* network code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`network`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **EC2** adapter
(`services/adapters/ec2.py`) as the canonical model for cross-source / ASG-member
dedup and the `$0`-placeholder→warning pattern, and the recently-audited
**Lambda** adapter (`services/adapters/lambda_svc.py`, commit history) as the
worked example for the `mark_zero_savings_advisory` pattern, rate-string
rejection, and the test style I expect.

### NOTE on structure (network is NOT shaped like EC2/RDS/Lambda)
- The network adapter is a **COMPOSITE** of five sub-shims, aggregated in
  `services/adapters/network.py` → `NetworkModule.scan`:
  - `get_elastic_ip_checks`  → `services/elastic_ip.py`
  - `get_nat_gateway_checks` → `services/nat_gateway.py`
  - `get_vpc_endpoints_checks` → `services/vpc_endpoints.py`
  - `get_load_balancer_checks` → `services/load_balancer.py`
  - `get_auto_scaling_checks` → **`services/ec2.py`** (imported across domains —
    this is the cross-adapter overlap hot-spot, see Phase 3)
- There is **NO** `services/network.py` legacy shim. The five sub-shims ARE the
  helpers; treat each as a mini-adapter.
- The adapter emits **five per-domain SourceBlocks** (`elastic_ips`,
  `nat_gateways`, `vpc_endpoints`, `load_balancers`, `auto_scaling_groups`) —
  NOT a single `enhanced_checks` block. Remember this for Phase 6.
- Network consumes **neither Cost Optimization Hub nor Compute Optimizer**. It is
  not in `scan_orchestrator._prefetch_advisor_data`'s `_HUB_SERVICES` / `type_map`,
  and pulls no CO helper. All savings are **local heuristics priced via
  `PricingEngine` (region-correct) with a hardcoded fallback constant per domain**.
- Savings are carried as a human **string** in `EstimatedSavings` and converted
  to a number by `services/_savings.py:parse_dollar_savings`, which counts only a
  bare/`/month` dollar figure and **rejects per-unit rate strings** (`$0.01/GB`,
  `$0.045/GB`). `mark_zero_savings_advisory` then flags any rec that parses to
  `$0` as `Counted=False` (advisory). The counted total sums only `Counted=True`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `network.py` row (listed under
    Live Pricing as `get_eip_monthly()`, `get_nat_hourly()`, … "5 methods").
    **Reconcile the doc against reality:** `core/pricing_engine.py` actually
    exposes `get_eip_monthly_price`, `get_nat_gateway_monthly_price`,
    `get_vpc_endpoint_monthly_price`, `get_alb_monthly_price` — note any method
    named in the doc that does not exist, and any priced domain (Classic LB, NAT
    data-processing $/GB, public IPv4) that has **no** PricingEngine method and is
    therefore a hardcoded string constant.
0b. Confirm module identity in `services/adapters/network.py`: `key="network"`,
    `cli_aliases=("network",)`, `display_name="Network & Infrastructure"`,
    `required_clients()` (`ec2`, `elbv2`, `autoscaling`, `elb`). Note that it does
    NOT set `requires_cloudwatch` / `reads_fast_mode` even though sub-shims read
    CloudWatch (NAT `BytesOutToDestination`, LB LCU/NLCU) — flag if so.
0c. Network is one of the few adapters with **no AWS advisory source** (no CoH /
    CO). So a "missing CoH/CO source" finding is NOT fair game here — savings are
    expected to be locally derived. Focus instead on pricing accuracy, the
    rate-string boundary, the ASG cross-adapter overlap, and render wiring.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the network path: `services/adapters/network.py` and all
   five sub-shims (`services/elastic_ip.py`, `services/nat_gateway.py`,
   `services/vpc_endpoints.py`, `services/load_balancer.py`, and
   `get_auto_scaling_checks` in `services/ec2.py`); the helpers they import
   (`services/_savings.py` — `parse_dollar_savings`, `mark_zero_savings_advisory`;
   `services/adapters/network.py` — `_derive_severity`, `_annotate_severity`,
   `_safe_collect`, `_sum_savings`); `core/contracts.py`,
   `core/pricing_engine.py` (the four network methods, lines ~824–875),
   `core/scan_orchestrator.py`, `core/result_builder.py`, and the reporter
   (`reporter_phase_b.py:_render_network_enhanced_checks` ~line 1132,
   `html_report_generator.py`).
2. List **every** cost check across all five sub-shims, and for each give:
   trigger condition, the data source (EC2/ELB describe-API, CloudWatch metric,
   or pure config heuristic), the exact `EstimatedSavings` string template, the
   constant/rate it embeds, and whether that string parses to a **counted** dollar
   or a **$0 advisory** (rate string / `$0.00 - requires CW …` / percentage).
   Map each check to its emitting SourceBlock. The known check inventory to
   confirm: unattached EIP, EIP on stopped instance, multiple EIPs, in-use public
   IP removal; unused/redundant/low-throughput/cross-AZ NAT; unused/duplicate VPC
   endpoints; idle/low-traffic/Classic LB + ALB consolidation/Ingress-Groups; ASG
   config/instance-type.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** savings figure, re-derive it from the live AWS Pricing API
   and confirm it matches. Validate EACH domain constant separately:
   - **EIP**: `get_eip_monthly_price` vs the `3.65` fallback (`services/elastic_ip.py`).
     In-use/auto-assigned public IPv4 has been billed at **$0.005/hr ≈ $3.65/mo
     since 2024** — confirm the per-IP figure and that the SKU is correct
     (`AmazonVPC` / `PublicIPv4:InUseAddress`). An unattached-EIP saving and an
     in-use-public-IP-removal saving are both legitimate; do not flag them as
     false positives, but confirm an EIP attached to a *running* instance (the one
     free per-ENI IP) is not double-charged.
   - **NAT**: `get_nat_gateway_monthly_price` vs the `32.0` fallback. Validate the
     base hourly ($0.045/hr ≈ $32.85/mo us-east-1) AND scrutinize the
     hardcoded **`+ 0.85`** addend in the "base + data processing fees" string and
     the bare `$0.045/GB` cross-AZ rate — confirm both against the API and confirm
     any `$/GB` figure is a **rate**, not a counted total.
   - **VPC endpoint**: `get_vpc_endpoint_monthly_price` vs the `7.30` fallback
     (Interface endpoint $0.01/hr/AZ ≈ $7.30/mo per AZ) — confirm whether the
     per-AZ multiplication is handled (an endpoint spanning N AZs bills N×).
   - **Load balancer**: `get_alb_monthly_price` vs the `16.20` fallback. Note
     `nlb_monthly = alb_monthly` (NLB priced AS ALB) — confirm the NLB base SKU
     and flag the approximation. Confirm there is **no Classic LB rate** (CLB base
     is ~$0.025/hr + $0.008/LCU-hr) — the CLB migration check emits a percentage
     ("10-20% + better features"), so confirm it parses to $0 advisory and is not
     counted.
   - **Region scaling**: PricingEngine methods are region-correct; the **fallback
     constants (3.65 / 32.0 / 7.30 / 16.20) are us-east-1** and are embedded
     directly into the savings string. Confirm whether `ctx.pricing_multiplier`
     is applied on the fallback path (it likely is NOT) — a non-us-east-1 scan
     with `pricing_engine=None` would emit us-east-1 dollars. Flag any missing
     region scaling on the fallback path.
   - **Reduction factors/heuristics** (ALB consolidation "to 2 ALBs", "10-20%")
     must be calibrated and labelled, not arbitrary; consolidation savings of
     `(count − N) × rate` must use a defensible floor N.
4. Confirm the savings basis is defensible from the report alone: each counted
   finding should make the rate/region/metric legible. A NAT or LB saving that
   depends on throughput (cross-AZ GB, LCU) but is emitted as a flat base-rate
   total with no metric is a finding — prefer a CloudWatch-backed number or a $0
   advisory. Record a structured **AuditBasis** (rate / region / metric-window /
   formula) on each counted finding, as the Lambda/RDS audits did.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter / intra-domain:** can one resource match multiple checks and
   stack savings beyond its real cost? (e.g. a NAT counted as both "redundant"
   AND "cross-AZ consolidation"; an ALB counted in both "consolidation" and
   "Ingress Groups"; an EIP in both "unattached" and "multiple EIPs".) Check each
   sub-shim for SUBSET redundancy (one check's population ⊆ another's) — fix by
   removal, not double-count.
6. **Cross-adapter (the big one):** `get_auto_scaling_checks` lives in
   `services/ec2.py` and is consumed by the **network** adapter, while the EC2
   adapter independently surfaces ASG via `get_asg_compute_optimizer_recommendations`
   and adds ASG members to its dedup `covered` set. Determine whether the SAME ASG
   (or its member instances) can be counted in BOTH the EC2 tab and the Network
   tab. Decide the correct single owner (ASG rightsizing is an EC2/compute lever;
   Network's ASG block may be redundant) and confirm `core/result_builder.py`
   doesn't blindly sum across tabs.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls network resources into a synthetic tab
   (Snapshots/AMIs-style), and that an EIP/IP attached to an EC2 instance is not
   also counted by the EC2 adapter.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Are checks gated to hardcoded types/states that silently exclude valid
   resources? Confirm full pagination of `describe_addresses`,
   `describe_nat_gateways`, `describe_vpc_endpoints`,
   `describe_load_balancers` (elbv2 AND classic `elb`),
   `describe_auto_scaling_groups`. Confirm LB coverage includes **all three
   types** (ALB / NLB / GWLB / Classic) and both `elbv2` and `elb` clients.
9. Are whole classes skipped? IPv6-only / dualstack endpoints; Gateway endpoints
   (free — should NOT be flagged like Interface endpoints); NAT in different VPCs;
   ASGs with 0 desired capacity (mirror the EKS 0-node fix — no savings from a
   scaled-to-zero group). Confirm each skip is intentional and documented.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger.warning`-only, and
    `return []` fallback. Specifically:
    - `services/adapters/network.py:_safe_collect` swallows ANY sub-shim exception
      with `logger.warning` only — a failed sub-domain (e.g. an ELB permission
      gap) vanishes from the report with no `ctx.warn` / `ctx.permission_issue`.
      Classify: `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`,
      other → `ctx.warn`. (This is the canonical prior-audit silent-failure class.)
    - Each sub-shim's own inner `except Exception as e:` (elastic_ip ~123/127,
      nat_gateway ~104/166, vpc_endpoints ~125, load_balancer ~49/90/178/186/238) —
      confirm none swallow describe-API or CloudWatch failures without recording
      them on `ctx`.
11. Does a pricing miss fall back to `0.0`/blank and still emit a finding? A
    finding with `$0` counted savings is a bug — it must be advisory or skipped.
    Verify `mark_zero_savings_advisory` covers EVERY metric-gated / rate-string /
    percentage path (NAT data-processing, LB LCU, CLB migration "10-20%",
    VPC-endpoint `$/GB`) — none should be counted.
12. **CloudWatch gating / fast-mode:** sub-shims read CloudWatch
    (NAT `BytesOutToDestination`, LB LCU/NLCU consumption). The adapter declares
    neither `requires_cloudwatch` nor `reads_fast_mode`. Confirm whether these
    reads are skipped under `ctx.fast_mode` and whether throttling/permission
    failures are recorded — mirror the Lambda fast-mode fix.
13. Are opt-in / "enable X" / "requires metric" nudges emitted as `$0`
    recommendations that inflate the count? They must be advisory (`Counted=False`),
    rendered but excluded from counts — confirm the `$0.00 - requires CW …` NAT/LB
    strings land as advisory, not counted.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Source-name vs handler mismatch (verify carefully):** the adapter emits five
    sources (`elastic_ips`, `nat_gateways`, `vpc_endpoints`, `load_balancers`,
    `auto_scaling_groups`), but `PHASE_B_HANDLERS` registers only
    `("network","enhanced_checks") → _render_network_enhanced_checks`, and
    `network` is in `_PHASE_B_SKIP_PER_REC` (no per-rec fallback). Trace
    `html_report_generator._get_detailed_recommendations` and confirm exactly how
    the five per-domain sources reach the renderer — a source with NO registered
    handler in a skip-per-rec service renders **nothing, silently**. If the five
    sources are remapped/merged into `enhanced_checks` somewhere, document where;
    if not, that is a CRITICAL render-desync.
15. **Counted == rendered:** `total_recommendations = len(all_recs)` (includes
    advisory) but `total_monthly_savings` sums only `Counted=True`. Confirm the
    per-tab headline shows the counted/advisory split and that
    `_render_network_enhanced_checks` renders advisory cards (so they are visible
    but contribute $0). Verify the per-tab total equals the sum of the COUNTED
    rendered findings, and reconcile the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings` +
    reconciliation footnote) against the per-service totals. Note that the renderer
    shows `resources[0]['EstimatedSavings']` (the raw string) per category — check
    it does not desync from the parsed counted number.
16. Confirm no finding is counted in `total_recommendations`/
    `total_monthly_savings` but dropped from the table (or vice-versa), across all
    five domains and all `CheckCategory` groups.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to network:
    `python3 cli.py <region> --scan-only network`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service network`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source/tab. Reconcile the
    headline against the per-source sum. Caveats: try a second region if the first
    is sparse; some accounts have only Classic LBs or only Gateway endpoints
    (exercise those branches); NAT/LB CloudWatch metrics may be empty (exercise the
    `$0 advisory` path). Use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC`.
18. For any duplication claim, prove it: show the same ASG / instance / IP in two
    sources or two tabs (the canonical example: an ASG in both the EC2 tab and the
    Network `auto_scaling_groups` block). For any accuracy claim, show the AWS
    Pricing API value (EIP / NAT / VPC-endpoint / ALB / NLB base rate) next to the
    scanner's constant.

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
  `_safe_collect` error classification, ASG dedup) and drive `NetworkModule.scan`
  with a `SimpleNamespace` ctx + monkeypatched sub-shims + fake boto3
  clients/paginators for any describe/CloudWatch path. Cover every fix:
  silent-failure classification, fallback region scaling, NLB/CLB pricing,
  ASG cross-adapter dedup, fast-mode skip, render wiring, counted==rendered,
  advisory `$0` gating.
- For any heuristic that assumes throughput with no usage evidence (NAT cross-AZ
  GB, LB LCU), replace it with a real CloudWatch read or keep it a $0 advisory —
  respect `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for network first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / pricing / dedup bug in a sibling adapter
  out of scope, note it as a follow-up (don't fix unprompted).
- Update the `network.py` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Per-unit **rate string** (`$0.01/GB`, `$0.045/GB`, `$/hour`) counted as a monthly
  total — must be rejected by `parse_dollar_savings` → $0 advisory.
- Hardcoded constant embedded in a savings string (NAT `+0.85`, fallback 3.65 /
  32.0 / 7.30 / 16.20) not validated against the Pricing API or not region-scaled.
- **NLB priced as ALB** / **Classic LB has no rate** — wrong or missing SKU.
- **Per-AZ resource billed once** (VPC interface endpoint, NAT) when it bills per AZ.
- **Public IPv4 billing**: in-use/auto-assigned public IPv4 IS billed ($3.65/mo) —
  removal savings are legit; do NOT flag as a false positive, but don't double-count
  the free per-ENI IP on a running instance.
- Same ASG / member instance counted in both the EC2 tab and the Network tab
  (cross-adapter overlap via the shared `get_auto_scaling_checks`).
- Sub-shim failure swallowed by `_safe_collect`/inner `except` with `logger`
  only — classify `AccessDenied`/`OptInRequired` → `ctx.permission_issue`,
  other → `ctx.warn`.
- Metric-gated `$0` nudge (NAT data-processing, LB LCU, CLB "10-20%") counted
  instead of advisory.
- Adapter source name (`elastic_ips`/`nat_gateways`/…) with no matching
  `PHASE_B_HANDLERS` key in a `_PHASE_B_SKIP_PER_REC` service → rendered as nothing.
- Render-time category drop desyncing the headline from the visible cards.
- Coverage gated to one LB client (`elbv2` only, missing Classic `elb`), or a
  scaled-to-zero ASG flagged for savings, or a free Gateway endpoint flagged like
  a paid Interface endpoint.
- CloudWatch reads not `fast_mode`-gated and failures not recorded on `ctx`.

## PROMPT (end)
