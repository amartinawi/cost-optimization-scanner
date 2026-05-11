"""Base adapter class for ServiceModule Protocol implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.contracts import Group, GroupingSpec, ServiceFindings, ServiceModule, StatCardSpec
from core.scan_context import ScanContext


class BaseServiceModule:
    """Base class providing default ServiceModule Protocol implementation.

    Subclasses override `scan()` and optionally `required_clients()`.
    All other Protocol fields have sensible defaults.
    """

    key: str = ""
    cli_aliases: tuple[str, ...] = ()
    display_name: str = ""
    stat_cards: tuple[StatCardSpec, ...] = ()
    grouping: GroupingSpec | None = None
    requires_cloudwatch: bool = False
    reads_fast_mode: bool = False
    custom_grouping: Callable[[ServiceFindings], list[Group]] | None = None

    def required_clients(self) -> tuple[str, ...]:
        """Return boto3 client names this adapter needs. Default: empty tuple."""
        return ()

    def scan(self, ctx: Any) -> ServiceFindings:
        """Execute the service scan and return findings. Must be overridden by subclasses."""
        raise NotImplementedError
