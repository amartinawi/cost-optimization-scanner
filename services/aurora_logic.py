"""Pure helpers for Aurora provisioned-instance rightsizing and Graviton checks.

Dependency-free (no boto3) so the class-sizing math is unit-testable. Pricing is
resolved by the caller via PricingEngine and passed in.
"""

from __future__ import annotations

import re

# vCPU per RDS/Aurora instance size suffix (the part after db.<family>.).
_SIZE_VCPU: dict[str, int] = {
    "medium": 1,
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
    "24xlarge": 96,
    "32xlarge": 128,
}
# Sizes we will snap a rightsize target to, ordered small→large. Restricted to
# the widely-available set so a target class actually exists for r/m/c families.
_SNAP_SIZES: tuple[str, ...] = (
    "large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge",
)

# AUR-C — x86 family -> Graviton family, GENERATION-AWARE.
#
# The old map sent every x86 family to a Graviton2 target regardless of source
# generation, so db.r7i mapped to db.r6g. That is a backwards migration: an
# operator moving off r7i goes to r7g, and pricing the delta against the
# cheaper r6g overstates the saving of the migration they would actually make.
#
# The rule is now "same generation where a Graviton counterpart exists, else the
# newest Graviton generation below it". Nothing here is trusted blindly: the
# caller prices the derived class with allow_fallback=False, so a class this map
# produces that AWS does not actually offer resolves to $0 and abstains rather
# than pricing against the flat RDS fallback constant.
_GRAVITON_BY_GENERATION: dict[str, dict[int, str]] = {
    "r": {5: "r6g", 6: "r6g", 7: "r7g", 8: "r8g"},
    "m": {5: "m6g", 6: "m6g", 7: "m7g", 8: "m8g"},
    "c": {5: "c6g", 6: "c6g", 7: "c7g", 8: "c8g"},
    "t": {2: "t4g", 3: "t4g", 4: "t4g"},
    "x": {1: "x2g", 2: "x2g"},
}
# Used when the family carries no parsable generation digit.
_GRAVITON_FAMILY: dict[str, str] = {"r": "r6g", "m": "m6g", "c": "c6g", "t": "t4g", "x": "x2g"}

RIGHTSIZE_HEADROOM: float = 1.2  # safety margin over measured peak CPU


def parse_instance_class(instance_class: str) -> tuple[str, str, int] | None:
    """Split 'db.r5.8xlarge' → ('db.r5', '8xlarge', 32 vCPU). None if unparseable."""
    parts = str(instance_class).split(".")
    if len(parts) != 3:
        return None
    family = f"{parts[0]}.{parts[1]}"
    size = parts[2]
    vcpu = _SIZE_VCPU.get(size)
    if vcpu is None:
        return None
    return family, size, vcpu


def is_graviton_family(family: str) -> bool:
    """True if the family is already ARM/Graviton (e.g. db.r6g, db.t4g, db.x2g)."""
    fam = family.split(".")[-1].lower()
    return bool(re.search(r"[0-9]g", fam))


def graviton_equivalent(family: str) -> str | None:
    """Map an x86 family to its same-generation Graviton equivalent.

    ``db.r5`` -> ``db.r6g`` (no r5g exists), ``db.r7i`` -> ``db.r7g``,
    ``db.m8i`` -> ``db.m8g``. Returns ``None`` when the family has no Graviton
    counterpart. The caller must price the result with ``allow_fallback=False``:
    this map cannot know which classes a given engine and region actually offer.
    """
    if is_graviton_family(family):
        return None
    fam = family.split(".")[-1].lower()  # e.g. r5, m6i, c5
    letter = fam[0]
    digits = "".join(ch for ch in fam[1:] if ch.isdigit())
    if digits:
        by_gen = _GRAVITON_BY_GENERATION.get(letter, {})
        generation = int(digits)
        target = by_gen.get(generation)
        if target is None and by_gen:
            # No counterpart at this generation - step down to the newest
            # Graviton generation below it rather than jumping to Graviton2.
            lower = [g for g in by_gen if g < generation]
            target = by_gen[max(lower)] if lower else None
        if target:
            return f"db.{target}"
        if letter not in _GRAVITON_BY_GENERATION:
            return None
    target = _GRAVITON_FAMILY.get(letter)
    return f"db.{target}" if target else None


def rightsize_target_size(current_vcpu: int, peak_cpu_pct: float, *, headroom: float = RIGHTSIZE_HEADROOM) -> str | None:
    """Smallest size suffix whose vCPU covers peak CPU × headroom, if smaller than current.

    Returns None when nothing smaller safely covers the measured peak (e.g. a
    busy instance), so a high-peak DB is never recommended for downsizing.
    """
    required = current_vcpu * max(0.0, peak_cpu_pct) / 100.0 * headroom
    for size in _SNAP_SIZES:
        vcpu = _SIZE_VCPU[size]
        if vcpu >= required:
            return size if vcpu < current_vcpu else None
    return None
