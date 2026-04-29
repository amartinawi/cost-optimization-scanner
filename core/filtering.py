"""CLI service-key resolver for --scan-only / --skip-service filtering.

Mirrors the service_map and should_scan_service logic in
CostOptimizer.scan_region (lines 2752-2814), but works against
ServiceModule metadata instead of a hardcoded dict.
"""

from __future__ import annotations

from core.contracts import ServiceModule


def _build_alias_index(modules: list[ServiceModule]) -> dict[str, str]:
    index: dict[str, str] = {}
    for mod in modules:
        for alias in mod.cli_aliases:
            index[alias.lower()] = mod.key
    return index


def _resolve_tokens(
    tokens: set[str],
    alias_index: dict[str, str],
    modules: list[ServiceModule],
) -> set[str]:
    keys: set[str] = set()
    for token in tokens:
        token_lower = token.lower()
        if token_lower in alias_index:
            keys.add(alias_index[token_lower])
        else:
            for mod in modules:
                if token_lower == mod.key:
                    keys.add(mod.key)
                    break
    return keys


def resolve_cli_keys(
    modules: list[ServiceModule],
    scan_only: set[str] | None,
    skip: set[str] | None,
) -> set[str]:
    """Resolve --scan-only / --skip-service CLI args to module keys.

    Returns the set of module keys that should be scanned.
    """
    all_keys = {m.key for m in modules}
    if scan_only is None and skip is None:
        return all_keys

    alias_index = _build_alias_index(modules)

    if scan_only is not None:
        return _resolve_tokens(scan_only, alias_index, modules)

    skip_keys = _resolve_tokens(skip, alias_index, modules) if skip else set()
    return all_keys - skip_keys
