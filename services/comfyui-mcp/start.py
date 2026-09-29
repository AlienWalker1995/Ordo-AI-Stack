"""Entrypoint of the comfyui-mcp image: wait for ComfyUI, then run the upstream server.

Upstream server.py probes ComfyUI five times (about 30 s of backoff) when it starts and then exits
1. ComfyUI itself takes minutes to boot (its healthcheck allows 420 s), so whenever both start
together (a deploy that recreates both, a Docker restart) this container exited and was
restarted by its restart policy until ComfyUI answered. A compose depends_on condition cannot
prevent that: `ordo apply` and `ordo recreate` start services with --no-deps, and the Docker
daemon ignores depends_on when it restarts containers.

So this process waits, with no deadline, on the same probe upstream uses (a GET of
/object_info/CheckpointLoaderSimple through the GPU admission gate; a read, not a submission, so it
takes no GPU lease). Once ComfyUI answers it replaces itself with server.py, whose own check then
passes on its first try and whose client loads the model list from a running ComfyUI. Until then
the MCP port is not bound, so the container's healthcheck reports it as not ready.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from collections.abc import Callable

logger = logging.getLogger("comfyui_mcp_start")

PROBE_PATH = "/object_info/CheckpointLoaderSimple"
PROBE_TIMEOUT_S = 5
FIRST_DELAY_S = 2
MAX_DELAY_S = 15


def comfyui_available(base_url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when ComfyUI answers the probe with HTTP 200 and a JSON object (upstream's test)."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + PROBE_PATH, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.load(response)
    except (OSError, ValueError):
        # OSError covers refused connections, timeouts and HTTP error statuses (HTTPError);
        # ValueError covers a body that is not JSON.
        return False
    return isinstance(body, dict)


def wait_for_comfyui(
    base_url: str,
    *,
    check: Callable[[str], bool] = comfyui_available,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once ComfyUI answers. Retries forever, backing off from 2 s to at most 15 s."""
    delay = FIRST_DELAY_S
    attempt = 1
    while not check(base_url):
        logger.info("ComfyUI at %s is not answering yet (attempt %d); retrying in %d s", base_url, attempt, delay)
        sleep(delay)
        delay = min(delay * 2, MAX_DELAY_S)
        attempt += 1
    logger.info("ComfyUI at %s is answering (attempt %d); starting the MCP server", base_url, attempt)


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    base_url = os.environ.get("COMFYUI_URL", "")
    if not base_url:
        # The manifest requires it (compose stops on an empty value); this only guards a hand run.
        sys.exit("COMFYUI_URL is not set: it must point at the ComfyUI GPU admission gate")
    wait_for_comfyui(base_url)
    # Replace this process, so server.py runs exactly as it would as the image's command.
    os.execv(sys.executable, [sys.executable, "server.py", *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
