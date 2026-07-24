"""Preflight — a read-only GO / NO-GO readiness check before bringing a stack up.

"Is it safe to deploy this config yet?" should be a command, not a vibe. `ordo preflight`
renders the target config and checks every gate we can verify WITHOUT starting anything:

  - config renders and the one ctx value is consistent across all consumers (the drift gate),
  - the active model is sha256-pinned (corrupt-weights gate) and MCP images are digest-pinned,
  - if a GPU is expected for the enabled media/voice plugins, one is actually present,
  - parity vs a reference .env (merge-gate (a)) when a --ref is given,
  - every image the rendered compose needs is available: project images (ordo/*) must be
    built locally (blocking); upstream images (llama.cpp, litellm, …) may be absent — Docker
    pulls them (a note, not a blocker).

Blocking checks failing = NO-GO. Non-blocking = a warning you can proceed past knowingly.
Pure logic here (docker/image presence is injected); the CLI wires the real `docker images`.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from . import parity
from .catalog import Catalog
from .config import Source
from .plugins import PluginRegistry
from .render import render

# ${VAR} or ${VAR:-default} — the compose interpolation syntax a plugin image ref may carry
# (e.g. `${COMFYUI_IMAGE:-yanwk/comfyui-boot:cu128-slim}`). Resolved against the rendered .env
# (with the `:-default` fallback) so the image-presence check compares the ACTUAL resolved ref.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(image: str, env: dict[str, str]) -> str:
    def sub(m: "re.Match[str]") -> str:
        val = env.get(m.group(1))
        return val if val not in (None, "") else (m.group(2) or "")
    return _VAR_RE.sub(sub, image)


@dataclasses.dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def required_images(rc, project: str = "ordo") -> list[str]:
    """The exact images the rendered compose will need (core + agent + enabled plugins), with
    ${VAR:-default} refs expanded against the rendered .env so presence-matching is accurate."""
    c = rc.compose_dict(project=project)
    return sorted({_expand(svc["image"], rc.env) for svc in c["services"].values()})


def _is_buildable(image: str, project: str) -> bool:
    """True for images this repo builds locally (so a registry pull can NOT provide them).

    Project images (`<project>/*`) are always local. The patched llama.cpp build is also
    local-only — it has a docker/ build context but no registry to pull from — so a missing
    one is 'build first', not 'Docker will pull'.
    """
    return image.startswith(f"{project}/") or "llamacpp-patched" in image


def _missing_secret_keys(rc, secrets_env: str) -> list[str]:
    """Keys the enabled stack requires that a present secrets.env leaves empty/absent."""
    present = {k for k, v in parity.load_env(secrets_env).items() if v}
    return [k for k in rc.required_secrets if k not in present]


def run(
    source: Source, catalog: Catalog, registry: PluginRegistry, *,
    ref_env: str | None = None,
    images_present: set[str] | None = None,
    secrets_env: str | None = None,
    project: str = "ordo",
) -> tuple[bool, list[Check]]:
    rc = render(source, catalog, registry)
    checks: list[Check] = []

    # 1. drift gate — one ctx value everywhere
    derived = rc.manifest()["derived"]
    consistent = len({str(v) for v in derived.values()}) == 1
    checks.append(Check("config renders + ctx consistent across .env/hermes/model-gateway",
                        consistent, f"ctx={rc.ctx_size:,}" if consistent else str(derived)))

    # 2. corrupt-weights gate — the chosen model is checksum-pinned
    checks.append(Check(f"active model '{rc.model.id}' is sha256-pinned",
                        rc.model.sha256 is not None,
                        "pinned" if rc.model.sha256 else "NO sha256 — download refuses unless --allow-unverified",
                        blocking=False))

    # 3. MCP images digest-pinned (drift/leak gate) — warn per unpinned PUBLIC server. Locally-built
    # project images (ordo/*) are pinned by build context, not a registry digest, so exempt.
    unpinned_mcp = [s["id"] for s in rc.mcp_servers
                    if not str(s.get("image", "")).startswith(f"{project}/")
                    and ("@sha256:" not in str(s.get("image", ""))
                         or len(set(str(s["image"]).split("@sha256:")[-1])) <= 1)]
    checks.append(Check("all enabled MCP images digest-pinned", not unpinned_mcp,
                        "all pinned" if not unpinned_mcp else f"placeholder/unpinned: {', '.join(unpinned_mcp)}",
                        blocking=False))

    # 4. GPU present if media/voice plugins are enabled
    gpu_plugins = [p for p in rc.plugins_enabled if p in ("comfyui", "song-gen", "voice")]
    gpu_ok = rc.hardware.has_gpu or not gpu_plugins
    checks.append(Check("GPU present for enabled media/voice plugins", gpu_ok,
                        "no GPU-only plugins" if not gpu_plugins else
                        (f"GPU present ({rc.hardware.primary_vram_gb:.0f}GB)" if gpu_ok
                         else f"media plugins {gpu_plugins} need a GPU but none detected")))

    # 5. merge-gate (a): parity vs the live .env (read-only)
    if ref_env:
        ok, mism, compared = parity.report(rc.env, ref_env)
        checks.append(Check(f"parity vs live .env ({ref_env})", ok,
                            f"{len(compared)} keys compared, 0 mismatch" if ok
                            else f"{len(mism)} mismatch: {', '.join(sorted(mism))}"))

    # 6. images available — project images must be built (blocking); upstream may be pulled (note)
    if images_present is not None:
        needed = required_images(rc, project)
        proj_missing = [i for i in needed if _is_buildable(i, project) and i not in images_present]
        upstream_missing = [i for i in needed
                            if not _is_buildable(i, project) and i not in images_present]
        detail = "all built"
        if proj_missing:
            hints = []
            for i in proj_missing:
                if "llamacpp-patched" in i:
                    hints.append(f"{i} (build from v2/docker/llamacpp-patched)")
                else:
                    hints.append(i)
            detail = f"build first: {', '.join(hints)}"
        checks.append(Check("project images built locally", not proj_missing, detail))
        if upstream_missing:
            checks.append(Check("upstream images cached", False,
                                f"Docker will pull: {', '.join(upstream_missing)}", blocking=False))

    # 7. secrets present (non-blocking): if a local secrets.env exists, warn which required KEYS
    # are still empty/absent. Missing secrets.env entirely is fine here — it's operator-managed and
    # created out-of-band; this only helps catch a half-filled one before the flip.
    if secrets_env is not None and Path(secrets_env).exists():
        missing = _missing_secret_keys(rc, secrets_env)
        checks.append(Check(f"secrets present in {secrets_env}", not missing,
                            "all required secrets set" if not missing
                            else f"{len(missing)} missing: {', '.join(missing)}",
                            blocking=False))

    go = all(c.ok for c in checks if c.blocking)
    return go, checks
