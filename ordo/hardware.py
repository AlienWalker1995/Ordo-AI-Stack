"""Hardware detection for the right-sizer.

Detects GPUs (vendor, VRAM, NVIDIA compute capability), system RAM, CPU cores and arch, and
platform. Fully mockable so the sizer can be tested against fake machines in CI (no real GPU
needed): that's how a hardware-adaptive stack gets validated for hardware you'll never own.

Every probe degrades instead of raising: a host without nvidia-smi / rocm-smi is a host without
that vendor's GPUs, and a probe that exists but fails says so on stdout.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import shutil
import subprocess
from typing import Any

# GPU vendors the render understands. "nvidia" is the default for a declared GPU, so an
# `ordo.yaml` written before vendors existed keeps meaning what it meant.
GPU_VENDORS = ("nvidia", "amd", "intel", "apple")
# CPU architectures, normalised from platform.machine() (Windows says AMD64, Linux aarch64).
CPU_ARCHES = ("x86_64", "arm64", "unknown")
_ARCH_ALIASES = {"x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
                 "arm64": "arm64", "aarch64": "arm64"}
_COMPUTE_CAP = re.compile(r"^(\d+)(?:\.(\d+))?$")


def normalize_arch(machine: str) -> str:
    """A `platform.machine()` spelling -> one of CPU_ARCHES ("unknown" when unrecognised)."""
    return _ARCH_ALIASES.get(str(machine or "").strip().lower(), "unknown")


def normalize_compute_cap(value: Any) -> str:
    """An NVIDIA compute capability as "major.minor" ("" = unknown). Accepts 8.9, "8.9", 12, "12.0".

    Raises ValueError for anything else, so a `sm_89` typo cannot quietly mean "unknown"."""
    if value is None or str(value).strip() == "":
        return ""
    match = _COMPUTE_CAP.match(str(value).strip())
    if not match:
        raise ValueError(f"compute_cap must look like 8.9 or 12.0, got {value!r}")
    return f"{int(match.group(1))}.{int(match.group(2) or 0)}"


def compute_cap_tuple(compute_cap: str) -> tuple[int, int] | None:
    """"12.0" -> (12, 0); "" (unknown) -> None."""
    normalized = normalize_compute_cap(compute_cap)
    if not normalized:
        return None
    major, minor = normalized.split(".")
    return int(major), int(minor)


@dataclasses.dataclass(frozen=True)
class GPU:
    name: str
    vram_gb: float
    uuid: str = ""          # nvidia GPU-<uuid>; the ONLY reliable pin under Docker Desktop/WSL2
    vendor: str = "nvidia"  # one of GPU_VENDORS
    compute_cap: str = ""   # NVIDIA only, "major.minor" (e.g. "12.0"); "" = unknown


@dataclasses.dataclass(frozen=True)
class HardwareProfile:
    gpus: tuple[GPU, ...] = ()
    ram_gb: float = 0.0
    cpu_cores: int = 1
    platform: str = "unknown"
    arch: str = "unknown"   # one of CPU_ARCHES

    @property
    def has_gpu(self) -> bool:
        return len(self.gpus) > 0

    @property
    def gpu_vendor(self) -> str:
        """The primary GPU's vendor, or "none" on a GPU-less host."""
        primary = self.primary_gpu
        return primary.vendor if primary else "none"

    @property
    def primary_is_nvidia(self) -> bool:
        """The compute card is NVIDIA: the only vendor compose can reserve (`driver: nvidia`), so
        device reservations, uuid pins and NVIDIA-only plugins key off this, not has_gpu."""
        return self.gpu_vendor == "nvidia"

    @property
    def primary_gpu(self) -> GPU | None:
        """The single largest-VRAM GPU — the stack pins compute (llama.cpp/ComfyUI) here."""
        return max(self.gpus, key=lambda g: g.vram_gb) if self.gpus else None

    @property
    def primary_vram_gb(self) -> float:
        """VRAM of the single largest GPU (the stack pins compute to one card)."""
        return max((g.vram_gb for g in self.gpus), default=0.0)

    @property
    def secondary_gpu(self) -> GPU | None:
        """Any GPU that is NOT the primary (largest-VRAM) one. Voice's STT/TTS have no
        Blackwell kernels and CRASH on the 5090 → they must land on the Pascal 1070 here.
        Returns the largest of the remaining GPUs (deterministic when >2 cards)."""
        primary = self.primary_gpu
        if primary is None:
            return None
        rest = [g for g in self.gpus if g is not primary]
        return max(rest, key=lambda g: g.vram_gb) if rest else None

    def summary(self) -> str:
        host = f"{self.ram_gb:.0f}GB RAM | {self.cpu_cores} cores | {self.platform} {self.arch}"
        if self.has_gpu:
            g = max(self.gpus, key=lambda x: x.vram_gb)
            compute = f" {g.compute_cap}" if g.compute_cap else ""
            return f"{g.name} {g.vram_gb:.0f}GB [{g.vendor}{compute}] | {host}"
        return f"CPU-only | {host}"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> HardwareProfile:
        """Build a profile from an explicit dict (pinned hardware / CI mock).

        A GPU without `vendor` is NVIDIA (what every pre-vendor source meant). A GPU without
        `compute_cap` has an unknown one, so a model that needs a specific build is not picked for
        it. `arch` defaults to this machine's, the same way `platform` does."""
        gpus = tuple(_gpu_from_spec(i, g) for i, g in enumerate(spec.get("gpus") or []))
        declared_arch = spec.get("arch")
        if declared_arch is None:
            arch = _detect_arch()
        else:
            arch = normalize_arch(declared_arch)
            if arch == "unknown" and str(declared_arch).strip().lower() != "unknown":
                raise ValueError(f"hardware.arch must be one of {list(CPU_ARCHES)} (or an alias such as "
                                 f"amd64/aarch64), got {declared_arch!r}")
        return cls(
            gpus=gpus,
            ram_gb=float(spec.get("ram_gb", 0) or 0),
            cpu_cores=int(spec.get("cpu_cores", 1) or 1),
            platform=str(spec.get("platform", platform.system() or "unknown")),
            arch=arch,
        )


