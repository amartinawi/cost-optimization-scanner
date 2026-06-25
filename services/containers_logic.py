"""Pure helper functions for the containers (ECS/EKS/ECR) adapter.

Kept dependency-free (no boto3, no ScanContext) so they can be unit-tested
directly. Covers Fargate cost math, valid-combination snapping, and
cross-source de-duplication by normalized resource name.
"""

from __future__ import annotations

from typing import Any

HOURS_PER_MONTH: int = 730

# AWS Fargate task sizes: each vCPU tier permits a discrete set of memory
# values (GB). A rightsizing target MUST snap to one of these — you cannot
# bill 0.3 vCPU or 1.5 GB on a 0.25 vCPU task. Source: AWS Fargate docs.
VALID_FARGATE_VCPU: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

# Minimum valid memory (GB) for each vCPU tier — used to pick the smallest
# legal task at a given vCPU when snapping down.
_FARGATE_MIN_MEM_GB: dict[float, float] = {
    0.25: 0.5,
    0.5: 1.0,
    1.0: 2.0,
    2.0: 4.0,
    4.0: 8.0,
    8.0: 16.0,
    16.0: 32.0,
}


def _allowed_mem_for_vcpu(vcpu: float) -> list[float]:
    """Return the allowed memory values (GB) for a Fargate vCPU tier."""
    if vcpu == 0.25:
        return [0.5, 1.0, 2.0]
    if vcpu == 0.5:
        return [1.0, 2.0, 3.0, 4.0]
    if vcpu == 1.0:
        return [float(g) for g in range(2, 9)]  # 2..8 GB, 1 GB steps
    if vcpu == 2.0:
        return [float(g) for g in range(4, 17)]  # 4..16
    if vcpu == 4.0:
        return [float(g) for g in range(8, 31)]  # 8..30
    if vcpu == 8.0:
        return [float(g) for g in range(16, 61, 4)]  # 16..60, 4 GB steps
    if vcpu == 16.0:
        return [float(g) for g in range(32, 121, 8)]  # 32..120, 8 GB steps
    return []


def snap_to_valid_fargate(vcpu: float, mem_gb: float) -> tuple[float, float]:
    """Snap a (vCPU, memory-GB) request UP to the smallest valid Fargate combo.

    Returns the smallest legal combination whose vCPU >= requested vCPU and
    whose memory >= requested memory. Falls back to the largest size if the
    request exceeds Fargate's maximum.
    """
    for tier in VALID_FARGATE_VCPU:
        if tier < vcpu:
            continue
        allowed = _allowed_mem_for_vcpu(tier)
        for mem in allowed:
            if mem >= mem_gb:
                return tier, mem
    # Exceeds max Fargate size: return the largest combo.
    return 16.0, 120.0


def snap_down_fargate(vcpu: float, mem_gb: float) -> tuple[float, float] | None:
    """Return the next-smaller valid Fargate combo below (vcpu, mem_gb), or None.

    Used as a conservative rightsizing target for an over-provisioned task:
    drop one vCPU tier and pick the smallest legal memory at that tier. Returns
    None when the task is already at the smallest Fargate size (nothing to do).
    """
    # Find the current tier (snap up first so arbitrary inputs are legal).
    cur_vcpu, _ = snap_to_valid_fargate(vcpu, mem_gb)
    idx = VALID_FARGATE_VCPU.index(cur_vcpu)
    if idx == 0:
        return None
    target_vcpu = VALID_FARGATE_VCPU[idx - 1]
    target_mem = _FARGATE_MIN_MEM_GB[target_vcpu]
    return target_vcpu, target_mem


def fargate_task_hourly(
    vcpu: float,
    mem_gb: float,
    vcpu_rate: float,
    gb_rate: float,
    *,
    windows_os_rate: float = 0.0,
) -> float:
    """Hourly Fargate cost for one task: vCPU leg + memory leg (+ Windows OS fee).

    Rates are passed in already region/architecture/OS-resolved by the caller
    (from PricingEngine), so this stays pure. ``windows_os_rate`` is the
    per-vCPU Windows OS license fee (0 for Linux).
    """
    return vcpu * vcpu_rate + mem_gb * gb_rate + vcpu * windows_os_rate


def parse_manifest_layers(manifest: dict[str, Any]) -> tuple[list[tuple[str, int]], list[str]]:
    """Extract (layer-digest, size) blobs and child-manifest digests from an OCI/Docker manifest.

    Handles:
      - Docker v2 / OCI image manifests (``layers`` + ``config``) → blobs, no children.
      - Docker manifest lists / OCI image indexes (``manifests``) → children, no blobs.
      - Docker v1 schema (``fsLayers``) → no size data available → empty.

    The compressed layer sizes returned are exactly what ECR meters for storage.
    """
    if not isinstance(manifest, dict):
        return [], []
    # Manifest list / image index → recurse into child manifests.
    if "manifests" in manifest and isinstance(manifest["manifests"], list):
        children = [m.get("digest", "") for m in manifest["manifests"] if m.get("digest")]
        return [], children
    # v2 / OCI single-arch image manifest → layers (+ config blob).
    blobs: list[tuple[str, int]] = []
    if "layers" in manifest and isinstance(manifest["layers"], list):
        for layer in manifest["layers"]:
            digest = layer.get("digest", "")
            if digest:
                blobs.append((digest, int(layer.get("size", 0) or 0)))
        config = manifest.get("config", {}) or {}
        if config.get("digest"):
            blobs.append((config["digest"], int(config.get("size", 0) or 0)))
    return blobs, []


