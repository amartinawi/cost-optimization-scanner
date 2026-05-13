# Changelog

All notable changes to the AWS Cost Optimization Scanner project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
