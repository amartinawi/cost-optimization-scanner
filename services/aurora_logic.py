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

# x86 family letter → Graviton family. Aurora supports r6g/r7g/m6g/c6g/t4g/x2g.
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
    """Map an x86 family to its Graviton equivalent (db.r5 → db.r6g). None if N/A."""
    if is_graviton_family(family):
        return None
    fam = family.split(".")[-1].lower()  # e.g. r5, m6i, c5
    letter = fam[0]
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