def compute_untagged_reclaimable_bytes(
    images: list[dict[str, Any]],
    fetch_manifest: Any,
) -> int:
    """Bytes reclaimable by expiring untagged images, deduplicated at layer level.

    The realizable saving from an "expire untagged images" lifecycle rule is the
    storage of layers (blobs) referenced ONLY by untagged images — NOT the sum
    of their ``imageSizeInBytes``, which double-counts base layers shared with
    tagged images (and with multi-arch indexes that tagged images point to).

    Args:
        images: dicts with ``digest`` and ``tagged`` (bool). Untagged child
            manifests of a tagged index correctly contribute 0 because their
            layers are reachable from the tagged index.
        fetch_manifest: callable(digest) -> parsed manifest dict or None.

    Returns:
        Total reclaimable bytes (>= 0).
    """
    layer_size: dict[str, int] = {}
    _reach_memo: dict[str, set[str]] = {}

    def reachable_layers(digest: str, _seen: frozenset[str] = frozenset()) -> set[str]:
        if digest in _reach_memo:
            return _reach_memo[digest]
        if digest in _seen:  # cycle guard (should not occur in OCI graphs)
            return set()
        manifest = fetch_manifest(digest)
        if not manifest:
            _reach_memo[digest] = set()
            return set()
        blobs, children = parse_manifest_layers(manifest)
        result: set[str] = set()
        for d, size in blobs:
            layer_size[d] = max(layer_size.get(d, 0), size)
            result.add(d)
        for child in children:
            result |= reachable_layers(child, _seen | {digest})
        _reach_memo[digest] = result
        return result

    tagged_layers: set[str] = set()
    untagged_layers: set[str] = set()
    for img in images:
        digest = img.get("digest", "")
        if not digest:
            continue
        layers = reachable_layers(digest)
        if img.get("tagged"):
            tagged_layers |= layers
        else:
            untagged_layers |= layers

    reclaimable = sum(layer_size.get(d, 0) for d in untagged_layers if d not in tagged_layers)
    return max(0, reclaimable)


def quantify_fargate_rightsizing(
    cpu_units: float,
    mem_mb: float,
    task_count: int,
    vcpu_rate: float,
    gb_rate: float,
    *,
    windows_os_rate: float = 0.0,
) -> dict[str, Any] | None:
    """Monthly saving + target for downsizing one over-provisioned Fargate task.

    Snaps the current (cpu_units, mem_mb) task to the next-smaller valid Fargate
    combo and prices the delta with the supplied (already region/arch/OS
    resolved) rates. Returns None when the inputs are unusable or the task is
    already at the smallest Fargate size (no realizable saving).

    Returns:
        ``{"saving", "current_vcpu", "current_mem_gb", "target_vcpu",
        "target_mem_gb", "target_cpu_units", "target_mem_mb"}`` or None.
    """
    if cpu_units <= 0 or mem_mb <= 0 or task_count <= 0 or vcpu_rate <= 0 or gb_rate <= 0:
        return None
    cur_vcpu = cpu_units / 1024.0
    cur_mem_gb = mem_mb / 1024.0
    target = snap_down_fargate(cur_vcpu, cur_mem_gb)
    if target is None:
        return None
    tgt_vcpu, tgt_mem_gb = target
    cur_hr = fargate_task_hourly(cur_vcpu, cur_mem_gb, vcpu_rate, gb_rate, windows_os_rate=windows_os_rate)
    tgt_hr = fargate_task_hourly(tgt_vcpu, tgt_mem_gb, vcpu_rate, gb_rate, windows_os_rate=windows_os_rate)
    saving = max(0.0, (cur_hr - tgt_hr) * HOURS_PER_MONTH * task_count)
    return {
        "saving": saving,
        "current_vcpu": cur_vcpu,
        "current_mem_gb": cur_mem_gb,
        "target_vcpu": tgt_vcpu,
        "target_mem_gb": tgt_mem_gb,
        "target_cpu_units": int(tgt_vcpu * 1024),
        "target_mem_mb": int(tgt_mem_gb * 1024),
    }


def normalize_resource_name(value: str) -> str:
    """Reduce an ARN or path to its final resource name segment.

    e.g. ``arn:aws:ecs:...:service/cluster/web`` → ``web``;
    ``cluster/web`` → ``web``; ``web`` → ``web``.
    """
    if not value:
        return ""
    return str(value).split("/")[-1].split(":")[-1]


def dedupe_by_authority(
    cost_hub: list[dict[str, Any]],
    compute_optimizer: list[dict[str, Any]],
    heuristics: list[dict[str, Any]],
    *,
    coh_key: Any,
    co_key: Any,
    heuristic_key: Any,
) -> list[dict[str, Any]]:
    """Return heuristic recs not already covered by CoH or Compute Optimizer.

    Authority order CoH > Compute Optimizer > heuristics: a heuristic finding
    for a resource already surfaced by an AWS-native source is dropped so its
    savings are never counted twice. ``*_key`` callables extract the normalized
    resource name from a rec of that source.
    """
    covered: set[str] = set()
    for rec in cost_hub:
        name = normalize_resource_name(coh_key(rec))
        if name:
            covered.add(name)
    for rec in compute_optimizer:
        name = normalize_resource_name(co_key(rec))
        if name:
            covered.add(name)
    kept: list[dict[str, Any]] = []
    for rec in heuristics:
        name = normalize_resource_name(heuristic_key(rec))
        if name and name in covered:
            continue
        kept.append(rec)
    return kept
