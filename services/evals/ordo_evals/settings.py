"""Runtime settings, read from the environment the plugin manifest renders (services/evals/plugin.yaml).

Secrets are held here but never printed: `redacted()` is the only representation that may be logged.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .secret_env import read_secret

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # services/evals (mounted at /app)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclasses.dataclass(frozen=True)
class Settings:
    model_base_url: str
    model_name: str
    litellm_key: str
    hermes_api_url: str
    hermes_api_key: str
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str
    ops_controller_url: str
    ops_controller_token: str
    n8n_url: str
    qdrant_url: str
    qdrant_collection: str
    git_commit: str | None
    git_dirty: bool | None
    results_dir: Path
    vault_dir: Path
    hermes_state_db: Path
    datasets_dir: Path
    hermes_timeout_s: float
    hermes_item_budget_s: float
    hermes_overrun_wait_s: float
    model_max_tokens: int | None

    @classmethod
    def from_env(cls) -> Settings:
        max_tokens = _env("EVALS_MODEL_MAX_TOKENS")
        return cls(
            model_base_url=_env("MODEL_BASE_URL", "http://model-gateway:11435/v1"),
            model_name=_env("MODEL_NAME", "local-chat"),
            # Secrets: a file under /run/secrets (<NAME>_FILE, the rendered delivery), else the env var.
            litellm_key=read_secret("LITELLM_KEY_EVALS"),
            hermes_api_url=_env("HERMES_API_URL", "http://agent:8642/v1"),
            hermes_api_key=read_secret("HERMES_API_SERVER_KEY"),
            langfuse_host=_env("LANGFUSE_HOST", "http://langfuse-web:3000"),
            langfuse_public_key=read_secret("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=read_secret("LANGFUSE_SECRET_KEY"),
            ops_controller_url=_env("OPS_CONTROLLER_URL", "http://ops-controller:9000"),
            # ops-controller authenticates every call except its health probe.
            ops_controller_token=read_secret("OPS_CONTROLLER_TOKEN"),
            n8n_url=_env("N8N_URL", "http://n8n:5678"),
            qdrant_url=_env("QDRANT_URL", "http://qdrant:6333"),
            # Same var rag-ingestion reads (services/rag/plugin.yaml: QDRANT_COLLECTION from
            # ${RAG_COLLECTION:-documents}), so the RAG-leak safety check scans the collection the
            # ingester actually writes to, not a hardcoded default that could drift from it.
            qdrant_collection=_env("QDRANT_COLLECTION", "documents"),
            # E7: git provenance of the mounted services/evals code, set by scripts/evals/run.sh
            # (the container has no git binary - the host wrapper computes these with the real git
            # and passes them through). Unset (both "") means the run was not launched through the
            # wrapper: provenance is unknown, not "clean". See runner._provenance_gate.
            git_commit=_env("GIT_COMMIT") or None,
            git_dirty={"1": True, "0": False}.get(_env("GIT_DIRTY")),
            results_dir=Path(_env("EVALS_RESULTS_DIR", "/results")),
            vault_dir=Path(_env("EVALS_VAULT_DIR", "/vault")),
            hermes_state_db=Path(_env("EVALS_HERMES_STATE_DB", "/hermes-home/state.db")),
            datasets_dir=Path(_env("EVALS_DATASETS_DIR", str(PACKAGE_ROOT / "datasets"))),
            # A Hermes turn on the local model can legitimately take many minutes (tool loops,
            # long prefill); the Hermes gateway's own turn cap is 3600s. This is the client's own
            # (httpx-level) outer ceiling - see hermes_item_budget_s below for the budget that
            # actually governs a normal run.
            hermes_timeout_s=float(_env("EVALS_HERMES_TIMEOUT_S", "3600")),
            # E10 (round-4 fix): a per-item wall-clock budget applied around the Hermes call itself
            # (suites/harness.py's call_hermes), well inside hermes_timeout_s above. On a real agent
            # this is expected to bind before the httpx-level timeout does; when it fires the item is
            # recovered from state.db and scored `did_not_converge` (a real result), not thrown away
            # as an infra error - see hermes_client.HermesTurn's error_kind docstring.
            hermes_item_budget_s=float(_env("EVALS_HERMES_ITEM_BUDGET_S", "900")),
            # E23 (round-10 fix): after the budget above fires, how long to wait for Hermes to stop
            # working the abandoned item before starting the next one (hermes_turn.wait_for_agent_idle).
            # The harness has no way to cancel a chat-completions turn (see hermes_client.py), and the
            # single llama.cpp slot is shared, so an item that is still generating is measured as the
            # next item's contention. 900s matches the item budget itself: the recorded overruns were
            # 873s and 853s, so a bound at the budget covers the observed worst case while still
            # ending a run that would otherwise wait on a stuck turn forever.
            hermes_overrun_wait_s=float(_env("EVALS_HERMES_OVERRUN_WAIT_S", "900")),
            # Unset = the deployment's own output cap (llama.cpp n_predict), i.e. the model as deployed.
            model_max_tokens=int(max_tokens) if max_tokens else None,
        )

    def redacted(self) -> dict[str, str]:
        """Loggable view: every credential reduced to set/unset."""
        view = {f.name: str(getattr(self, f.name)) for f in dataclasses.fields(self)}
        for secret in ("litellm_key", "hermes_api_key", "langfuse_public_key", "langfuse_secret_key"):
            view[secret] = "set" if getattr(self, secret) else "unset"
        return view
