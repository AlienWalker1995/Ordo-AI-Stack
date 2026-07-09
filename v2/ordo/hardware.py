"""Hardware detection for the right-sizer.

Detects GPUs (VRAM), system RAM, CPU, and platform. Fully mockable so the sizer can be
tested against fake machines in CI (no real GPU needed) — that's how a hardware-adaptive
stack gets validated for hardware you'll never own.
"""
from __future__ import annotations

import dataclasses
import os
import platform
import re
import shutil
import subprocess
from typing import Any


@dataclasses.dataclass(frozen=True)
class GPU:
    name: str
    vram_gb: float


@dataclasses.dataclass(frozen=True)
class HardwareProfile:
    gpus: tuple[GPU, ...] = ()
    ram_gb: float = 0.0
    cpu_cores: int = 1
    platform: str = "unknown"

    @property
    def has_gpu(self) -> bool:
        return len(self.gpus) > 0

    @property
    def primary_vram_gb(self) -> float:
        """VRAM of the single largest GPU (the stack pins compute to one card)."""
        return max((g.vram_gb for g in self.gpus), default=0.0)

    def summary(self) -> str:
        if self.has_gpu:
            g = max(self.gpus, key=lambda x: x.vram_gb)
            return f"{g.name} {g.vram_gb:.0f}GB · {self.ram_gb:.0f}GB RAM · {self.cpu_cores} cores · {self.platform}"
        return f"CPU-only · {self.ram_gb:.0f}GB RAM · {self.cpu_cores} cores · {self.platform}"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "HardwareProfile":
        """Build a profile from an explicit dict (pinned hardware / CI mock)."""
        gpus = tuple(
            GPU(name=str(g.get("name", "GPU")), vram_gb=float(g.get("vram_gb", 0)))
            for g in (spec.get("gpus") or [])
        )
        return cls(
            gpus=gpus,
            ram_gb=float(spec.get("ram_gb", 0) or 0),
            cpu_cores=int(spec.get("cpu_cores", 1) or 1),
            platform=str(spec.get("platform", platform.system() or "unknown")),
        )


def _detect_gpus() -> tuple[GPU, ...]:
    if not shutil.which("nvidia-smi"):
        return ()
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return ()
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    gpus.append(GPU(name=parts[0], vram_gb=round(float(parts[1]) / 1024.0, 1)))
                except ValueError:
                    continue
        return tuple(gpus)
    except (OSError, subprocess.SubprocessError):
        return ()


def _detect_ram_gb() -> float:
    # Linux / macOS
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3, 1)
    except (ValueError, OSError):
        pass
    # Windows via ctypes
    if platform.system() == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / 1024**3, 1)
        except Exception:
            pass
    return 0.0


def detect() -> HardwareProfile:
    """Detect the real machine."""
    return HardwareProfile(
        gpus=_detect_gpus(),
        ram_gb=_detect_ram_gb(),
        cpu_cores=os.cpu_count() or 1,
        platform=platform.system() or "unknown",
    )
