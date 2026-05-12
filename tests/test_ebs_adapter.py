"""Unit tests for the EBS adapter — gp2→gp3 savings + dedup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.adapters.ebs import _gp2_to_gp3_savings_per_gb


class _FakeEngine:
    """Minimal PricingEngine stand-in returning fixed gp2/gp3 rates."""

    def __init__(self, gp2: float, gp3: float) -> None:
        self._gp2 = gp2
        self._gp3 = gp3

    def get_ebs_monthly_price_per_gb(self, volume_type: str) -> float:
        return {"gp2": self._gp2, "gp3": self._gp3}.get(volume_type, 0.0)


class TestGp2ToGp3Delta:
    def test_live_path_returns_difference(self):
        ctx = SimpleNamespace(pricing_engine=_FakeEngine(gp2=0.10, gp3=0.08), pricing_multiplier=1.0)
        # $0.02/GB-Mo difference for us-east-1.
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx(0.02)

    def test_live_path_ignores_pricing_multiplier(self):
        """PricingEngine returns region-correct prices already — multiplier
        must not be re-applied (audit finding L2-EBS-001)."""
        ctx = SimpleNamespace(pricing_engine=_FakeEngine(gp2=0.121, gp3=0.0968), pricing_multiplier=1.10)
        # If the bug were still present we'd get 0.0242 × 1.10 = 0.02662.
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx(0.121 - 0.0968)

    def test_live_path_never_negative(self):
        ctx = SimpleNamespace(pricing_engine=_FakeEngine(gp2=0.05, gp3=0.10), pricing_multiplier=1.0)
        assert _gp2_to_gp3_savings_per_gb(ctx) == 0.0

    def test_fallback_path_applies_multiplier(self):
        ctx = SimpleNamespace(pricing_engine=None, pricing_multiplier=1.10)
        # Fallback formula: 0.10 × 0.20 × multiplier.
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx(0.10 * 0.20 * 1.10)
