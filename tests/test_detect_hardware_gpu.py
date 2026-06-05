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
