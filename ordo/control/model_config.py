"""The active chat model: what the source asks for and what the render resolved, and the switch.

A switch is drift-safe: it writes only the source's `model:` key, renders, and commits through the
post-render step (ordo/control/apply.py), which recreates what the render changed. It refuses
before writing anything when the chat service would load a file the models volume lacks.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import yaml

from ..render.config import Source
from ..render.models_volume import CHAT_SERVICE
from ..render.served_models import model_files
from .apply import RenderApply
from .responses import error
from .source import StackSource


class ModelConfig:
    """`GET /model-config` and `POST /model-config`."""

    def __init__(self, source: StackSource, applier: RenderApply,
                 model_volume_files: Callable[[], set[str] | None] | None):
        self.source = source
        self.applier = applier
        # Lists the file names in the models volume (None: it could not be listed). A switch checks
        # the target's files against it before writing anything; None = no volume to check.
        self.model_volume_files = model_volume_files

    def get(self) -> dict[str, Any]:
        src = Source.load(self.source.path)
        rc = self.source.render()
        mmproj = rc.env.get("LLAMACPP_MMPROJ") or ""
        return {
            "source_model": src.model,           # what the source asks for ("auto" or an id)
            "active_model": rc.model.id,          # what best-fit/override actually resolved to
            # The GGUF the resolved model serves. Consumers that key by file (throughput
            # attribution, the dashboard's installed check) need this, not the catalog id.
            "active_file": rc.model.file,
            # The vision projector llama.cpp loads beside it (a bare file name, like active_file).
            "active_mmproj": mmproj.rsplit("/", 1)[-1] or None,
            # Every file a rendered service loads (chat model + projector, CPU fallback, embed):
            # the dashboard's delete guard protects exactly these.
            "model_files": [{"file": f.file, "service": f.service, "optional": f.optional}
                            for f in model_files(rc.compose_dict(), rc.env)],
            "tier": rc.tier,
            "ctx_size": rc.ctx_size,
            "available": [
                {"id": m.id, "tier": m.tier, "vram_gb": m.vram_gb, "file": m.file}
                for m in self.source.catalog.models
            ],
        }

    def set(self, body: dict[str, Any]) -> dict[str, Any]:
        """Switch the active model the drift-safe way: write the SOURCE, re-render, then apply.

        `.env`, Hermes context, and model-gateway ctx are all regenerated from the new source in
        one pass — they cannot end up disagreeing. `model: "auto"` hands control back to best-fit.
        The render decides what restarts (`RenderApply.apply_render`): llama.cpp and the gateway, the CPU
        fallback and the agent when the context window changed, whatever else the render touched.
        The response's `apply` says what was recreated and what the host must finish.
        """
        model_id = str(body.get("model", "")).strip()
        if not model_id:
            return error(400, "body must include 'model' (a catalog id or 'auto')")
        if model_id != "auto" and self.source.catalog.get(model_id) is None:
            ids = [m.id for m in self.source.catalog.models]
            return error(404, f"model '{model_id}' not in catalog", available=ids)
        conflict = self.source.substrate_conflict()
        if conflict:
            return conflict

        # ONE write path: mutate only the model key of the raw source, preserving everything else.
        raw = yaml.safe_load(self.source.read_text()) or {}
        raw["model"] = model_id
        rc = self.source.render_source(Source.from_dict(raw))
        missing = self.missing_model_files(rc)
        if missing:
            return missing
        applied, failure = self.applier.commit(yaml.safe_dump(raw, sort_keys=False), rc)
        if failure:
            return failure
        return {"ok": True, "active_model": rc.model.id, "ctx_size": rc.ctx_size,
                "warnings": rc.warnings, "wrote": str(self.source.out_dir), "apply": applied}

    def missing_model_files(self, target: Any) -> dict[str, Any] | None:
        """A refusal when the chat service would load a file the models volume lacks, else None.

        Checked before the source is written: the post-render step recreates llama.cpp right after
        a switch, and onto a missing weights file it crash-loops (a missing projector leaves it running
        without vision while model-gateway advertises vision). The fix is deliberately NOT a
        download from here: tens of GB is the host's `ordo fetch` (resumable, preflighted for
        disk), and a download inside this process would die with every ops-controller recreate."""
        if self.model_volume_files is None:
            return None
        present = self.model_volume_files()
        if present is None:
            return error(503, "cannot list the models volume to confirm the model's files are in "
                              "place; not switching")
        needed = [f for f in model_files(target.compose_dict(), target.env) if f.service == CHAT_SERVICE]
        missing = [need.file for need in needed if need.file not in present]
        if not missing:
            return None
        command = f"ordo fetch {target.model.id}"
        verb = "is" if len(missing) == 1 else "are"
        return error(409, f"{', '.join(missing)} {verb} not in the models volume: run `{command}` on "
                          "the host, then switch again", missing_files=missing, fetch_command=command)
