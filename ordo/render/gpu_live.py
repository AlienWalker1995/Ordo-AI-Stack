"""Live GPU telemetry: the stack's one reader of what each card is doing right now.

ops-controller serves it at `GET /gpus` and derives `GET /registry/gpus` from it; the dashboard
reads `GET /gpus` rather than probing a GPU itself. hardware.py answers a different question (what
cards this box has, for the right-sizer); this module answers how full and how busy they are.

Each card is a dict: index, uuid, name, vendor ("nvidia" | "amd"), vram_total_mib, vram_used_mib
(None when the reading is not credible), utilization_pct and temp_c (None when unknown).

Sources:
  * NVIDIA: one nvidia-smi query.
  * AMD: the amdgpu driver's sysfs files, which containers see without rocm-smi. Experimental: the
    AMD path has not run on real AMD hardware.

Every probe degrades to no cards instead of raising: a host without nvidia-smi or an amdgpu sysfs
tree is a host without that vendor's GPUs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_NVIDIA_QUERY = ("index,uuid,name,memory.total,memory.free,memory.used,"
                 "utilization.gpu,temperature.gpu")
# The fields after the name in _NVIDIA_QUERY, all numeric.
_NVIDIA_TRAILING_FIELDS = 5
_AMD_PCI_VENDOR = "0x1002"
_MIB = 1024 * 1024


def _int_or_none(text: str) -> int | None:
    """"41" -> 41; "[N/A]", "" or anything else unparseable -> None."""
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _credible_used_mib(total: int, free: int | None, used: int | None) -> int | None:
    """Used VRAM, or None when neither reading is believable.

    Windows Docker Desktop drivers can report memory.used (and sometimes memory.free) wrapped far
    past the card's total. Prefer used when it fits the card, else total - free when free fits,
    else unknown: a confident wrong number is worse than none."""
    if used is not None and 0 <= used <= total:
        return used
    if free is not None and 0 <= free <= total:
        return total - free
    return None


def parse_nvidia_smi(csv_text: str) -> list[dict[str, Any]]:
    """Parse `nvidia-smi --query-gpu=<_NVIDIA_QUERY> --format=csv,noheader,nounits`.

    The name may contain commas (the CSV is not quote-escaped): fields 0 and 1 are index and uuid,
    the last five are numeric, and the name is everything between. A line without a readable
    index, uuid or total is skipped."""
    gpus: list[dict[str, Any]] = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 + _NVIDIA_TRAILING_FIELDS or not parts[1]:
            continue
        index = _int_or_none(parts[0])
        total, free, used, util, temp = (_int_or_none(p) for p in parts[-_NVIDIA_TRAILING_FIELDS:])
        if index is None or total is None or total <= 0:
            continue
        gpus.append({
            "index": index,
            "uuid": parts[1],
            "name": ", ".join(parts[2:-_NVIDIA_TRAILING_FIELDS]),
            "vendor": "nvidia",
            "vram_total_mib": total,
            "vram_used_mib": _credible_used_mib(total, free, used),
            "utilization_pct": util,
            "temp_c": temp,
        })
    return gpus


def nvidia_gpus() -> list[dict[str, Any]]:
    """Every NVIDIA card nvidia-smi reports; [] when it is missing or fails."""
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi(result.stdout)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_int(path: Path) -> int | None:
    return _int_or_none(_read_text(path) or "")


def _amd_temp_c(device: Path) -> int | None:
    """The first hwmon temp1_input (millidegrees C), in whole degrees."""
    for sensor in sorted(device.glob("hwmon/hwmon*/temp1_input")):
        millidegrees = _read_int(sensor)
        if millidegrees is not None:
            return millidegrees // 1000
    return None


def _amd_card(device: Path, index: int) -> dict[str, Any] | None:
    """One amdgpu device directory as a card, or None when it is not an AMD GPU with VRAM."""
    if (_read_text(device / "vendor") or "").lower() != _AMD_PCI_VENDOR:
        return None
    total_bytes = _read_int(device / "mem_info_vram_total")
    if total_bytes is None or total_bytes <= 0:
        return None
    total = total_bytes // _MIB
    used_bytes = _read_int(device / "mem_info_vram_used")
    used = used_bytes // _MIB if used_bytes is not None else None
    return {
        "index": index,
        # unique_id is only exposed on some ASICs; the PCI address always identifies the card.
        "uuid": _read_text(device / "unique_id") or device.resolve().name,
        "name": _read_text(device / "product_name") or "AMD GPU",
        "vendor": "amd",
        "vram_total_mib": total,
        "vram_used_mib": _credible_used_mib(total, None, used),
        "utilization_pct": _read_int(device / "gpu_busy_percent"),
        "temp_c": _amd_temp_c(device),
    }


def _card_number(entry: Path) -> int:
    return int(entry.name[len("card"):])


def amd_sysfs_gpus(sysfs_root: str | Path = "/sys") -> list[dict[str, Any]]:
    """Every AMD card under `<sysfs_root>/class/drm/card<N>/device`; [] when there are none.

    /sys/class/drm also lists connectors (card0-DP-1) and render nodes (renderD128); only the bare
    card<N> entries are GPUs. Other vendors' cards are skipped by their PCI vendor id."""
    drm = Path(sysfs_root) / "class" / "drm"
    try:
        entries = [e for e in drm.iterdir() if e.name.startswith("card") and e.name[4:].isdigit()]
    except OSError:
        return []
    gpus: list[dict[str, Any]] = []
    for entry in sorted(entries, key=_card_number):
        try:
            card = _amd_card(entry / "device", index=len(gpus))
        except OSError:
            continue
        if card is not None:
            gpus.append(card)
    return gpus


def live_gpus(sysfs_root: str | Path = "/sys") -> list[dict[str, Any]]:
    """Every card this host can read, NVIDIA first. AMD indexes continue after NVIDIA's, so each
    index names one card."""
    gpus = nvidia_gpus()
    for card in amd_sysfs_gpus(sysfs_root):
        gpus.append({**card, "index": len(gpus)})
    return gpus
