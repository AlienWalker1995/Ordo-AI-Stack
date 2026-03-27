#!/usr/bin/env python3
"""
Detect GPU hardware and generate docker-compose compute overrides.
Run before first `docker compose up` to auto-configure for best performance.

Detects: NVIDIA > AMD (ROCm) > Intel (XPU) > Apple Silicon (ARM64) > CPU fallback
Also detects host RAM and:
  - Writes .wslconfig with appropriate memory allocation (Windows/WSL)
  - Sets Ollama memory limit scaled to available RAM
  - Sets ComfyUI memory limit (GPU+lowvram needs more for LTX offload)
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Reserve this much RAM for Windows OS + non-Docker processes
OS_HEADROOM_GB = 8
# Reserve this much for all other containers combined (webui, n8n, gateway, etc.)
CONTAINER_HEADROOM_GB = 8


def run(cmd: list[str], check: bool = False) -> tuple[int, str]:
    """Run command, return (returncode, stdout+stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1, ""


def get_host_ram_gb() -> float:
    """
    Return actual physical host RAM in GB.
    Works from Windows Python, WSL, or native Linux.
    WSL /proc/meminfo only shows WSL allocation, so we query Windows directly when possible.
    """
    # psutil reports real physical RAM on all platforms
    try:
        import psutil
        return psutil.virtual_memory().total / (1024**3)
    except ImportError:
        pass

    # Windows Python: try PowerShell first (wmic is deprecated on Windows 11)
    if platform.system() == "Windows":
        code, out = run(["powershell", "-NoProfile", "-Command",
                         "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"])
        if code == 0 and out.strip().isdigit():
            return int(out.strip()) / (1024**3)
        # wmic fallback for older Windows
        code, out = run(["wmic", "computersystem", "get", "TotalPhysicalMemory", "/format:value"])
        if code == 0:
            for line in out.splitlines():
                if line.strip().startswith("TotalPhysicalMemory="):
                    try:
                        return int(line.split("=", 1)[1]) / (1024**3)
                    except (ValueError, IndexError):
                        pass

    # WSL / Linux: ask Windows via powershell for real RAM, not WSL view
    if platform.system() == "Linux":
        code, out = run(["powershell.exe", "-Command",
                         "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"])
        if code == 0 and out.strip().isdigit():
            return int(out.strip()) / (1024**3)

        # Native Linux fallback (not WSL)
        if Path("/proc/meminfo").exists():
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            return int(line.split()[1]) / (1024 * 1024)
            except (ValueError, OSError):
                pass

    return 16.0


def get_windows_home() -> Path | None:
    """Return Windows user home dir. Works from Windows Python or WSL."""
    if platform.system() == "Windows":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile)

    if platform.system() == "Linux":
        # Try wslvar (available in WSL)
        code, out = run(["wslvar", "USERPROFILE"])
        if code == 0 and out:
            code2, wsl_path = run(["wslpath", "-u", out.strip()])
            if code2 == 0 and wsl_path:
                return Path(wsl_path.strip())

        # Fallback: scan /mnt/c/Users for a single non-system user
        mnt = Path("/mnt/c/Users")
        system_dirs = {"Public", "Default", "Default User", "All Users"}
        if mnt.exists():
            users = [u for u in mnt.iterdir()
                     if u.is_dir() and not u.name.startswith(".") and u.name not in system_dirs]
            if len(users) == 1:
                return users[0]

    return None


def ollama_memory_limit(total_ram_gb: float) -> str:
    """
    Compute Ollama container memory limit.
    Ollama needs as much RAM as possible to hold large model weights that spill from VRAM.
    Leaves OS_HEADROOM_GB for Windows + CONTAINER_HEADROOM_GB for other containers.
    """
    available = int(total_ram_gb) - OS_HEADROOM_GB - CONTAINER_HEADROOM_GB
    result = max(6, available)
    return f"{result}G"


def comfyui_memory_limit(mode: str, total_ram_gb: float) -> str:
    """
    Compute ComfyUI container memory limit from mode and host RAM.
    GPU: LTX + VideoVAE can spike host RAM during load (mmap, pinned buffers); too low
    and the cgroup OOM killer SIGKILLs the process (docker logs: "Killed" after VideoVAE).
    """
    available = int(total_ram_gb) - OS_HEADROOM_GB
    if mode in ("nvidia", "amd", "intel"):
        # ~42% RAM, floor 32G, cap 96G — was 25%/48G max and caused OOM at VideoVAE on 128G hosts.
        target = max(32, min(96, int(total_ram_gb * 0.42)))
    else:
        target = max(4, min(16, int(total_ram_gb * 0.4)))
    return f"{min(target, available)}G"


