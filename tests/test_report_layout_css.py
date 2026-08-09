"""Layout/print CSS contract pins (M360 live audit, 2026-08-09).

Two rendering bugs regressed silently because no test pinned the CSS:
- some tabs rendered WIDE (overflowing tables/tokens widened the page while
  other tabs stayed centered);
- Export-as-PDF produced mostly blank pages (page-break-inside: avoid on
  whole multi-page panels forces the print engine to push/clip them).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_report_generator import HTMLReportGenerator


def _css() -> str:
    gen = HTMLReportGenerator.__new__(HTMLReportGenerator)
    return gen._get_css()


def test_panels_contain_wide_content():
    css = _css()
    base = re.search(r"\.tab-content \{[^}]*\}", css).group(0)
    assert "overflow-x: auto" in base            # wide content scrolls inside the panel
    assert "overflow-wrap: anywhere" in css      # ARNs/bucket names break, never widen


def test_print_panels_flow_across_pages():
    css = _css()
    print_block = css[css.index("@media print"):]
    tab_rule = re.search(r"\.tab-content \{[^}]*\}", print_block).group(0)
    assert "display: block !important" in tab_rule
    assert "page-break-inside" not in tab_rule   # blank-pages bug #1 (avoid on panels)
    # Blank-pages bug #2: overflow-x:auto computes overflow-y:auto, and Chrome
    # clips scroll containers in print instead of paginating — the reset must
    # cover BOTH axes, and the fadeIn animation must not run at print time.
    assert "overflow: visible !important" in tab_rule
    assert "animation: none !important" in tab_rule
    assert "page-break-before: always" in print_block
