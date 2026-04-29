"""AWS session factory with retry configuration.

Extracts boto3 session creation, STS account resolution, and
retry configuration from CostOptimizer.__init__ (lines 261-268).
"""

from __future__ import annotations

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]


class AwsSessionFactory:
    _retry_config: Config

    def __init__(self, region: str, profile: str | None = None) -> None:
        self._region = region
        self._profile = profile
        self._session: boto3.Session | None = None
        self._retry_config = Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
        )

    def session(self) -> boto3.Session:
        if self._session is None:
            self._session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return self._session

    def account_id(self) -> str:
        sts = self.session().client("sts", region_name=self._region, config=self._retry_config)
        return str(sts.get_caller_identity()["Account"])

    def retry_config(self) -> Config:
        return self._retry_config

    @property
    def region(self) -> str:
        return self._region

    @property
    def profile(self) -> str | None:
        return self._profile
