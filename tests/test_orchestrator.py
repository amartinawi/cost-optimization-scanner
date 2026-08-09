"""Unit tests for ScanOrchestrator.safe_scan error isolation."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.client_registry import ClientRegistry
from core.contracts import ServiceFindings
from core.scan_context import ScanContext


def _make_ctx() -> ScanContext:
    """Build a minimal ScanContext with mocked client registry."""
    registry = MagicMock(spec=ClientRegistry)
    return ScanContext(
        region="us-east-1",
        account_id="123456789012",
        profile=None,
        fast_mode=False,
        clients=registry,
    )


def _broken_module() -> MagicMock:
    """Return a mock module whose scan() raises RuntimeError."""
    m = MagicMock()
    m.key = "broken"
    m.display_name = "Broken"
    m.scan.side_effect = RuntimeError("boom")
    return m


def test_safe_scan_catches_exception():
    """Verify safe_scan returns empty findings and logs a warning when a module raises."""
    ctx = _make_ctx()
    from core.scan_orchestrator import safe_scan

    findings = safe_scan(_broken_module(), ctx)

    assert findings.service_name == "Broken"
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert len(ctx._warnings) == 1
    assert "boom" in ctx._warnings[0].message


def _failing_pricing_engine():
    """A PricingEngine whose Pricing API always errors, forcing fallbacks."""
    from core.pricing_engine import PricingEngine

    client = MagicMock()
    client.get_products.side_effect = RuntimeError("pricing api down")
    return PricingEngine("us-east-1", client, fallback_multiplier=1.0)


def test_drain_pricing_warnings_surfaces_real_fallback():
    """A live-pricing failure becomes a single 'pricing' scan warning.

    Rates are cached by SKU, so a repeated lookup of the same rate falls back
    only once — the warning carries no count suffix.
    """
    from core.scan_orchestrator import ScanOrchestrator

    ctx = _make_ctx()
    engine = _failing_pricing_engine()
    ctx.pricing_engine = engine
    engine.get_eip_monthly_price()
    engine.get_eip_monthly_price()  # cache hit — does not re-trigger the fallback

    ScanOrchestrator(ctx, [])._drain_pricing_warnings()

    pricing_warns = [w for w in ctx._warnings if w.service == "pricing"]
    assert len(pricing_warns) == 1
    assert "fallback rate" in pricing_warns[0].message
    assert "EIP" in pricing_warns[0].message
    assert "(x" not in pricing_warns[0].message  # single occurrence, no count suffix


def test_drain_pricing_warnings_dedups_with_count_suffix():
    """Duplicate fallback messages collapse to one warning carrying a count."""
    from core.scan_orchestrator import ScanOrchestrator

    ctx = _make_ctx()
    engine = _failing_pricing_engine()
    ctx.pricing_engine = engine
    # Seed duplicates directly (a method that does not cache before falling back
    # could append the same message more than once across resources).
    engine.warnings.extend(["rate X unavailable", "rate X unavailable", "rate Y unavailable"])

    ScanOrchestrator(ctx, [])._drain_pricing_warnings()

    pricing_warns = sorted(w.message for w in ctx._warnings if w.service == "pricing")
    assert len(pricing_warns) == 2
    assert any("rate X unavailable (x2)" in m for m in pricing_warns)
    assert any("rate Y unavailable" in m and "(x" not in m for m in pricing_warns)


def test_drain_pricing_warnings_includes_region_siblings():
    """A fallback on a cross-region sibling engine is also surfaced."""
    from core.scan_orchestrator import ScanOrchestrator

    ctx = _make_ctx()
    engine = _failing_pricing_engine()
    ctx.pricing_engine = engine
    # Force a fallback on a different region's sibling engine.
    engine.for_region("eu-west-1").get_eip_monthly_price()

    ScanOrchestrator(ctx, [])._drain_pricing_warnings()

    pricing_warns = [w for w in ctx._warnings if w.service == "pricing"]
    assert len(pricing_warns) == 1
    assert "fallback rate" in pricing_warns[0].message


def test_drain_pricing_warnings_noop_when_no_engine():
    """No pricing engine -> no pricing warnings, no error."""
    from core.scan_orchestrator import ScanOrchestrator

    ctx = _make_ctx()
    ctx.pricing_engine = None

    ScanOrchestrator(ctx, [])._drain_pricing_warnings()

    assert [w for w in ctx._warnings if w.service == "pricing"] == []


# --------------------------------------------------------------------------- #
# CoH type_map hygiene — every key must be a REAL ResourceType (sweep rank 7)
# --------------------------------------------------------------------------- #
def test_coh_type_map_keys_are_real_resource_types():
    """RedshiftCluster / OpenSearchDomain / EksCluster / RdsDbCluster / EcsTask /
    EcsCluster are not in AWS's ResourceType enum, so those keys could never
    match a payload and the buckets they fed were permanently empty — which is
    why the redshift tab (CoH-only counted source) is structurally $0."""
    import gzip
    import json
    import re
    from pathlib import Path

    import botocore

    model = (
        Path(botocore.__file__).parent
        / "data" / "cost-optimization-hub" / "2022-07-26" / "service-2.json.gz"
    )
    if not model.exists():  # botocore layout changed — skip rather than false-fail
        import pytest

        pytest.skip("cost-optimization-hub model not present in this botocore build")
    enum = set(json.load(gzip.open(model))["shapes"]["ResourceType"]["enum"])

    src = Path("core/scan_orchestrator.py").read_text()
    block = src.split("type_map = {", 1)[1].split("}", 1)[0]
    keys = set(re.findall(r'"([A-Za-z0-9]+)":', block))

    # Case-insensitive: real payloads spell some types with a lowercase c2
    # (Ec2ReservedInstances), and the map deliberately carries both spellings.
    lowered = {e.lower() for e in enum}
    # Deliberate defensive alias: the pre-rename Elasticsearch spelling of
    # OpenSearchReservedInstances (paired in services/commitment_scenarios.py).
    # It routes to the SAME tab as the real key, so an unmatched alias costs
    # nothing — unlike a key that feeds an adapter's only counted bucket.
    known_aliases = {"EsReservedInstances"}
    unknown = {k for k in keys if k.lower() not in lowered} - known_aliases
    assert unknown == set(), f"type_map keys absent from AWS ResourceType enum: {sorted(unknown)}"


def test_coh_type_map_has_no_dead_cluster_keys():
    from pathlib import Path

    block = Path("core/scan_orchestrator.py").read_text().split("type_map = {", 1)[1].split("}", 1)[0]
    for dead in ("RedshiftCluster", "OpenSearchDomain", "EksCluster", "RdsDbCluster",
                 "EcsTask", "EcsCluster"):
        assert f'"{dead}":' not in block, f"{dead} is not a CoH ResourceType"
