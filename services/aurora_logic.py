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

# M360-1 / C18 — RAM per vCPU by DB family. Memory scales linearly with size
# WITHIN a family (db.r6g.large 2 vCPU/16 GiB … db.r6g.4xlarge 16 vCPU/128 GiB),
# so the ratio plus the vCPU count gives the class's memory without a per-size
# table or a pricing lookup.
#
# Burstable (t) and compute (c) are deliberately ABSENT: t-family memory is not
# linear in vCPU (t3.micro 1 GiB and t3.large 8 GiB both have 2 vCPU), so there
# is no honest ratio. An absent family returns None and the caller withholds the
# lever rather than guessing — the whole point of this fix.
_FAMILY_GIB_PER_VCPU: dict[str, float] = {
    "r": 8.0,    # memory optimized — r5/r6g/r6i/r7g/r8g
    "m": 4.0,    # general purpose  — m5/m6g/m6i/m7g
    "x": 16.0,   # high memory      — x2g
}
RIGHTSIZE_MEMORY_HEADROOM: float = 1.2  # safety margin over measured memory in use


def instance_memory_gib(family: str, vcpu: int) -> float | None:
    """Total RAM (GiB) for a DB class, or None when the family has no ratio.

    ``family`` is the ``db.<fam>`` prefix from :func:`parse_instance_class`.
    """
    parts = str(family or "").split(".")
    fam = parts[1] if len(parts) > 1 else ""
    ratio = _FAMILY_GIB_PER_VCPU.get(fam[:1]) if fam else None
    if ratio is None or vcpu <= 0:
        return None
    return round(vcpu * ratio, 2)


def memory_floor_size(
    used_gib: float, *, gib_per_vcpu: float, headroom: float = RIGHTSIZE_MEMORY_HEADROOM
) -> str | None:
    """Smallest size whose RAM covers ``used_gib`` x headroom, or None if none does.

    The memory counterpart of :func:`rightsize_target_size`. Unlike the CPU
    floor this never returns "smaller than current" logic — the caller composes
    the two floors and decides whether the winner is actually a downsize.
    """
    required = max(0.0, used_gib) * headroom
    for size in _SNAP_SIZES:
        if _SIZE_VCPU[size] * gib_per_vcpu >= required:
            return size
    return None


def combined_rightsize_target(
    current_vcpu: int, peak_cpu_pct: float, family: str, used_memory_gib: float | None
) -> str | None:
    """The smallest size that satisfies BOTH the CPU and the memory floor.

    Returns None when there is no safe downsize — including when memory could
    not be established at all (metric unreadable, or a family with no ratio).
    Falling back to the CPU-only answer in that case is precisely the M360-1
    defect: it recommended db.r6g.4xlarge → db.r6g.large, an 8x RAM cut on a
    production writer, from a 2% CPU reading.
    """
    cpu_target = rightsize_target_size(current_vcpu, peak_cpu_pct)
    if cpu_target is None:
        return None
    if used_memory_gib is None:
        return None
    ratio_probe = instance_memory_gib(family, 1)
    if ratio_probe is None:
        return None
    mem_target = memory_floor_size(used_memory_gib, gib_per_vcpu=ratio_probe)
    if mem_target is None:
        return None
    winner = max(cpu_target, mem_target, key=lambda s: _SIZE_VCPU[s])
    return winner if _SIZE_VCPU[winner] < current_vcpu else None


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