def write_wslconfig(total_ram_gb: float) -> None:
    """
    Write (or update) ~/.wslconfig with memory sized to actual host RAM.
    Leaves OS_HEADROOM_GB for Windows. Only touches the memory= and swap= lines.
    """
    home = get_windows_home()
    if home is None:
        print("  Warning: could not find Windows home dir — skipping .wslconfig")
        return

    wsl_ram_gb = max(4, int(total_ram_gb) - OS_HEADROOM_GB)
    wslconfig_path = home / ".wslconfig"

    new_content = (
        "# Auto-generated by scripts/detect_hardware.py\n"
        "[wsl2]\n"
        f"# Leaves {OS_HEADROOM_GB} GB headroom for Windows (total: {int(total_ram_gb)} GB detected)\n"
        f"memory={wsl_ram_gb}GB\n"
        "swap=0\n"
        "\n"
        "[experimental]\n"
        "# Gradually return unused WSL memory to Windows\n"
        "autoMemoryReclaim=gradual\n"
    )

    if wslconfig_path.exists():
        existing = wslconfig_path.read_text(encoding="utf-8")
        if f"memory={wsl_ram_gb}GB" in existing:
            print(f"  .wslconfig already correct (memory={wsl_ram_gb}GB), skipping")
            return
        print(f"  Updating {wslconfig_path} -> memory={wsl_ram_gb}GB")
    else:
        print(f"  Writing {wslconfig_path} -> memory={wsl_ram_gb}GB")

    wslconfig_path.write_text(new_content, encoding="utf-8")
    print("  -> Run: wsl --shutdown   (then restart Docker Desktop to apply)")


def detect_nvidia() -> bool:
    """Check for NVIDIA GPU via nvidia-smi."""
    code, out = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    return code == 0 and bool(out)


def detect_amd() -> bool:
    """Check for AMD GPU (ROCm)."""
    if platform.system() != "Linux":
        return False
    if shutil.which("rocm-smi"):
        code, _ = run(["rocm-smi", "--showproductname"])
        return code == 0
    return Path("/dev/kfd").exists()


def detect_intel() -> bool:
    """Check for Intel GPU (XPU)."""
    if platform.system() != "Linux":
        return False
    return Path("/dev/dri").exists() and any(
        p.name.startswith("renderD") for p in Path("/dev/dri").iterdir()
    )


def detect_apple_silicon() -> bool:
    """Check for Apple Silicon (M1/M2/M3) on macOS."""
    if platform.system() != "Darwin":
        return False
    return platform.machine().lower() in ("arm64", "aarch64")


def detect() -> str:
    """Return: nvidia | amd | intel | apple_silicon | cpu."""
    if detect_nvidia():
        return "nvidia"
    if detect_amd():
        return "amd"
    if detect_intel():
        return "intel"
    if detect_apple_silicon():
        return "apple_silicon"
    return "cpu"


def ensure_comfyui_cli_args_in_env(env_path: Path, mode: str) -> None:
    """Append COMFYUI_CLI_ARGS for GPU modes when unset (explicit .env control of --normalvram vs --lowvram)."""
    if mode not in ("nvidia", "amd", "intel"):
        return
    content = env_path.read_text(encoding="utf-8")
    if re.search(r"^COMFYUI_CLI_ARGS=", content, re.MULTILINE):
        return
    line = "COMFYUI_CLI_ARGS=--disable-xformers --normalvram --enable-manager\n"
    env_path.write_text(content.rstrip() + "\n" + line, encoding="utf-8")
    print(f"  Appended COMFYUI_CLI_ARGS to {env_path}")


