"""Pure, dependency-free construction of the SAFE per-service compose-recreate command.

Kept in its own module (no fastapi/docker/httpx imports) so the exact argv can be
unit-tested in the v2 substrate's throwaway container, whose dev deps are only
pyyaml/pytest/ruff. `main.py` imports `build_recreate_cmd` from here; the command it
returns is the ONLY thing that shells docker-compose against the ordo-v2 stack.

Why this shape (the guardrails the 2026-06-26 and pin-drop incidents demand):
  * BOTH env files (`.env` + `secrets.env`) are passed with `--env-file`. Passing any
    `--env-file` disables compose's implicit `.env` auto-load, so `.env` must be listed
    too. Without `secrets.env`, `${LITELLM_MASTER_KEY}`/`${OPS_CONTROLLER_TOKEN}`/… go
    UNSET and secret-dependent services crash-loop (the 2026-06-26 oauth2-proxy 11-byte
    cookie outage shape). Both are mandatory.
  * `--project-name ordo-v2` + `--project-directory <out>` pin the recreate to the V2
    project and the EXISTING rendered tree — never another project, never a re-render.
  * `--no-deps` is MANDATORY: recreate ONLY the named service, never cascade-recreate its
    dependencies (which would drop their GPU pins / secrets and touch services the operator
    didn't ask for).
  * `--force-recreate` so a recreate with unchanged compose still restarts the container to
    pick up an edited `.env` value (e.g. a new LLAMACPP_MODEL from the model-config button).
  * NO render step. The rendered `out/docker-compose.yml` (with the llamacpp 5090 uuid pin
    baked into its `environment:`/`deploy:` blocks) is replayed byte-for-byte.
"""
from __future__ import annotations

# Both env files are required at the CLI level (see module docstring). Order matters:
# .env first (derived), secrets.env second (operator secrets) — later files win, mirroring
# how the live stack was brought up and how each service's own `env_file:` layers them.
ENV_FILES = (".env", "secrets.env")


def build_recreate_cmd(
    service: str,
    *,
    project: str,
    project_dir: str,
    compose_files: list[str],
    profiles: list[str] | None = None,
    docker_compose_bin: str = "docker-compose",
) -> list[str]:
    """Build the exact `docker-compose ... up -d --no-deps --force-recreate <service>` argv.

    `project` -> --project-name, `project_dir` -> --project-directory (the rendered out/ dir,
    which is also where .env/secrets.env live). `compose_files` are filenames relative to
    `project_dir` (e.g. ["docker-compose.yml"]).

    `profiles` -> one `--profile <p>` per entry. These MUST cover every profile the running
    stack was started with, because a target service's `depends_on:` may reference a PROFILED
    service (e.g. open-webui depends_on qdrant, which sits behind the `rag` profile). Without
    the profile active, `docker compose ... open-webui` aborts with "no such service: qdrant"
    even though `--no-deps` means we never START qdrant. Passing all defined profiles just
    widens the resolvable service set; `--no-deps` still guarantees only the named service is
    (re)created. Returns a list ready for subprocess.run.
    """
    if not service:
        raise ValueError("service must be a non-empty string")
    cmd = [docker_compose_bin, "--project-name", project, "--project-directory", project_dir]
    for cf in compose_files:
        # compose files are addressed inside the project dir (the mounted out/ tree)
        cmd += ["-f", f"{project_dir}/{cf}"]
    # every profile the stack runs with, so profiled depends_on references resolve (see docstring)
    for prof in (profiles or []):
        cmd += ["--profile", prof]
    # BOTH env files, resolved inside the project dir. Mandatory — see module docstring.
    for env_file in ENV_FILES:
        cmd += ["--env-file", f"{project_dir}/{env_file}"]
    cmd += ["up", "-d", "--no-deps", "--force-recreate", service]
    return cmd


def discover_profiles(compose_yaml: dict) -> list[str]:
    """Every profile name declared by any service in the rendered compose, sorted + deduped.

    This is the drift-free source of truth: the profiles come straight from the artifact being
    replayed, so they always match the file (no hardcoded list to drift). Passing ALL of them is
    safe — `--no-deps` keeps the recreate scoped to the single named service regardless.
    """
    profiles: set[str] = set()
    for svc in (compose_yaml.get("services") or {}).values():
        for p in (svc.get("profiles") or []):
            if isinstance(p, str) and p:
                profiles.add(p)
    return sorted(profiles)