def _gpu_from_spec(index: int, g: dict[str, Any]) -> GPU:
    vendor = str(g.get("vendor", "nvidia")).strip().lower()
    if vendor not in GPU_VENDORS:
        raise ValueError(f"hardware.gpus[{index}].vendor must be one of {list(GPU_VENDORS)}, got {vendor!r}")
    try:
        compute_cap = normalize_compute_cap(g.get("compute_cap"))
    except ValueError as exc:
        raise ValueError(f"hardware.gpus[{index}].{exc}") from None
    if compute_cap and vendor != "nvidia":
        raise ValueError(f"hardware.gpus[{index}].compute_cap is an NVIDIA CUDA property; "
                         f"remove it from this {vendor} GPU")
    return GPU(name=str(g.get("name", "GPU")), vram_gb=float(g.get("vram_gb", 0)),
               uuid=str(g.get("uuid", "")), vendor=vendor, compute_cap=compute_cap)


def _run_probe(argv: list[str]) -> subprocess.CompletedProcess | None:
    """Run a read-only GPU probe; None when it cannot be executed at all."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def _nvidia_query(fields: list[str]) -> subprocess.CompletedProcess | None:
    return _run_probe(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])


def _detect_nvidia_gpus() -> tuple[GPU, ...]:
    if not shutil.which("nvidia-smi"):
        return ()
    # compute_cap tells the sizer which builds this card can run. Drivers that predate the field
    # reject the whole query, so retry with the fields every driver knows rather than lose the GPUs.
    out = _nvidia_query(["name", "memory.total", "uuid", "compute_cap"])
    if out is not None and out.returncode != 0:
        out = _nvidia_query(["name", "memory.total", "uuid"])
    if out is None:
        return ()
    if out.returncode != 0:
        # A present-but-broken nvidia-smi (driver/library mismatch) used to render CPU-only in
        # silence. Say so: the operator almost certainly has a GPU they expect to be used.
        reason = (out.stderr or out.stdout or "").strip() or f"exit {out.returncode}"
        print(f"[ordo] WARNING: nvidia-smi is installed but failed ({reason}); "
              "no NVIDIA GPU detected, the render will not use it")
        return ()
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            vram_gb = round(float(parts[1]) / 1024.0, 1)
        except ValueError:
            continue
        # uuid pins a service to a specific card under WSL2 (the only thing that works)
        uuid = parts[2] if len(parts) >= 3 else ""
        try:
            compute_cap = normalize_compute_cap(parts[3]) if len(parts) >= 4 else ""
        except ValueError:          # "[N/A]" and the like: unknown, not an error
            compute_cap = ""
        gpus.append(GPU(name=parts[0], vram_gb=vram_gb, uuid=uuid, vendor="nvidia", compute_cap=compute_cap))
    return tuple(gpus)


def _detect_amd_gpus() -> tuple[GPU, ...]:
    """AMD GPUs via rocm-smi. Experimental: the ROCm path has not run on real AMD hardware."""
    if not shutil.which("rocm-smi"):
        return ()
    out = _run_probe(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
    if out is None:
        return ()
    try:
        if out.returncode != 0:
            raise ValueError((out.stderr or "").strip() or f"exit {out.returncode}")
        cards = json.loads(out.stdout)
        gpus = []
        for key in sorted(cards):
            if not key.startswith("card"):
                continue
            # rocm-smi's key capitalisation varies between releases ("Card Series"/"Card series").
            info = {str(k).lower(): v for k, v in cards[key].items()}
            vram_gb = round(float(info["vram total memory (b)"]) / 1024**3, 1)
            name = str(info.get("card series") or info.get("card model") or "AMD GPU")
            gpus.append(GPU(name=name, vram_gb=vram_gb, vendor="amd"))
        return tuple(gpus)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"[ordo] WARNING: rocm-smi is installed but its output could not be read ({exc}); "
              "no AMD GPU detected")
        return ()


def _detect_apple_gpu(system: str, arch: str) -> tuple[GPU, ...]:
    """Apple Silicon's integrated GPU has no dedicated VRAM (vram_gb 0). Docker cannot reach Metal,
    so it never runs the chat backend; it is reported so the render can say why."""
    if system == "Darwin" and arch == "arm64":
        return (GPU(name="Apple Silicon GPU", vram_gb=0.0, vendor="apple"),)
    return ()


def _detect_arch() -> str:
    return normalize_arch(platform.machine())


def _detect_gpus() -> tuple[GPU, ...]:
    """Every GPU this host exposes, NVIDIA first. A missing probe means none of that vendor."""
    return _detect_nvidia_gpus() + _detect_amd_gpus() + _detect_apple_gpu(platform.system(), _detect_arch())


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
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) and m.ullTotalPhys:
                return round(m.ullTotalPhys / 1024**3, 1)
        except (AttributeError, OSError) as exc:  # narrow: ctypes/kernel32 unavailable only
            print(f"[ordo] WARNING: Windows RAM detection failed ({exc}); "
                  "ram_gb=0.0 disables RAM gating — set hardware.ram_gb in ordo.yaml")
    # 0.0 means UNKNOWN (RAM gate is waived by Plugin.fits); detection failure is loud above
    # rather than silent (audit P2-38).
    return 0.0


def detect() -> HardwareProfile:
    """Detect the real machine."""
    return HardwareProfile(
        gpus=_detect_gpus(),
        ram_gb=_detect_ram_gb(),
        cpu_cores=os.cpu_count() or 1,
        platform=platform.system() or "unknown",
        arch=_detect_arch(),
    )
