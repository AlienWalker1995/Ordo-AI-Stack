"""The dashboard names the GPU chat service after the engine that actually serves it.

The card used to read "llama.cpp (GPU)" whatever the catalog model's backend was, so with an NInfer
model the Services page named the wrong engine. The render now names the card from the active
model's backend (ordo/render/engine.py, aggregate_services_catalog)."""
from __future__ import annotations

from ordo.render.engine import aggregate_services_catalog, gpu_engine_label


def card(catalog: dict, card_id: str) -> dict:
    return next(c for c in catalog["services"] if c["id"] == card_id)


def test_the_card_names_the_ninfer_engine():
    assert card(aggregate_services_catalog(gpu_engine="ninfer"), "llamacpp")["name"] == "NInfer (GPU)"


def test_the_card_names_llama_cpp_for_a_gguf_model():
    assert card(aggregate_services_catalog(gpu_engine="llama.cpp"), "llamacpp")["name"] == "llama.cpp (GPU)"


def test_other_cards_are_untouched():
    plain, ninfer = aggregate_services_catalog(), aggregate_services_catalog(gpu_engine="ninfer")
    assert card(plain, "llamacpp-cpu") == card(ninfer, "llamacpp-cpu")


def test_labels():
    assert gpu_engine_label("ninfer") == "NInfer"
    assert gpu_engine_label("llama.cpp") == "llama.cpp"
    assert gpu_engine_label("something-new") == "something-new"
