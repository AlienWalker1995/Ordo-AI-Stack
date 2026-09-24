from dashboard import gpu_stats


def test_parse_smi_gpus_multi():
    csv = (
        "0, GPU-aaa, NVIDIA GeForce GTX 1070, 8192, 512, 11, 55\n"
        "1, GPU-bbb, NVIDIA GeForce RTX 5090, 32607, 20426, 1, 41\n"
    )
    gpus = gpu_stats.parse_smi_gpus(csv)
    assert len(gpus) == 2
    g = next(x for x in gpus if x["uuid"] == "GPU-bbb")
    assert g["index"] == 1
    assert g["name"] == "NVIDIA GeForce RTX 5090"
    assert g["vram_total_gb"] == 34.2
    assert g["vram_used_gb"] == 21.4
    assert g["vram_total_mib"] == 32607
    assert g["utilization_pct"] == 1
    assert g["temp_c"] == 41


def test_parse_smi_gpus_comma_in_name():
    csv = "0, GPU-x, NVIDIA RTX, Special, 16384, 100, 5, 50\n"
    gpus = gpu_stats.parse_smi_gpus(csv)
    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX, Special"
    assert gpus[0]["uuid"] == "GPU-x"
    assert gpus[0]["index"] == 0
    assert gpus[0]["vram_total_gb"] == 17.2
    assert gpus[0]["vram_used_gb"] == 0.1


def test_parse_smi_gpus_empty():
    assert gpu_stats.parse_smi_gpus("") == []



# On a CPU-only host the dashboard gets no `utility` reservation, so neither NVML nor nvidia-smi
# exists in its container. The hw-stat bar must degrade to gpu:null + gpus:[], never raise.
def _no_nvidia(monkeypatch):
    import builtins
    import subprocess

    real_import = builtins.__import__

    def import_without_pynvml(name, *args, **kwargs):
        if name == "pynvml":
            raise ImportError("pynvml")
        return real_import(name, *args, **kwargs)

    def missing_binary(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(builtins, "__import__", import_without_pynvml)
    monkeypatch.setattr(subprocess, "check_output", missing_binary)


def test_list_gpus_without_nvidia_smi_is_empty(monkeypatch):
    _no_nvidia(monkeypatch)
    assert gpu_stats.list_gpus() == {"gpus": [], "reachable": False}


def test_hardware_stats_without_a_gpu_returns_no_gpus(monkeypatch):
    import asyncio

    import dashboard.app as app
    _no_nvidia(monkeypatch)
    assert app._probe_gpu() is None
    stats = asyncio.run(app.hardware_stats())
    assert stats["gpu"] is None
    assert stats["gpus"] == []
