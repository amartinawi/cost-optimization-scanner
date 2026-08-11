"""LS-5 / LS-6 — a card that refutes itself, and a summary that contradicts it.

**LS-5** — S3 emitted *"Empty bucket older than 171 days - consider deletion.
$0.00/month - empty bucket incurs no storage cost."* The card states its own
saving is zero **because the resource is free**. There is no cost lever here at
all: an empty bucket bills nothing, so deleting it saves nothing. (Incomplete
multipart-upload parts DO bill and are invisible to `list_objects_v2` — but
those are the separate `multipart_uploads` check's job, and that check does
carry a dollar.) Deleted, along with the per-bucket `list_objects_v2` call that
existed only to feed it.

**LS-6** — with 0 counted recommendations and 52 advisory cards, the executive
summary rendered "No Cost Optimization Recommendations Found - Your AWS
resources appear to be well-optimized" directly above those 52 cards, and the
footer read "Report covers 4 AWS services with 0 optimization recommendations".
Both numbers were internally correct — they are the COUNTED count, which is the
right headline semantics — but a reader sees a contradiction, and "well
optimized" is an affirmative claim the scan did not make. The advisory count is
now carried in the summary and appears in both places.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from core.contracts import ServiceFindings, SourceBlock
from core.result_builder import ScanResultBuilder


# --------------------------------------------------------------------------- #
# LS-5
# --------------------------------------------------------------------------- #
class _S3:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def list_buckets(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {"Buckets": [{"Name": "b1", "CreationDate": datetime(2020, 1, 1, tzinfo=UTC)}]}

    def get_bucket_location(self, **_kw: Any) -> dict[str, Any]:
        return {"LocationConstraint": None}

    def list_objects_v2(self, **_kw: Any) -> dict[str, Any]:
        self.calls.append("list_objects_v2")
        return {"KeyCount": 0}

    def __getattr__(self, name: str) -> Any:
        def _stub(**_kw: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {}

        return _stub


def _ctx(s3: _S3) -> SimpleNamespace:
    ctx = SimpleNamespace(pricing_engine=None, pricing_multiplier=1.0, fast_mode=False,
                          region="us-east-1", warnings=[], permissions=[])
    ctx.warn = lambda msg, service="", **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service="", action=None, **k: ctx.permissions.append(msg)
    ctx.client = lambda name, region=None: s3
    # See test_ls9_static_website._ctx: _bucket_s3_client needs this, and
    # without it the whole bucket loop is swallowed and absence assertions pass
    # for the wrong reason.
    ctx.clients = SimpleNamespace(
        _factory=SimpleNamespace(
            session=lambda: SimpleNamespace(client=lambda *a, **k: s3)
        )
    )
    return ctx


def test_empty_bucket_card_and_its_api_call_are_gone() -> None:
    """An empty bucket bills nothing, so deleting it saves nothing — the card
    said so itself. Nothing to gate or quantify; there is no lever."""
    from services.s3 import get_enhanced_s3_checks

    calls: list[str] = []
    out = get_enhanced_s3_checks(_ctx(_S3(calls)), pricing_multiplier=1.0)

    # Proof the bucket loop actually ran (see the _ctx docstring): otherwise
    # these absence assertions pass because nothing executed.
    assert "list_multipart_uploads" in calls

    assert "unused_buckets" not in out
    assert not any(r.get("CheckCategory") == "Unused Resources"
                   for r in out.get("recommendations", []))
    assert "list_objects_v2" not in calls


def test_the_paying_sibling_check_survives() -> None:
    """Regression guard: incomplete multipart uploads DO bill and are invisible
    to list_objects_v2, so that check must not be collateral damage."""
    import services.s3 as s3_mod

    src = Path(s3_mod.__file__).read_text()
    assert "multipart_uploads" in src


# --------------------------------------------------------------------------- #
# LS-6 — the summary must publish the advisory count
# --------------------------------------------------------------------------- #
def _findings(counted: int, advisory: int, optimized: int = 0) -> ServiceFindings:
    recs: list[dict[str, Any]] = []
    recs += [{"resource_id": f"c{i}", "monthly_savings": 5.0} for i in range(counted)]
    recs += [{"resource_id": f"a{i}", "Counted": False} for i in range(advisory)]
    recs += [{"resource_id": f"o{i}", "finding": "OPTIMIZED"} for i in range(optimized)]
    return ServiceFindings(
        service_name="Test",
        total_recommendations=len(recs),
        total_monthly_savings=5.0 * counted,
        sources={"s": SourceBlock(count=len(recs), recommendations=tuple(recs))},
    )


def test_summary_carries_the_advisory_count() -> None:
    summary = ScanResultBuilder._summary({"a": _findings(counted=2, advisory=7)})
    assert summary["total_recommendations"] == 2            # counted, unchanged
    assert summary["total_advisory_recommendations"] == 7


def test_advisory_count_excludes_optimized_findings() -> None:
    """An OPTIMIZED finding renders no card, so it is neither counted nor an
    advisory — it must not pad the number that explains the cards on screen."""
    summary = ScanResultBuilder._summary({"a": _findings(counted=1, advisory=3, optimized=4)})
    assert summary["total_advisory_recommendations"] == 3


def test_advisory_count_is_counted_directly_not_subtracted() -> None:
    """Deriving it as rendered-minus-counted would go NEGATIVE on a rec that is
    both Counted and OPTIMIZED, printing '-2 advisory findings'."""
    weird = ServiceFindings(
        service_name="Weird",
        total_recommendations=2,
        total_monthly_savings=0.0,
        sources={"s": SourceBlock(count=2, recommendations=(
            {"resource_id": "x", "finding": "OPTIMIZED"},
            {"resource_id": "y", "finding": "OPTIMIZED"},
        ))},
    )
    assert ScanResultBuilder._summary({"w": weird})["total_advisory_recommendations"] == 0


def test_a_placeholder_source_contributes_no_advisories() -> None:
    """A source declaring a count with no materialised recs is trusted as
    counted (existing contract); it must not also be read as advisory."""
    placeholder = ServiceFindings(
        service_name="Placeholder",
        total_recommendations=3,
        total_monthly_savings=9.0,
        sources={"s": SourceBlock(count=3, recommendations=())},
    )
    summary = ScanResultBuilder._summary({"p": placeholder})
    assert summary["total_recommendations"] == 3
    assert summary["total_advisory_recommendations"] == 0


# --------------------------------------------------------------------------- #
# LS-6 — the rendered copy
# --------------------------------------------------------------------------- #
def _generator(counted: int, advisory: int, services: int = 4) -> Any:
    from html_report_generator import HTMLReportGenerator

    return HTMLReportGenerator({
        "summary": {
            "total_services_scanned": services,
            "total_recommendations": counted,
            "total_advisory_recommendations": advisory,
            "total_monthly_savings": 5.0 * counted,
        },
        "services": {},
        "account_info": {"account_id": "123456789012", "region": "us-east-1"},
    })


def test_exec_summary_does_not_claim_well_optimized_beside_advisories() -> None:
    """The live report said "well-optimized" directly above 52 advisory cards."""
    out = _generator(counted=0, advisory=52)._get_executive_summary_content()
    assert "well-optimized" not in out
    assert "52" in out


def test_exec_summary_keeps_the_clean_bill_when_there_is_nothing_at_all() -> None:
    out = _generator(counted=0, advisory=0)._get_executive_summary_content()
    assert "No Cost Optimization Recommendations Found" in out


def test_footer_reports_advisories_alongside_the_counted_total() -> None:
    footer = _generator(counted=0, advisory=52)._get_footer()
    assert "0 optimization recommendations" in footer
    assert "52" in footer


def test_footer_stays_quiet_when_there_are_no_advisories() -> None:
    footer = _generator(counted=3, advisory=0)._get_footer()
    assert "3 optimization recommendations" in footer
    assert "advisory" not in footer.lower()


def test_report_copy_survives_a_summary_without_the_new_key() -> None:
    """Old scan JSON re-rendered through a new binary must not crash."""
    from html_report_generator import HTMLReportGenerator

    gen = HTMLReportGenerator({
        "summary": {"total_services_scanned": 1, "total_recommendations": 0,
                    "total_monthly_savings": 0.0},
        "services": {},
        "account_info": {"account_id": "1", "region": "us-east-1"},
    })
    assert "No Cost Optimization Recommendations Found" in gen._get_executive_summary_content()
    assert gen._get_footer()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
