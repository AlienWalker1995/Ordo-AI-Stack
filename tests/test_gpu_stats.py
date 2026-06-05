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
