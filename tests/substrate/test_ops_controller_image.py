"""The ops-controller image must ship what DockerBackend actually shells out to.

`DockerBackend` drives real docker through the CLI. Two of the things it invokes are separate
binaries, and a missing one is invisible to every test in this suite: `MockBackend` never shells
out, so compose-backed routes stay green in CI and answer 500 in production with docker's own help
text. That is exactly the failure slice 1 shipped (a protocol the real backend did not implement);
this is the same bug one layer down, in the image rather than the class.

Live defect, 2026-09-23: the Dockerfile copied /usr/local/bin/docker and nothing else, so
`docker compose` was "not a docker command" and compose up/down/restart, service recreate and
image pull were all broken on the real control plane.
"""
import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "services" / "ops-controller" / "Dockerfile"


def test_image_ships_the_docker_cli():
    assert "/usr/local/bin/docker" in DOCKERFILE.read_text(encoding="utf-8")


def test_image_ships_the_compose_plugin():
    """`docker compose` resolves through the cli-plugins dir, not the docker binary itself."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "cli-plugins/docker-compose" in text, (
        "DockerBackend._compose() shells out to `docker compose`, which is a CLI PLUGIN. Copying "
        "only /usr/local/bin/docker leaves every compose-backed route returning 500 against real "
        "docker while mock-backed tests pass."
    )


def test_docker_client_is_pinned_by_version_and_digest():
    """The compose plugin must match the host's, or each side hashes bind mounts differently.

    compose v2.33 (the floating `docker:27-cli`) adds `create_host_path: true` to every bind, the
    host's v5.1.0 does not, so a container created by one side reads as changed to the other and
    every ops-controller recreate cascaded into services the host had just created (drift D2).
    """
    sources = re.findall(r"^COPY --from=(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert sources, "expected the docker CLI and compose plugin to be copied from a docker image"
    for source in sources:
        assert re.fullmatch(r"docker:\d+\.\d+\.\d+-cli@sha256:[0-9a-f]{64}", source), (
            f"{source!r} floats: pin the exact docker CLI tag (whose bundled compose matches the host's) "
            f"and its digest"
        )
