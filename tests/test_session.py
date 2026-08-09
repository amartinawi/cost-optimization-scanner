"""AwsSessionFactory — STS regionality (af-south-1 opt-in region fix).

Live-diagnosed 2026-08-09 (afs-prod, af-south-1): pinning GetCallerIdentity
to the SCAN region routes the profile's AssumeRole through the opt-in
region's STS, which rejects v1/SSO source tokens (InvalidClientTokenId) and
kills the scan at startup. GetCallerIdentity has no regional semantics —
resolve it via a default-partition region, which also warms the role
credentials for every later opt-in-region client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.session import AwsSessionFactory


def test_account_id_sts_pinned_to_default_region_not_scan_region():
    factory = AwsSessionFactory(region="af-south-1", profile="afs-prod")
    fake_session = MagicMock()
    fake_session.client.return_value.get_caller_identity.return_value = {"Account": "370525687312"}
    with patch.object(AwsSessionFactory, "session", return_value=fake_session):
        assert factory.account_id() == "370525687312"
    _, kwargs = fake_session.client.call_args
    assert kwargs["region_name"] == "us-east-1"      # never the opt-in scan region