def update_env(env_path: Path, mode: str, sep: str) -> None:
    """Write COMPUTE_MODE and COMPOSE_FILE into .env, handling commented-out lines."""
    new_compose_file = f"COMPOSE_FILE=docker-compose.yml{sep}overrides/compute.yml"
    new_compute_mode = f"COMPUTE_MODE={mode}"

    content = env_path.read_text(encoding="utf-8")

    # Replace whether the line is active or commented out (e.g. "# COMPOSE_FILE=...")
    if re.search(r"^#?\s*COMPOSE_FILE=", content, re.MULTILINE):
        content = re.sub(r"^#?\s*COMPOSE_FILE=.*", new_compose_file, content, flags=re.MULTILINE)
    else:
        content = content.rstrip() + "\n" + new_compose_file + "\n"

    if re.search(r"^#?\s*COMPUTE_MODE=", content, re.MULTILINE):
        content = re.sub(r"^#?\s*COMPUTE_MODE=.*", new_compute_mode, content, flags=re.MULTILINE)
    else:
        content = content.rstrip() + "\n" + new_compute_mode + "\n"

    env_path.write_text(content, encoding="utf-8")
    print(f"  Updated {env_path} (COMPOSE_FILE, COMPUTE_MODE)")


def format_override(cfg: dict) -> str:
    lines = ["# Auto-generated by scripts/detect_hardware.py", "services:"]
    for svc, opts in cfg.items():
        if not opts:
            continue
        lines.append(f"  {svc}:")
        for k, v in opts.items():
            if v is None:
                continue
            if k == "image":
                lines.append(f"    image: {v}")
            elif k == "platform":
                lines.append(f"    platform: {v}")
            elif k == "environment":
                lines.append("    environment:")
                for ek, ev in v.items():
                    lines.append(f"      - {ek}={ev}")
            elif k == "deploy":
                lines.append("    deploy:")
                lines.append("      resources:")
                res = v.get("resources", {})
                if "limits" in res:
                    lines.append("        limits:")
                    for lk, lv in res["limits"].items():
                        lines.append(f"          {lk}: {lv}")
                if "reservations" in res and "devices" in res["reservations"]:
                    lines.append("        reservations:")
                    lines.append("          devices:")
                    for d in res["reservations"]["devices"]:
                        lines.append("            - driver: " + d["driver"])
                        lines.append("              count: " + str(d["count"]))
                        lines.append("              capabilities: " + str(d["capabilities"]))
            elif k == "devices":
                lines.append("    devices:")
                for d in v:
                    lines.append(f"      - {d}")
            elif k == "security_opt":
                lines.append("    security_opt:")
                for s in v:
                    lines.append(f"      - {s}")
    return "\n".join(lines)


