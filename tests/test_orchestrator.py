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

    # CASE-SENSITIVE: type_map.get(rec_type) is an exact-match lookup, so a
    # key that differs only in case is dead weight that can never fire. The
    # earlier case-insensitive form passed on exactly those keys, which is how
    # the dead uppercase spellings below survived unnoticed. Every deliberate
    # alias must be listed here with a reason.
    known_aliases = {
        # Uppercase-EC2 spellings retained defensively. Real payloads use the
        # enum's Ec2* spelling (live-verified 2026-08-09), so these never fire
        # today; they route to the SAME tab as the real key, so an unmatched
        # alias costs nothing — unlike a key feeding an adapter's only counted
        # bucket.
        "EC2ReservedInstances",
        "EC2InstanceSavingsPlans",
    }
    unknown = {k for k in keys if k not in enum} - known_aliases
    assert unknown == set(), f"type_map keys absent from AWS ResourceType enum: {sorted(unknown)}"
    # Every alias must be a case-variant of a real type, not an invention.
    lowered = {e.lower() for e in enum}
    stale = {a for a in known_aliases if a.lower() not in lowered}
    assert stale == set(), f"alias allowlist entries match no real type: {sorted(stale)}"


def test_coh_type_map_has_no_dead_cluster_keys():
    from pathlib import Path

    block = Path("core/scan_orchestrator.py").read_text().split("type_map = {", 1)[1].split("}", 1)[0]
    for dead in ("RedshiftCluster", "OpenSearchDomain", "EksCluster", "RdsDbCluster",
                 "EcsTask", "EcsCluster"):
        assert f'"{dead}":' not in block, f"{dead} is not a CoH ResourceType"


def _type_map_block() -> str:
    from pathlib import Path

    return Path("core/scan_orchestrator.py").read_text().split("type_map = {", 1)[1].split("}", 1)[0]


def test_coh_storage_types_route_to_rds():
    """RdsDbInstanceStorage / AuroraDbClusterStorage are real enum types whose
    AWS-computed dollars were dropped for want of a bucket."""
    block = _type_map_block()
    assert '"RdsDbInstanceStorage": "rds"' in block
    assert '"AuroraDbClusterStorage": "rds"' in block


def test_coh_reserved_capacity_types_route_to_commitment_analysis():
    block = _type_map_block()
    assert '"DynamoDbReservedCapacity": "commitment_analysis"' in block
    assert '"MemoryDbReservedInstances": "commitment_analysis"' in block


def test_every_routed_bucket_exists_in_hub_services():
    """A route to a bucket _HUB_SERVICES never creates is silently dropped by
    the `bucket in splits` gate — the failure mode is invisible at runtime."""
    import re
    from pathlib import Path

    src = Path("core/scan_orchestrator.py").read_text()
    hub_block = src.split("_HUB_SERVICES = {", 1)[1].split("}", 1)[0]
    buckets = set(re.findall(r'"([a-z_]+)"', hub_block))
    routed = set(re.findall(r'"[A-Za-z0-9]+": "([a-z_]+)"', _type_map_block()))
    assert routed - buckets == set(), f"routed to nonexistent buckets: {sorted(routed - buckets)}"
