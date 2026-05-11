"""Caching boto3 client registry with global-service routing.

Mirrors the client initialisation in CostOptimizer.__init__ (lines 286-356).
Global services are always routed to us-east-1; ``trustedadvisor`` is
aliased to ``support``.
"""

from __future__ import annotations

from typing import Any

from botocore.client import BaseClient  # type: ignore[import-untyped]

from core.session import AwsSessionFactory


class ClientRegistry:
    """Caching boto3 client factory with global-service routing and aliases.

    Global services (e.g. route53, cloudfront) are automatically routed to
    us-east-1 when no explicit region is provided.
    """

    _GLOBAL_SERVICES: frozenset[str] = frozenset(
        {
            "route53",
            "cloudfront",
            "iam",
            "support",
            "pricing",
            "ce",
            "cur",
            "budgets",
            "cost-optimization-hub",
            "organizations",
        }
    )

    _ALIASES: dict[str, str] = {"trustedadvisor": "support"}

    def __init__(self, session_factory: AwsSessionFactory) -> None:
        """Initialise with an AWS session factory for client creation."""
        self._factory = session_factory
        self._cache: dict[tuple[str, str | None], BaseClient] = {}

    def client(self, name: str, region: str | None = None) -> Any:
        """Return a cached boto3 client, handling aliases and global-service routing."""
        resolved = self._ALIASES.get(name, name)
        effective_region: str | None = region
        if resolved in self._GLOBAL_SERVICES and region is None:
            effective_region = "us-east-1"
        key = (resolved, effective_region)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        cli = self._factory.session().client(
            resolved,
            region_name=effective_region or self._factory.region,
            config=self._factory.retry_config(),
        )
        self._cache[key] = cli
        return cli