def main() -> int:
    base = Path(os.environ.get("BASE_PATH", ".")).resolve()
    repo_root = Path(__file__).resolve().parent.parent
    if base == Path(".").resolve():
        base = repo_root

    mode = detect()
    ram_gb = get_host_ram_gb()

    # Check for manual overrides in .env
    env_path = base / ".env"
    comfyui_override = None
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("COMFYUI_MEMORY_LIMIT="):
                comfyui_override = stripped.split("=", 1)[1].strip().strip('"\'')

    ollama_mem = ollama_memory_limit(ram_gb)
    comfyui_mem = comfyui_override if comfyui_override else comfyui_memory_limit(mode, ram_gb)

    print(f"Detected: compute={mode}, RAM={ram_gb:.0f} GB")
    print(f"  Ollama limit : {ollama_mem}  (RAM minus {OS_HEADROOM_GB}GB OS + {CONTAINER_HEADROOM_GB}GB containers)")
    print(f"  ComfyUI limit: {comfyui_mem}")

    # Write .wslconfig (Windows/WSL only — skipped on macOS/native Linux)
    if platform.system() in ("Windows", "Linux"):
        print("WSL config:")
        write_wslconfig(ram_gb)

    # Compose overrides per GPU mode
    nvidia_gpu = [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
    overrides = {
        "nvidia": {
            "ollama": {
                "deploy": {
                    "resources": {
                        "limits": {"memory": ollama_mem},
                        "reservations": {"devices": nvidia_gpu},
                    }
                }
            },
            "dashboard": {
                "deploy": {
                    "resources": {
                        "reservations": {"devices": nvidia_gpu},
                    }
                }
            },
            "comfyui": {
                "image": "yanwk/comfyui-boot:cu128-slim",
                "environment": {
                    # normalvram: keep more weights on GPU (lowvram offloads heavily to CPU — slow LTX text encode).
                    "CLI_ARGS": "${COMFYUI_CLI_ARGS:---disable-xformers --normalvram --enable-manager}",
                    # Override in .env: PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True (no pinned) if LTX Gemma hits cudaErrorInvalidValue on GPU→CPU copy.
                    "PYTORCH_CUDA_ALLOC_CONF": "${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,pinned_use_cuda_host_register:True}",
                    "HF_TOKEN": "${HF_TOKEN:-}",
                    "GITHUB_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN:-}",
                },
                "deploy": {
                    "resources": {
                        "limits": {"memory": comfyui_mem},
                        "reservations": {"devices": nvidia_gpu},
                    }
                },
            },
        },
        "amd": {
            "ollama": {
                "image": "ollama/ollama:rocm",
                "deploy": {"resources": {"limits": {"memory": ollama_mem}}},
                "devices": ["/dev/kfd", "/dev/dri"],
                "security_opt": ["seccomp:unconfined"],
            },
            "comfyui": {
                "image": "yanwk/comfyui-boot:rocm",
                "environment": {
                    "CLI_ARGS": "${COMFYUI_CLI_ARGS:---disable-xformers --normalvram --enable-manager}",
                    "HF_TOKEN": "${HF_TOKEN:-}",
                    "GITHUB_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN:-}",
                },
                "deploy": {"resources": {"limits": {"memory": comfyui_mem}}},
                "devices": ["/dev/kfd", "/dev/dri"],
                "security_opt": ["seccomp:unconfined"],
            },
        },
        "intel": {
            "ollama": {
                "deploy": {"resources": {"limits": {"memory": ollama_mem}}},
            },
            "comfyui": {
                "image": "yanwk/comfyui-boot:xpu",
                "environment": {
                    "CLI_ARGS": "${COMFYUI_CLI_ARGS:---disable-xformers --normalvram --enable-manager}",
                    "HF_TOKEN": "${HF_TOKEN:-}",
                    "GITHUB_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN:-}",
                },
                "deploy": {"resources": {"limits": {"memory": comfyui_mem}}},
                "devices": ["/dev/dri"],
            },
        },
        "apple_silicon": {
            "ollama": {
                "deploy": {"resources": {"limits": {"memory": ollama_mem}}},
            },
            "comfyui": {
                "image": "thiagoin/comfyui:arm64",
                "platform": "linux/arm64",
                "environment": {
                    "CLI_ARGS": "${COMFYUI_CLI_ARGS:---cpu --enable-manager}",
                    "HF_TOKEN": "${HF_TOKEN:-}",
                    "GITHUB_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN:-}",
                },
                "deploy": {"resources": {"limits": {"memory": comfyui_mem}}},
            },
        },
        "cpu": {
            "ollama": {
                "deploy": {"resources": {"limits": {"memory": ollama_mem}}},
            },
            "comfyui": {
                "image": "yanwk/comfyui-boot:cpu",
                "environment": {
                    "CLI_ARGS": "${COMFYUI_CLI_ARGS:---cpu --enable-manager}",
                    "HF_TOKEN": "${HF_TOKEN:-}",
                    "GITHUB_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN:-}",
                },
                "deploy": {"resources": {"limits": {"memory": comfyui_mem}}},
            },
        },
    }

    override_content = format_override(overrides[mode])
    override_path = base / "overrides" / "compute.yml"
    override_path.parent.mkdir(exist_ok=True)
    override_path.write_text(override_content, encoding="utf-8")
    print("Compute override:")
    print(f"  Wrote {override_path}")

    # Update .env with COMPOSE_FILE and COMPUTE_MODE
    sep = ";" if platform.system() == "Windows" else ":"
    if env_path.exists():
        update_env(env_path, mode, sep)
        ensure_comfyui_cli_args_in_env(env_path, mode)
    else:
        env_compute = base / ".env.compute"
        gpu_cli = ""
        if mode in ("nvidia", "amd", "intel"):
            gpu_cli = "COMFYUI_CLI_ARGS=--disable-xformers --normalvram --enable-manager\n"
        env_compute.write_text(
            f"# Auto-generated\nCOMPUTE_MODE={mode}\nCOMPOSE_FILE=docker-compose.yml{sep}overrides/compute.yml\n{gpu_cli}",
            encoding="utf-8",
        )
        print(f"  Wrote {env_compute} (create .env from .env.example, then re-run)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
