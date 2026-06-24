"""CLI service-key resolver for --scan-only / --skip-service filtering.

Mirrors the service_map and should_scan_service logic in
CostOptimizer.scan_region (lines 2752-2814), but works against
ServiceModule metadata instead of a hardcoded dict.
"""

from __future__ import annotations

from core.contracts import ServiceModule


def _build_alias_index(modules: list[ServiceModule]) -> dict[str, str]:
    """Build a lower-cased alias-to-key lookup from all module cli_aliases."""
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
    """Resolve a set of CLI tokens to canonical module keys via alias index or direct match."""
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


def unrecognized_tokens(modules: list[ServiceModule], tokens: set[str] | None) -> set[str]:
    """Return CLI tokens that match no module key or alias.

    Lets callers warn the user instead of silently scanning nothing when a
    ``--scan-only``/``--skip-service`` token is misspelled or unknown (e.g.
    ``--scan-only ecs`` before ecs was registered as a containers alias).
    """
    if not tokens:
        return set()
    alias_index = _build_alias_index(modules)
    keys = {m.key for m in modules}
    return {t for t in tokens if t.lower() not in alias_index and t.lower() not in keys}


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
