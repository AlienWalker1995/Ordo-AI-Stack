"""The operator source (`ordo.yaml`) and its render in out/, as the control plane reads and writes them.

ONE write path: the control plane never hand-edits a rendered file. It writes the declarative
source (`write`, atomic) and renders from it (`render`); out/ is always a function of the source.
`substrate_conflict` refuses a render when out/ was last rendered from different inputs than this
process ships, and `secrets_present` reads which secrets out/secrets.env holds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import render
from ..render.plugins import PluginRegistry
from .responses import error


def enabled_ids(rendered: Any) -> set[str]:
    """Every plugin a render enabled. `plugins_enabled` lists only kind=service plugins; the
    enabled kind=mcp plugins are the ones behind its MCP servers."""
    return set(rendered.plugins_enabled) | {s["plugin_id"] for s in rendered.mcp_servers}


class StackSource:
    """Where the source and out/ live, and what this process renders them with."""

    def __init__(self, path: Path, catalog: Catalog, registry: PluginRegistry, out_dir: Path,
                 substrate_digest: str):
        self.path = path
        self.catalog = catalog
        self.registry = registry
        self.out_dir = out_dir
        # The digest of the render inputs this process ships (its baked copy, in the image).
        self.substrate_digest = substrate_digest

    def render(self) -> Any:
        """The render of the source as it is on disk now."""
        return self.render_source(Source.load(self.path))

    def render_source(self, source: Source) -> Any:
        """The render of a source that is not (yet) on disk."""
        return render(source, self.catalog, self.registry)

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def write(self, text: str) -> None:
        """Replace the operator source atomically (a temp file, then a rename): a read-only request
        rendering it on another thread (GET /status) sees the old file or the new one, never half."""
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.path)

    def substrate_conflict(self) -> dict[str, Any] | None:
        """A 409 payload when out/ was last rendered from different inputs than this process ships.

        Rendering over it would silently revert whatever the newer side changed (the image renders
        from its own baked copy of ordo/, catalog/ and the manifests). No manifest, or one written
        before renders recorded a digest, is allowed: this render then records ours.
        """
        try:
            recorded = self.recorded_substrate_digest()
        except (OSError, ValueError, AttributeError) as e:
            return error(409, f"cannot read {self.out_dir / 'manifest.json'} to check the render substrate "
                              f"({e}); re-render from the host checkout, then retry")
        if recorded is None or recorded == self.substrate_digest:
            return None
        return error(
            409,
            f"ops-controller's render substrate ({self.substrate_digest[:12]}) differs from the last "
            f"host render's ({str(recorded)[:12]}): the image is older or newer than the checkout that "
            "rendered out/, and a render here would silently change what that checkout rendered. "
            "Rebuild ordo/ops-controller from the checkout that rendered out/ (`ordo build "
            "ops-controller`), re-render, then `ordo recreate ops-controller`.",
            substrate_digest=self.substrate_digest, rendered_substrate_digest=recorded)

    def recorded_substrate_digest(self) -> str | None:
        """The substrate digest the last render recorded in out/manifest.json. None when there is
        no manifest or it was written before renders recorded one. Raises OSError, ValueError or
        AttributeError when the manifest cannot be read."""
        manifest_path = self.out_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("substrate_digest")
        return str(recorded) if recorded else None

    def secrets_present(self) -> set[str]:
        """Secret KEYS with a non-empty value in out/secrets.env (empty if the file is absent). Lets an
        enable request tell whether a service's secrets are provisioned, so a secret-dependent service
        is escalated to a host `make up` rather than started broken."""
        p = self.out_dir / "secrets.env"
        present: set[str] = set()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if v.strip().strip('"').strip("'"):
                    present.add(k.strip())
        return present
