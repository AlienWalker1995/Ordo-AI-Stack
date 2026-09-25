"""The chat backend runs on the llama.cpp build the host's compute can actually run.

Three layers, each tested here against the compute targets of the 2026-09 portability audit:
  - detection (ordo.hardware): GPU vendor, NVIDIA compute capability and CPU arch, degrading to
    none/unknown when nvidia-smi / rocm-smi are absent or fail;
  - backend selection (ordo.llamacpp_backend): (vendor, compute capability, arch) -> one pinned
    upstream llama.cpp server image;
  - sizing + render: a model whose `backend_image` is a special build is only picked on a GPU with
    the compute capability that build targets, and compose wires each backend's devices (NVIDIA
    reservations only for NVIDIA).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ordo import hardware, llamacpp_backend, wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import GPU, HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
RENDER_MODULE = sys.modules["ordo.render"]

UPSTREAM_IMAGE = re.compile(r"^ghcr\.io/ggml-org/llama\.cpp:server(-[a-z0-9]+)?-b(\d+)@sha256:[0-9a-f]{64}$")
PATCHED_IMAGE = "ordo/llamacpp-patched"


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── detection ─────────────────────────────────────────────────────────────────

def test_nvidia_detection_reads_compute_capability(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return _completed(0, "NVIDIA GeForce GTX 1070, 8192, GPU-aaa, 6.1\n"
                             "NVIDIA GeForce RTX 5090, 32607, GPU-bbb, 12.0\n")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    gpus = hardware._detect_nvidia_gpus()
    assert "compute_cap" in calls[0][1]
    assert gpus == (GPU(name="NVIDIA GeForce GTX 1070", vram_gb=8.0, uuid="GPU-aaa", vendor="nvidia", compute_cap="6.1"),
                    GPU(name="NVIDIA GeForce RTX 5090", vram_gb=31.8, uuid="GPU-bbb", vendor="nvidia", compute_cap="12.0"))


def test_nvidia_detection_without_compute_cap_field_keeps_the_gpus(monkeypatch):
    # Drivers older than the compute_cap query field reject the WHOLE query. Losing every GPU over
    # one unknown field would silently render the host CPU-only; retry without it instead.
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)

    def fake_run(argv, **_kwargs):
        if "compute_cap" in argv[1]:
            return _completed(2, stderr='Field "compute_cap" is not a valid field to query.')
        return _completed(0, "Tesla T4, 15360, GPU-ccc\n")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    assert hardware._detect_nvidia_gpus() == (GPU(name="Tesla T4", vram_gb=15.0, uuid="GPU-ccc", compute_cap=""),)


def test_nvidia_smi_that_fails_is_reported_not_silent(monkeypatch, capsys):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(hardware.subprocess, "run",
                        lambda *_a, **_k: _completed(9, stderr="Failed to initialize NVML: Driver/library version mismatch"))
    assert hardware._detect_nvidia_gpus() == ()
    assert "Driver/library version mismatch" in capsys.readouterr().out


def test_amd_detection_without_rocm_smi_finds_nothing(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: None)
    assert hardware._detect_amd_gpus() == ()


def test_amd_detection_reads_rocm_smi_json(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/opt/rocm/bin/rocm-smi" if name == "rocm-smi" else None)
    payload = ('{"card0": {"VRAM Total Memory (B)": "25753026560", "VRAM Total Used Memory (B)": "1",'
               ' "Card Series": "Radeon RX 7900 XTX", "Card Vendor": "Advanced Micro Devices, Inc. [AMD/ATI]"}}')
    monkeypatch.setattr(hardware.subprocess, "run", lambda *_a, **_k: _completed(0, payload))
    assert hardware._detect_amd_gpus() == (GPU(name="Radeon RX 7900 XTX", vram_gb=24.0, vendor="amd"),)


def test_amd_detection_with_unparseable_output_degrades(monkeypatch, capsys):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/opt/rocm/bin/rocm-smi" if name == "rocm-smi" else None)
    monkeypatch.setattr(hardware.subprocess, "run", lambda *_a, **_k: _completed(0, "not json"))
    assert hardware._detect_amd_gpus() == ()
    assert "rocm-smi" in capsys.readouterr().out


@pytest.mark.parametrize("machine, arch", [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("amd64", "x86_64"),
                                           ("aarch64", "arm64"), ("arm64", "arm64"), ("ARM64", "arm64"),
                                           ("riscv64", "unknown"), ("", "unknown")])
def test_detected_arch_is_normalised(monkeypatch, machine, arch):
    monkeypatch.setattr(hardware.platform, "machine", lambda: machine)
    assert hardware._detect_arch() == arch


def test_detect_with_no_gpu_tools_is_cpu_only(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: None)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "aarch64")
    hw = hardware.detect()
    assert hw.gpus == () and hw.arch == "arm64" and hw.gpu_vendor == "none"


def test_detect_on_apple_silicon_reports_an_apple_gpu(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: None)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "arm64")
    hw = hardware.detect()
    assert hw.gpu_vendor == "apple" and hw.arch == "arm64"
    assert not hw.primary_is_nvidia


# ── declared hardware (ordo.yaml `hardware:`) ────────────────────────────────

def test_declared_gpu_defaults_to_nvidia_with_unknown_compute_capability():
    hw = HardwareProfile.from_spec({"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "arch": "x86_64"})
    assert hw.primary_gpu.vendor == "nvidia" and hw.primary_gpu.compute_cap == ""
    assert hw.primary_is_nvidia


@pytest.mark.parametrize("declared, normalised", [(12.0, "12.0"), (8.9, "8.9"), ("8.6", "8.6"), (12, "12.0"), ("7", "7.0")])
def test_declared_compute_capability_is_normalised(declared, normalised):
    hw = HardwareProfile.from_spec({"gpus": [{"vram_gb": 24, "compute_cap": declared}]})
    assert hw.primary_gpu.compute_cap == normalised


@pytest.mark.parametrize("spec, message", [
    ({"gpus": [{"vram_gb": 24, "vendor": "matrox"}]}, "vendor"),
    ({"gpus": [{"vram_gb": 24, "compute_cap": "sm_89"}]}, "compute_cap"),
    ({"gpus": [{"vram_gb": 24, "vendor": "amd", "compute_cap": "8.9"}]}, "compute_cap"),
    ({"arch": "sparc"}, "arch"),
])
def test_declared_hardware_rejects_values_it_cannot_act_on(spec, message):
    with pytest.raises(ValueError, match=message):
        HardwareProfile.from_spec(spec)


def test_source_accepts_the_new_hardware_keys():
    src = Source.from_dict({"hardware": {"arch": "arm64", "gpus": [
        {"name": "RTX 4090", "vram_gb": 24, "vendor": "nvidia", "compute_cap": "8.9", "uuid": "GPU-x"}]}})
    assert src.hardware["arch"] == "arm64"


# ── backend selection ────────────────────────────────────────────────────────

def _hw(arch: str = "x86_64", **gpu) -> HardwareProfile:
    gpus = [gpu] if gpu else []
    return HardwareProfile.from_spec({"gpus": gpus, "ram_gb": 64, "arch": arch})


@pytest.mark.parametrize("hw, backend", [
    (_hw(), "cpu"),
    (_hw(arch="arm64"), "cpu"),
    (_hw(vram_gb=8, compute_cap="6.1"), "cuda"),
    (_hw(vram_gb=32, compute_cap="12.0"), "cuda"),
    (_hw(arch="arm64", vram_gb=96, compute_cap="12.1"), "cuda"),
    (_hw(vram_gb=24), "cuda"),                        # NVIDIA, capability not known
    (_hw(vram_gb=4, compute_cap="3.5"), "cpu"),        # Kepler: below the CUDA build's floor
    (_hw(vram_gb=24, vendor="amd"), "rocm"),
    (_hw(arch="arm64", vram_gb=24, vendor="amd"), "vulkan"),  # no ROCm image for arm64
    (_hw(vram_gb=16, vendor="intel"), "vulkan"),
    (_hw(arch="arm64", vram_gb=36, vendor="apple"), "cpu"),   # no Metal inside Docker
])
def test_backend_selection(hw, backend):
    chosen, _notes = llamacpp_backend.select(hw)
    assert chosen.name == backend
    assert chosen.accelerated is (backend != "cpu")


def test_every_backend_image_is_one_pinned_upstream_build():
    builds = set()
    for backend in llamacpp_backend.BACKENDS.values():
        match = UPSTREAM_IMAGE.match(backend.image)
        assert match, f"{backend.name}: {backend.image} is not a digest-pinned upstream build tag"
        builds.add(match.group(2))
    assert len(builds) == 1, f"backends pin different llama.cpp builds: {sorted(builds)}"


def test_cpu_backend_is_the_same_digest_the_cpu_manifests_run():
    # llamacpp-cpu and llamacpp-embed pin the CPU server build; the chat backend's CPU choice must
    # be that same image, not a second CPU build that can drift from it.
    digest = llamacpp_backend.CPU.image.split("@", 1)[1]
    for manifest in ("llamacpp-cpu", "rag"):
        text = (ROOT / "services" / manifest / "plugin.yaml").read_text(encoding="utf-8")
        assert f"ghcr.io/ggml-org/llama.cpp:server@{digest}" in text


def test_special_builds_declare_the_compute_capability_they_need():
    # A model that pins a non-upstream build must say which GPUs that build runs on, or the sizer
    # will hand it to any card with enough VRAM (the L40S / sm_89 case).
    for model in CATALOG.models:
        if model.backend_image:
            assert model.min_compute_cap, f"{model.id} pins {model.backend_image} without requires.min_compute_cap"


def test_patched_image_requirement_matches_what_its_dockerfile_builds():
    dockerfile = (ROOT / "services" / "llamacpp-patched" / "Dockerfile").read_text(encoding="utf-8")
    archs = re.search(r'GGML_CUDA_ARCHITECTURES="([^"]+)"', dockerfile).group(1).split(";")
    lowest = min(archs, key=lambda a: tuple(int(p) for p in a.split(".")))
    for model in CATALOG.models:
        if model.backend_image == PATCHED_IMAGE:
            assert model.min_compute_cap == lowest


def test_patched_build_context_is_lf_only():
    # The Dockerfile's python heredoc and the git-applied diff run on Linux: a CRLF checkout on
    # Windows made the diff stop applying. .gitattributes keeps the whole context LF.
    context = ROOT / "services" / "llamacpp-patched"
    for path in sorted(p for p in context.rglob("*") if p.is_file()):
        assert b"\r" not in path.read_bytes(), f"{path.relative_to(ROOT)} has CR line endings"


def test_patched_build_pins_its_web_ui_instead_of_downloading_latest():
    # Left alone, llama.cpp's build fetches the embedded web UI for build "b<commit count>", which a
    # shallow clone reports as b1, so it silently used the floating "latest" UI; a newer UI broke
    # the pinned commit's embed step. The UI must come from a pinned, checksummed release.
    dockerfile = (ROOT / "services" / "llamacpp-patched" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG LLAMA_UI_RELEASE=b\d+$", dockerfile, re.M)
    assert re.search(r"^ARG LLAMA_UI_SHA256=[0-9a-f]{64}$", dockerfile, re.M)
    assert "sha256sum -c" in dockerfile
    assert "-DLLAMA_USE_PREBUILT_UI=OFF" in dockerfile


# ── the audit's compute targets, end to end ─────────────────────────────────

def _gpu(name, vram_gb, uuid, compute_cap=None, vendor=None):
    g = {"name": name, "vram_gb": vram_gb, "uuid": uuid}
    if compute_cap is not None:
        g["compute_cap"] = compute_cap
    if vendor is not None:
        g["vendor"] = vendor
    return g


TARGETS = {
    # name: (hardware, backend, model, image)
    "cpu-x86_64": ({"gpus": [], "ram_gb": 64, "arch": "x86_64"},
                   "cpu", "qwen2.5-3b-instruct-q4", None),
    "cpu-arm64": ({"gpus": [], "ram_gb": 64, "arch": "arm64"},
                  "cpu", "qwen2.5-3b-instruct-q4", None),
    "nvidia-8gb-sm61": ({"gpus": [_gpu("GTX 1070", 8, "GPU-1070", "6.1")], "ram_gb": 32, "arch": "x86_64"},
                        "cuda", "qwen2.5-3b-instruct-q4", None),
    "nvidia-12gb-sm86": ({"gpus": [_gpu("RTX 3060", 12, "GPU-3060", "8.6")], "ram_gb": 32, "arch": "x86_64"},
                         "cuda", "qwen2.5-7b-instruct-q4", None),
    "nvidia-24gb-sm89": ({"gpus": [_gpu("RTX 4090", 24, "GPU-4090", "8.9")], "ram_gb": 64, "arch": "x86_64"},
                         "cuda", "qwen2.5-14b-instruct-q5", None),
    "nvidia-32gb-sm120": ({"gpus": [_gpu("RTX 5090", 32, "GPU-5090", "12.0")], "ram_gb": 128, "arch": "x86_64"},
                          "cuda", "qwen3.8-27b-turbo-fable-q6", PATCHED_IMAGE),
    "nvidia-45gb-sm89-l40s": ({"gpus": [_gpu("L40S", 45, "GPU-l40s", "8.9")], "ram_gb": 64, "arch": "x86_64"},
                              "cuda", "qwen2.5-14b-instruct-q5", None),
    "nvidia-32gb-unknown-cc": ({"gpus": [_gpu("RTX 5090", 32, "GPU-5090")], "ram_gb": 128, "arch": "x86_64"},
                               "cuda", "qwen2.5-14b-instruct-q5", None),
    "dual-5090-1070": ({"gpus": [_gpu("RTX 5090", 32, "GPU-5090", "12.0"), _gpu("GTX 1070", 8, "GPU-1070", "6.1")],
                        "ram_gb": 128, "arch": "x86_64"},
                       "cuda", "qwen3.8-27b-turbo-fable-q6", PATCHED_IMAGE),
    "amd-24gb": ({"gpus": [_gpu("Radeon RX 7900 XTX", 24, "", vendor="amd")], "ram_gb": 64, "arch": "x86_64"},
                 "rocm", "qwen2.5-14b-instruct-q5", None),
    "apple-36gb": ({"gpus": [_gpu("Apple M3 Max", 36, "", vendor="apple")], "ram_gb": 36, "arch": "arm64"},
                   "cpu", "qwen2.5-3b-instruct-q4", None),
}


def _nvidia_devices(service: dict) -> list[dict]:
    devices = (((service.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") or []
    return [d for d in devices if d.get("driver") == "nvidia"]


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, text=True).returncode == 0


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_compute_target(target, tmp_path, monkeypatch):
    spec, backend_name, model_id, special_image = TARGETS[target]
    hw = HardwareProfile.from_spec(spec)
    backend, _notes = llamacpp_backend.select(hw)
    assert backend.name == backend_name

    # The headless install on this hardware, exactly as `ordo init --yes` + `ordo render` run it.
    monkeypatch.setattr(wizard, "detect", lambda: hw)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: hw)
    out = tmp_path / "out"
    result = wizard.run(CATALOG, REGISTRY, out, interactive=False, answers={}, host_root=tmp_path / "repo")
    rc = render(Source.load(result.source_path), CATALOG, REGISTRY)
    rc.write(out)

    assert rc.model.id == model_id
    assert CATALOG.fits(rc.model, hw), f"{model_id} was chosen but does not fit {target}"
    expected_image = special_image or backend.image
    assert rc.env["LLAMACPP_IMAGE"] == expected_image
    assert rc.env["LLAMACPP_GPU_LAYERS"] == ("-1" if backend.accelerated else "0")

    c = yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))
    llamacpp = c["services"]["llamacpp"]
    # a special build is first-party, so render pins its recorded tag (`current` before a build)
    assert llamacpp["image"] == (f"{special_image}:current" if special_image else backend.image)
    if backend_name == "cuda":
        assert _nvidia_devices(llamacpp)
        assert "devices" not in llamacpp
    else:
        assert not _nvidia_devices(llamacpp)
        assert llamacpp.get("devices", []) == list(backend.devices)
    if not hw.primary_is_nvidia:
        # No NVIDIA device request anywhere: Docker refuses to create a container that asks for
        # a driver the host does not have.
        offenders = sorted(name for name, svc in c["services"].items() if _nvidia_devices(svc))
        assert offenders == []

    if not _docker_compose_available():
        return
    check = subprocess.run(
        ["docker", "compose", "-f", str(out / "docker-compose.yml"),
         "--env-file", str(out / ".env"), "--env-file", str(out / "secrets.env"), "config", "-q"],
        capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_rocm_backend_passes_the_amd_device_nodes():
    assert llamacpp_backend.BACKENDS["rocm"].devices == ("/dev/kfd", "/dev/dri")
    assert llamacpp_backend.BACKENDS["vulkan"].devices == ("/dev/dri",)


def test_non_blackwell_card_is_told_why_the_ultra_tier_was_skipped():
    rc = render(Source.from_dict({"hardware": TARGETS["nvidia-45gb-sm89-l40s"][0], "plugins": []}), CATALOG, REGISTRY)
    assert any("compute capability" in w and "12.0" in w and "8.9" in w for w in rc.warnings), rc.warnings


def test_unknown_compute_capability_says_how_to_declare_it():
    rc = render(Source.from_dict({"hardware": TARGETS["nvidia-32gb-unknown-cc"][0], "plugins": []}), CATALOG, REGISTRY)
    assert any("compute_cap" in w for w in rc.warnings), rc.warnings


def test_explicit_special_build_model_on_the_wrong_gpu_warns():
    src = Source.from_dict({"hardware": TARGETS["nvidia-45gb-sm89-l40s"][0], "plugins": [],
                            "model": "qwen3.8-27b-turbo-fable-q6"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.id == "qwen3.8-27b-turbo-fable-q6"   # an explicit pick is honoured...
    assert any("compute capability" in w for w in rc.warnings)   # ...but never silently


def test_amd_host_disables_nvidia_only_plugins():
    rc = render(Source.from_dict({"hardware": TARGETS["amd-24gb"][0], "plugins": ["comfyui", "rag"]}),
                CATALOG, REGISTRY)
    assert "comfyui" not in rc.plugins_enabled and "rag" in rc.plugins_enabled
