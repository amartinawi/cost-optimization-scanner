# CLAUDE.md — AWS Cost Optimization Scanner

## Project Context

AWS cost optimization scanner with modular ServiceModule architecture. Scans 37 AWS services, generates HTML reports with recommendations and savings estimates.

## Quick Reference

| What | Where |
|------|-------|
| Architecture | `ARCHITECTURE.md` (full), `docs/ROADMAP.md` (future) |
| Agent execution policy | `AGENTS.md` (canonical — DO NOT duplicate here) |
| Contributing guide | `CONTRIBUTING.md` |
| Changelog | `CHANGELOG.md` |
| Service audit results | `docs/audits/SUMMARY.md` |
| Live pricing plan | `docs/LIVE_PRICING_PLAN.md` |

## Key Architecture

- **Protocol**: `ServiceModule` in `core/contracts.py` — all adapters implement this
- **Registration**: `services/__init__.py` `ALL_MODULES` list — append-only
- **Adapters**: `services/adapters/*.py` — 37 adapters, each wraps one AWS service
- **Legacy shims**: `services/*.py` — thin wrappers, DO NOT modify (use adapters/)
- **Pricing**: `core/pricing_engine.py` (`PricingEngine`) — live AWS pricing API
- **Scan flow**: `cost_optimizer.py` → `ScanOrchestrator` → adapters → `ResultBuilder` → HTML report

## Constraints

- Python 3.8+, boto3, zero external API dependencies
- All pricing via `PricingEngine` or `ScanContext.pricing_multiplier` — no hardcoded rates
- Google-style docstrings, ATX headers, no emoji
- Regression gate: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`

## Per-Directory Notes

- `core/CLAUDE.md` — core module specifics
- `services/adapters/CLAUDE.md` — adapter development guide
