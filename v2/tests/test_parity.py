"""Parity check — the render engine reproduces a reference .env (merge-gate a)."""
from pathlib import Path

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.parity import diff, load_env, report
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}


def _ultra_env():
    src = Source.from_dict({"hardware": P_5090, "tier": "auto", "model": "auto", "plugins": "auto"})
    return render(src, CATALOG).env


def test_full_llamacpp_surface_rendered():
    env = _ultra_env()
    for k in ["LLAMACPP_MODEL", "LLAMACPP_CTX_SIZE", "LLAMACPP_MMPROJ", "LLAMACPP_EXTRA_ARGS",
              "LLAMACPP_N_PREDICT", "LLAMACPP_FLASH_ATTN", "LLAMACPP_KV_CACHE_TYPE_K"]:
        assert k in env
    # the ultra model carries its MTP spec-decode args + vision projector
    assert "draft-mtp" in env["LLAMACPP_EXTRA_ARGS"]
    assert env["LLAMACPP_MMPROJ"].endswith("mmproj-Huihui-Q6_K.gguf")


def test_parity_roundtrip(tmp_path):
    env = _ultra_env()
    ref = tmp_path / ".env"
    ref.write_text("\n".join(f"{k}={v}" for k, v in env.items()))
    ok, mism, compared = report(env, ref)
    assert ok and mism == {}
    assert "LLAMACPP_CTX_SIZE" in compared


def test_parity_detects_drift(tmp_path):
    env = _ultra_env()
    ref = tmp_path / ".env"
    # simulate the exact drift that started all this: a stale ctx in the deployed config
    lines = [f"{k}={'999999' if k == 'LLAMACPP_CTX_SIZE' else v}" for k, v in env.items()]
    ref.write_text("\n".join(lines))
    ok, mism, _ = report(env, ref)
    assert not ok
    assert "LLAMACPP_CTX_SIZE" in mism
    assert mism["LLAMACPP_CTX_SIZE"]["reference"] == "999999"


def test_only_compares_shared_keys():
    rendered = {"A": "1", "B": "2"}
    reference = {"A": "1", "C": "3"}          # B not in ref, C not rendered
    assert diff(rendered, reference) == {}    # only A compared, and it matches


def test_load_env_skips_comments_and_blanks(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\n\nFOO=bar\n  BAZ = qux \n")
    env = load_env(p)
    assert env == {"FOO": "bar", "BAZ": "qux"}
