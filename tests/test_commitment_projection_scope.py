"""AFS-3 — a region-scoped report carried an account-wide projection unlabelled.

Live evidence, afs-prod / af-south-1 / 2026-08-11 (account 370525687312). The
executive summary rendered:

    Projected commitment — up to $18,653.58/mo
    (EC2 Instance SP path + service RIs (...); requires purchase — not in the
    counted total)

in a report titled af-south-1, while **65% of the RI recommendations behind it
($11,912.98 of $18,283.73) were for eu-west-1**. The dominant term — the EC2
Instance SP card at $15,596.73 — carried ``region: None`` and families
``c5, c5a, c6a, c6i, m5, r5, r6i, r8i, t3, t3a``, plainly account-wide.

This is inherent to the source, not a computation error: Cost Explorer's
purchase-recommendation APIs are account-scoped and accept no region filter. The
figure is also correctly kept OUT of the counted headline. What was missing is
that the report never SAID so — it contained zero instances of "account-wide" or
any equivalent — so:

* the reader attributes the whole opportunity to the scanned region, and
* a second scan of another region returns the SAME account-wide recommendations.
  This operator had already run one (`afs-prod_eu-west-1_20260808.md`), so adding
  the two projections would double count.

The scan-region share is published alongside the total rather than a bare label,
because "account-wide" alone still leaves the reader unable to tell how much is
local. Only the RI cards carry a region; SP cards do not (an EC2 Instance SP is
region-locked per purchase, but CE reports the bundle account-wide), so the
breakdown is explicitly described as covering the RI portion.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.commitment_scenarios import projected_region_split


def _ri(service: str, region: str, savings: float) -> dict:
    return {"card_kind": "ri_type", "service": service, "region": region,
            "monthly_savings": savings}


def _sp(sp_type: str, savings: float) -> dict:
    return {"card_kind": "sp_commitment", "sp_type": sp_type, "monthly_savings": savings}


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #
def test_the_af_south_1_split_is_reported() -> None:
    cards = [_ri("EC2", "eu-west-1", 11_912.98), _ri("EC2", "af-south-1", 6_370.75)]
    local, total, off_regions = projected_region_split(cards, "af-south-1")
    assert local == pytest.approx(6_370.75, abs=0.01)
    assert total == pytest.approx(18_283.73, abs=0.01)
    assert off_regions == ("eu-west-1",)


def test_a_single_region_account_reports_no_off_region_work() -> None:
    cards = [_ri("EC2", "af-south-1", 100.0), _ri("RDS", "af-south-1", 50.0)]
    local, total, off_regions = projected_region_split(cards, "af-south-1")
    assert (local, total) == (150.0, 150.0)
    assert off_regions == ()


def test_sp_cards_are_excluded_from_the_split() -> None:
    """SP cards carry no region — CE reports the bundle account-wide — so folding
    them in would silently attribute them to the scan region."""
    cards = [_ri("EC2", "af-south-1", 100.0), _sp("EC2_INSTANCE_SP", 15_596.73)]
    local, total, _ = projected_region_split(cards, "af-south-1")
    assert (local, total) == (100.0, 100.0)


def test_a_card_with_no_region_is_not_credited_to_the_scan_region() -> None:
    cards = [_ri("EC2", "", 500.0), _ri("EC2", "af-south-1", 100.0)]
    local, total, off = projected_region_split(cards, "af-south-1")
    assert local == 100.0 and total == 600.0
    assert off == ()          # unknown is not another region, just not local


def test_off_regions_are_sorted_and_deduped() -> None:
    cards = [_ri("EC2", "us-east-1", 1.0), _ri("RDS", "eu-west-1", 1.0),
             _ri("EC2", "eu-west-1", 1.0)]
    _l, _t, off = projected_region_split(cards, "af-south-1")
    assert off == ("eu-west-1", "us-east-1")


# --------------------------------------------------------------------------- #
# The rendered disclosure
# --------------------------------------------------------------------------- #
def _summary(**over) -> dict:
    base = {
        "total_services_scanned": 14, "total_recommendations": 149,
        "total_advisory_recommendations": 93, "total_monthly_savings": 6818.16,
        "projected_commitment_monthly_savings": 18653.58,
        "projected_commitment_basis": "EC2 Instance SP path + service RIs",
    }
    base.update(over)
    return base


def _render(summary: dict) -> str:
    from html_report_generator import HTMLReportGenerator

    gen = HTMLReportGenerator({
        "summary": summary, "services": {},
        "account_info": {"account_id": "370525687312", "region": "af-south-1"},
    })
    return gen._get_summary()


def test_the_projection_is_labelled_account_wide() -> None:
    """Always labelled: CE's purchase APIs are account-scoped regardless of what
    a particular account's estate looks like."""
    out = _render(_summary())
    assert "account-wide" in out.lower()


def test_the_local_share_is_shown_when_work_sits_off_region() -> None:
    out = _render(_summary(projected_commitment_scan_region_share=6370.75,
                           projected_commitment_offregion=["eu-west-1"]))
    assert "6,370.75" in out
    assert "eu-west-1" in out


def test_no_off_region_note_when_everything_is_local() -> None:
    out = _render(_summary(projected_commitment_scan_region_share=18653.58,
                           projected_commitment_offregion=[]))
    assert "eu-west-1" not in out
    # Still labelled account-wide — the SOURCE is account-scoped either way.
    assert "account-wide" in out.lower()


def test_an_older_summary_without_the_new_keys_still_renders() -> None:
    out = _render(_summary())
    assert "18,653.58" in out


def test_no_projection_means_no_note_at_all() -> None:
    out = _render(_summary(projected_commitment_monthly_savings=0.0))
    assert "Projected commitment" not in out


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
