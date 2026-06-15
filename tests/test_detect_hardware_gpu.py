import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "detect_hardware",
    Path(__file__).resolve().parent.parent / "scripts" / "detect_hardware.py",
)
detect_hardware = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect_hardware)


def test_parse_gpu_query_ranks_by_total_vram_desc():
    csv = (
        "GPU-1070uuid, NVIDIA GeForce GTX 1070, 8192\n"
        "GPU-5090uuid, NVIDIA GeForce RTX 5090, 32607\n"
    )
    gpus = detect_hardware.parse_gpu_query(csv)
    assert [g["uuid"] for g in gpus] == ["GPU-5090uuid", "GPU-1070uuid"]
    assert gpus[0]["name"] == "NVIDIA GeForce RTX 5090"
    assert gpus[0]["memory_total_mib"] == 32607


def test_parse_gpu_query_empty_returns_empty_list():
    assert detect_hardware.parse_gpu_query("") == []


def test_build_gpu_assignments_pins_services_to_biggest():
    gpus = [
        {"uuid": "GPU-5090uuid", "name": "RTX 5090", "memory_total_mib": 32607},
        {"uuid": "GPU-1070uuid", "name": "GTX 1070", "memory_total_mib": 8192},
    ]
    assigns = detect_hardware.build_gpu_assignments(gpus)
    # Primary compute services → biggest GPU
    assert assigns["llamacpp"] == "GPU-5090uuid"
    assert assigns["comfyui"] == "GPU-5090uuid"
    assert assigns["llamacpp-embed"] == "GPU-5090uuid"
    # Voice services → secondary GPU (GTX 1070, Pascal, int8-friendly)
    assert assigns["stt"] == "GPU-1070uuid"
    assert assigns["tts"] == "GPU-1070uuid"


def test_build_gpu_assignments_single_gpu_voice_falls_back_to_primary():
    """With one GPU, voice services share it with primary compute."""
    gpus = [{"uuid": "GPU-onlyuuid", "name": "RTX 4090", "memory_total_mib": 24576}]
    assigns = detect_hardware.build_gpu_assignments(gpus)
    assert assigns["stt"] == "GPU-onlyuuid"
    assert assigns["tts"] == "GPU-onlyuuid"
    assert assigns["llamacpp"] == "GPU-onlyuuid"


def test_build_gpu_assignments_no_gpus_is_empty():
    assert detect_hardware.build_gpu_assignments([]) == {}


def test_format_gpu_assignments_emits_device_ids_yaml():
    text = detect_hardware.format_gpu_assignments({"llamacpp": "GPU-5090uuid"})
    # Header comment emitted by canonical formatter (ops-controller/gpu_assignments_fmt.py)
    assert "services:" in text
    assert "  llamacpp:" in text
    # Canonical emitter uses single quotes for device_ids (valid YAML; double-quote
    # legacy files are handled by the parser)
    assert "device_ids: ['GPU-5090uuid']" in text
    assert "capabilities: ['gpu']" in text
    assert "count:" not in text


def test_format_gpu_assignments_emits_cuda_visible_devices():
    # WSL2/Docker Desktop ignores container-level GPU isolation; the
    # CUDA_VISIBLE_DEVICES env is what actually pins the process to the GPU.
    import yaml as _yaml
    text = detect_hardware.format_gpu_assignments({"llamacpp": "GPU-5090uuid"})
    assert "CUDA_VISIBLE_DEVICES=GPU-5090uuid" in text
    assert "NVIDIA_VISIBLE_DEVICES=GPU-5090uuid" in text
    # must be valid YAML and the env must merge as a list (matches base compose)
    doc = _yaml.safe_load(text)
    env = doc["services"]["llamacpp"]["environment"]
    assert "CUDA_VISIBLE_DEVICES=GPU-5090uuid" in env
    assert "NVIDIA_VISIBLE_DEVICES=GPU-5090uuid" in env


def test_nvidia_compute_override_has_no_gpu_compute_reservations():
    overrides = detect_hardware.build_overrides(
        llamacpp_mem="100G", comfyui_mem="42G",
        embed_mem="6G", common_sidecars={},
    )
    text = detect_hardware.format_override(overrides["nvidia"])
    assert "device_ids" not in text
    # Primary compute services (llamacpp/embed/comfyui) must have NO gpu-compute
    # reservations here — those moved to overrides/gpu-assignments.yml to avoid
    # Docker Compose sequence-concatenation conflicts.
    # Voice services (stt/tts) DO get a gpu compute reservation in compute.yml
    # (pinned UUID comes from gpu-assignments.yml; capability stanza lives here).
    # Dashboard + ops-controller have utility-only reservations.
    assert "capabilities: ['utility']" in text
    assert "mem_limit: 100G" in text  # llamacpp mem limit still present
    # llamacpp/embed/comfyui must NOT have per-service gpu compute reservations
    # (confirmed by absence of device_ids — those only appear in gpu-assignments.yml)
    assert "device_ids" not in text
    # voice services bring gpu compute capability into compute.yml
    assert "  stt:" in text
    assert "  tts:" in text


def test_update_env_appends_gpu_assignments_after_compute(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    detect_hardware.update_env(env, mode="nvidia", sep=";", gpu_assignments=True)
    content = env.read_text(encoding="utf-8")
    assert "COMPOSE_FILE=docker-compose.yml;overrides/compute.yml;overrides/gpu-assignments.yml" in content


def test_update_env_no_gpu_assignments_for_cpu(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    detect_hardware.update_env(env, mode="cpu", sep=":")
    content = env.read_text(encoding="utf-8")
    assert "gpu-assignments.yml" not in content
    assert "COMPOSE_FILE=docker-compose.yml:overrides/compute.yml" in content


def test_update_env_nvidia_without_assignments_omits_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    detect_hardware.update_env(env, mode="nvidia", sep=";", gpu_assignments=False)
    content = env.read_text(encoding="utf-8")
    assert "gpu-assignments.yml" not in content
    assert "COMPOSE_FILE=docker-compose.yml;overrides/compute.yml" in content


def test_parse_gpu_query_handles_comma_in_name():
    csv = "GPU-x, NVIDIA RTX, Special Edition, 16384\n"
    gpus = detect_hardware.parse_gpu_query(csv)
    assert len(gpus) == 1
    assert gpus[0]["uuid"] == "GPU-x"
    assert gpus[0]["name"] == "NVIDIA RTX, Special Edition"
    assert gpus[0]["memory_total_mib"] == 16384
