"""The one live GPU reader (ordo/render/gpu_live.py): NVIDIA via nvidia-smi, AMD via sysfs.

ops-controller serves it at `GET /gpus` and derives `GET /registry/gpus` from it; the dashboard
reads `GET /gpus` instead of probing a GPU itself. No real GPU is needed: nvidia-smi output is
parsed from text and the AMD sysfs tree is built under tmp_path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ordo.render import gpu_live


# ── NVIDIA: nvidia-smi CSV ─────────────────────────────────────────────────────
# Query order: index,uuid,name,memory.total,memory.free,memory.used,utilization.gpu,temperature.gpu
def test_nvidia_parse_reads_every_card():
    csv = (
        "0, GPU-aaa, NVIDIA GeForce GTX 1070, 8192, 7680, 512, 11, 55\n"
        "1, GPU-bbb, NVIDIA GeForce RTX 5090, 32607, 12181, 20426, 1, 41\n"
    )
    gpus = gpu_live.parse_nvidia_smi(csv)
    assert len(gpus) == 2
    card = next(g for g in gpus if g["uuid"] == "GPU-bbb")
    assert card == {
        "index": 1,
        "uuid": "GPU-bbb",
        "name": "NVIDIA GeForce RTX 5090",
        "vendor": "nvidia",
        "vram_total_mib": 32607,
        "vram_used_mib": 20426,
        "utilization_pct": 1,
        "temp_c": 41,
    }


def test_nvidia_parse_keeps_commas_in_the_name():
    """nvidia-smi does not quote-escape CSV: index,uuid lead, the numeric fields trail, the name
    is everything between."""
    gpus = gpu_live.parse_nvidia_smi("0, GPU-x, NVIDIA RTX, Special, 16384, 16284, 100, 5, 50\n")
    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX, Special"
    assert gpus[0]["uuid"] == "GPU-x"
    assert gpus[0]["index"] == 0
    assert gpus[0]["vram_total_mib"] == 16384
    assert gpus[0]["vram_used_mib"] == 100


def test_nvidia_wrapped_used_falls_back_to_total_minus_free():
    """Windows Docker Desktop drivers can report memory.used wrapped past total."""
    gpus = gpu_live.parse_nvidia_smi("0, GPU-w, RTX 5090, 32607, 30000, 99999999, 3, 40\n")
    assert gpus[0]["vram_used_mib"] == 2607


def test_nvidia_used_is_none_when_used_and_free_are_both_garbage():
    """Neither reading is credible: say unknown, never a confident wrong number."""
    gpus = gpu_live.parse_nvidia_smi("0, GPU-g, RTX 5090, 32607, 4400000000, 99999999, 3, 40\n")
    assert gpus[0]["vram_total_mib"] == 32607
    assert gpus[0]["vram_used_mib"] is None


def test_nvidia_unknown_temperature_is_none():
    gpus = gpu_live.parse_nvidia_smi("0, GPU-t, RTX 5090, 32607, 30000, 2607, 3, [N/A]\n")
    assert gpus[0]["temp_c"] is None


def test_nvidia_parse_of_empty_output_is_empty():
    assert gpu_live.parse_nvidia_smi("") == []


# ── AMD: sysfs ─────────────────────────────────────────────────────────────────
def _pci(address: str) -> str:
    """A PCI address as a directory name. Windows forbids ':' in file names, so the test tree
    uses '-' there; the reader never parses the address, it only reports it."""
    return address.replace(":", "-") if os.name == "nt" else address


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _amd_card(sys_root: Path, card: str, pci: str, *, full: bool = True) -> None:
    device = sys_root / "devices" / _pci("pci0000:00") / pci
    _write(device / "vendor", "0x1002\n")
    _write(device / "mem_info_vram_total", str(24 * 1024 * 1024 * 1024) + "\n")
    if full:
        _write(device / "mem_info_vram_used", str(6 * 1024 * 1024 * 1024) + "\n")
        _write(device / "gpu_busy_percent", "37\n")
        _write(device / "product_name", "Radeon RX 7900 XTX\n")
        _write(device / "unique_id", "a1b2c3d4e5f60718\n")
        _write(device / "hwmon" / "hwmon3" / "temp1_input", "52000\n")
    link = sys_root / "class" / "drm" / card / "device"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(device, target_is_directory=True)


def _other_vendor_card(sys_root: Path, card: str, pci: str) -> None:
    device = sys_root / "devices" / _pci("pci0000:00") / pci
    _write(device / "vendor", "0x8086\n")  # an Intel iGPU: not ours to read
    _write(device / "mem_info_vram_total", "1073741824\n")
    link = sys_root / "class" / "drm" / card / "device"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(device, target_is_directory=True)


def test_amd_sysfs_reads_an_amd_card_and_ignores_other_vendors(tmp_path):
    _amd_card(tmp_path, "card1", _pci("0000:03:00.0"))
    _other_vendor_card(tmp_path, "card0", _pci("0000:00:02.0"))
    gpus = gpu_live.amd_sysfs_gpus(tmp_path)
    assert gpus == [{
        "index": 0,
        "uuid": "a1b2c3d4e5f60718",
        "name": "Radeon RX 7900 XTX",
        "vendor": "amd",
        "vram_total_mib": 24576,
        "vram_used_mib": 6144,
        "utilization_pct": 37,
        "temp_c": 52,
    }]


def test_amd_sysfs_degrades_when_optional_files_are_missing(tmp_path):
    _amd_card(tmp_path, "card0", _pci("0000:0b:00.0"), full=False)
    gpus = gpu_live.amd_sysfs_gpus(tmp_path)
    assert gpus == [{
        "index": 0,
        "uuid": _pci("0000:0b:00.0"),
        "name": "AMD GPU",
        "vendor": "amd",
        "vram_total_mib": 24576,
        "vram_used_mib": None,
        "utilization_pct": None,
        "temp_c": None,
    }]


def test_amd_sysfs_ignores_connector_entries(tmp_path):
    """/sys/class/drm also lists connectors (card0-DP-1) and render nodes; only cardN is a GPU."""
    _amd_card(tmp_path, "card0", _pci("0000:0b:00.0"))
    (tmp_path / "class" / "drm" / "card0-DP-1").mkdir(parents=True)
    (tmp_path / "class" / "drm" / "renderD128").mkdir(parents=True)
    assert len(gpu_live.amd_sysfs_gpus(tmp_path)) == 1


def test_amd_sysfs_without_a_drm_tree_is_empty(tmp_path):
    assert gpu_live.amd_sysfs_gpus(tmp_path) == []


# ── live_gpus: every source, never raising ─────────────────────────────────────
def test_live_gpus_with_no_source_is_empty(tmp_path, monkeypatch):
    def missing_binary(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", missing_binary)
    assert gpu_live.live_gpus(sysfs_root=tmp_path) == []


def test_live_gpus_combines_vendors_with_unique_indexes(tmp_path, monkeypatch):
    smi = "0, GPU-aaa, NVIDIA GeForce RTX 5090, 32607, 12181, 20426, 1, 41\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, smi, ""))
    _amd_card(tmp_path, "card1", _pci("0000:03:00.0"))
    gpus = gpu_live.live_gpus(sysfs_root=tmp_path)
    assert [(g["vendor"], g["index"]) for g in gpus] == [("nvidia", 0), ("amd", 1)]


def test_live_gpus_survives_a_failing_nvidia_smi(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 9, "", "NVML: driver mismatch"))
    assert gpu_live.live_gpus(sysfs_root=tmp_path) == []
