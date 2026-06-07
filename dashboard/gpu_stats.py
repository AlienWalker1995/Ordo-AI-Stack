"""Multi-GPU enumeration for the dashboard GPU tab.

Uses `nvidia-smi`. The dashboard container has the `utility` GPU capability
(no compute), which is enough for stats.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


def _mib_to_gb(mib: float) -> float:
    return round(mib * _MIB / 1e9, 1)


def parse_smi_gpus(csv_text: str) -> list[dict]:
    """Parse `nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,
    utilization.gpu,temperature.gpu --format=csv,noheader,nounits`.
    The name field may contain commas (CSV is not quote-escaped): fields 0,1 are
    index,uuid and the last four are numeric, so name is everything between."""
    gpus: list[dict] = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6 or not parts[1]:
            continue
        try:
            index = int(parts[0])
            total_mib = int(parts[-4])
            used_mib = int(parts[-3])
            util = int(parts[-2])
            temp = int(parts[-1])
        except ValueError:
            continue
        name = ", ".join(parts[2:-4])
        gpus.append({
            "index": index,
            "uuid": parts[1],
            "name": name,
            "vram_total_gb": _mib_to_gb(total_mib),
            "vram_used_gb": _mib_to_gb(used_mib),
            "vram_total_mib": total_mib,
            "utilization_pct": util,
            "temp_c": temp,
        })
    return gpus


def list_gpus() -> dict:
    """Return {"gpus": [...], "reachable": bool}. reachable=False => the container
    runtime sees no GPU adapter (e.g. WSL passthrough lost)."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        gpus = parse_smi_gpus(out)
        return {"gpus": gpus, "reachable": bool(gpus)}
    except Exception as e:
        logger.debug("nvidia-smi list_gpus failed: %s", e)
        return {"gpus": [], "reachable": False}


def biggest(gpus: list[dict]) -> dict | None:
    """Return the largest-VRAM GPU, or None."""
    return max(gpus, key=lambda g: g["vram_total_mib"], default=None)
