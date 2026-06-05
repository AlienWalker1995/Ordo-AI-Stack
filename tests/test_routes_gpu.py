from dashboard import routes_gpu


def test_capacity_ok_when_fits():
    assert routes_gpu.capacity_check(need_gb=20.0, gpu_total_gb=34.2) == {"ok": True, "reason": None}


def test_capacity_blocks_when_too_big():
    r = routes_gpu.capacity_check(need_gb=20.0, gpu_total_gb=8.6)
    assert r["ok"] is False
    assert "8.6" in r["reason"]


def test_estimate_service_vram_llm_uses_model_size():
    assert routes_gpu.estimate_service_vram_gb("llamacpp", model_size_gb=20.0) == 23.0


def test_estimate_service_vram_embed_is_small():
    assert routes_gpu.estimate_service_vram_gb("llamacpp-embed", model_size_gb=None) <= 2.0


def test_estimate_service_vram_comfyui():
    assert routes_gpu.estimate_service_vram_gb("comfyui", model_size_gb=None) >= 8.0
