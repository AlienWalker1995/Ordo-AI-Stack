"""Live ground-truth probes for the harness checks (the `checks.Probes` protocol).

All reads go straight to the source of truth over the project network or the mounted vault; none go
through Hermes. The vault is mounted read-only except the scratch root (checks.VAULT_EVAL_ROOT), and
`vault_write` refuses any other path even if the mount would allow it.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx

from ordo_evals.checks import VAULT_EVAL_ROOT, ProbeError, is_scratch_source
from ordo_evals.ids import safe_token


class LiveProbes:
    def __init__(self, *, vault_dir: Path, ops_controller_url: str, ops_controller_token: str, n8n_url: str,
                 qdrant_url: str, qdrant_collection: str = "documents"):
        self._vault = vault_dir.resolve()
        self._ops = ops_controller_url.rstrip("/")
        self._ops_headers = {"Authorization": f"Bearer {ops_controller_token}"}
        self._n8n = n8n_url.rstrip("/")
        self._qdrant = qdrant_url.rstrip("/")
        self._qdrant_collection = qdrant_collection

    def _vault_path(self, relative_path: str) -> Path:
        path = (self._vault / relative_path).resolve()
        if self._vault not in path.parents:
            raise ProbeError(f"vault path escapes the vault: {relative_path!r}")
        return path

    def vault_read(self, relative_path: str) -> str | None:
        path = self._vault_path(relative_path)
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProbeError(f"cannot read {relative_path}: {exc}") from exc

    def vault_write(self, relative_path: str, content: str) -> None:
        if not relative_path.startswith(f"{VAULT_EVAL_ROOT}/"):
            raise ProbeError(f"refusing to write outside {VAULT_EVAL_ROOT}/: {relative_path!r}")
        path = self._vault_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def cleanup_run(self, run_id: str) -> None:
        """Delete <VAULT_EVAL_ROOT>/<run>/ (everything the run seeded and everything Hermes wrote for it)."""
        run_dir = self._vault_path(f"{VAULT_EVAL_ROOT}/{safe_token(run_id)}")
        if run_dir.is_dir():
            shutil.rmtree(run_dir)

    def ops_status(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self._ops}/status", headers=self._ops_headers, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProbeError(f"ops-controller /status: {exc}") from exc

    def n8n_healthy(self) -> bool:
        """n8n's own /healthz. Unreachable IS the truth "not healthy", not a probe failure."""
        try:
            response = httpx.get(f"{self._n8n}/healthz", timeout=15.0)
        except httpx.HTTPError:
            return False
        try:
            return response.status_code == 200 and response.json().get("status") == "ok"
        except ValueError:
            return False

    def qdrant_collections(self) -> list[str]:
        try:
            response = httpx.get(f"{self._qdrant}/collections", timeout=15.0)
            response.raise_for_status()
            return [c["name"] for c in response.json()["result"]["collections"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProbeError(f"qdrant /collections: {exc}") from exc

    def qdrant_scratch_leak_sources(self) -> list[str]:
        """Every point currently in the RAG collection whose ingested `source` is rooted under
        VAULT_EVAL_ROOT (E6 safety net). rag-ingestion's hidden-path rule should always keep these
        out; a non-empty result means that exclusion did not hold for this run. A collection that
        does not exist yet (a fresh stack, or `rag` disabled) has no points and is not an error."""
        leaked: list[str] = []
        offset: Any = None
        try:
            with httpx.Client(timeout=30.0) as client:
                exists = client.get(f"{self._qdrant}/collections/{self._qdrant_collection}")
                if exists.status_code == 404:
                    return []
                exists.raise_for_status()
                while True:
                    body: dict[str, Any] = {"limit": 256, "with_payload": ["source"], "with_vector": False}
                    if offset is not None:
                        body["offset"] = offset
                    response = client.post(
                        f"{self._qdrant}/collections/{self._qdrant_collection}/points/scroll", json=body)
                    response.raise_for_status()
                    result = response.json()["result"]
                    for point in result.get("points", []):
                        source = str((point.get("payload") or {}).get("source", ""))
                        if source and is_scratch_source(source):
                            leaked.append(source)
                    offset = result.get("next_page_offset")
                    if offset is None:
                        break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProbeError(f"qdrant scratch-leak scan of {self._qdrant_collection}: {exc}") from exc
        return leaked
