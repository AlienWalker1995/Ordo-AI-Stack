"""Runtime settings, read from the environment the plugin manifest renders (services/evals/plugin.yaml).

Secrets are held here but never printed: `redacted()` is the only representation that may be logged.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

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
    n8n_url: str
    qdrant_url: str
    results_dir: Path
    vault_dir: Path
    hermes_state_db: Path
    datasets_dir: Path
    hermes_timeout_s: float
    model_max_tokens: int | None

    @classmethod
    def from_env(cls) -> Settings:
        max_tokens = _env("EVALS_MODEL_MAX_TOKENS")
        return cls(
            model_base_url=_env("MODEL_BASE_URL", "http://model-gateway:11435/v1"),
            model_name=_env("MODEL_NAME", "local-chat"),
            litellm_key=_env("LITELLM_KEY_EVALS"),
            hermes_api_url=_env("HERMES_API_URL", "http://agent:8642/v1"),
            hermes_api_key=_env("HERMES_API_SERVER_KEY"),
            langfuse_host=_env("LANGFUSE_HOST", "http://langfuse-web:3000"),
            langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
            ops_controller_url=_env("OPS_CONTROLLER_URL", "http://ops-controller:9000"),
            n8n_url=_env("N8N_URL", "http://n8n:5678"),
            qdrant_url=_env("QDRANT_URL", "http://qdrant:6333"),
            results_dir=Path(_env("EVALS_RESULTS_DIR", "/results")),
            vault_dir=Path(_env("EVALS_VAULT_DIR", "/vault")),
            hermes_state_db=Path(_env("EVALS_HERMES_STATE_DB", "/hermes-home/state.db")),
            datasets_dir=Path(_env("EVALS_DATASETS_DIR", str(PACKAGE_ROOT / "datasets"))),
            # A Hermes turn on the local model can legitimately take many minutes (tool loops,
            # long prefill); the Hermes gateway's own turn cap is 3600s.
            hermes_timeout_s=float(_env("EVALS_HERMES_TIMEOUT_S", "3600")),
            # Unset = the deployment's own output cap (llama.cpp n_predict), i.e. the model as deployed.
            model_max_tokens=int(max_tokens) if max_tokens else None,
        )

    def redacted(self) -> dict[str, str]:
        """Loggable view: every credential reduced to set/unset."""
        view = {f.name: str(getattr(self, f.name)) for f in dataclasses.fields(self)}
        for secret in ("litellm_key", "hermes_api_key", "langfuse_public_key", "langfuse_secret_key"):
            view[secret] = "set" if getattr(self, secret) else "unset"
        return view
