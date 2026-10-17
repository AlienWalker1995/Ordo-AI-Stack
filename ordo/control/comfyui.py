"""ComfyUI's files, managed from the control plane: model downloads and custom-node requirements.

- `ModelDownloads`: one resumable download at a time into the ComfyUI models directory, from an
  allowlisted model host only (SSRF guard: HTTPS, known host, no private address, every redirect
  re-checked), with its progress polled in-process (`GET /models/download/status`).
- `NodeRequirements`: `pip install -r` a custom node pack's requirements inside the running
  ComfyUI container, where ComfyUI imports from.

Where those live (the models and custom-nodes directories, the container's name) is the control
plane's configuration (ordo/control/api.py); each is read when it is used.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .broker import Broker
from .responses import CONFIRM_REQUIRED, confirmed, error

# Model download validation (matches ops-api)
COMFYUI_CATEGORIES = (
    "checkpoints", "loras", "text_encoders", "latent_upscale_models",
    "vae", "unet", "clip", "clip_vision", "controlnet", "embeddings",
    "upscale_models", "diffusion_models", "vae_approx",
)

_MODEL_DOWNLOAD_ALLOWED_HOSTS = {
    "huggingface.co", "hf-mirror.com", "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co", "cdn-lfs-eu-1.huggingface.co",
    "civitai.com", "github.com", "objects.githubusercontent.com",
}
# Hugging Face serves LFS files by redirecting to a regional CDN whose host names are not stable
# (`us.aws.cdn.hf.co` on 2026-10-16; `cdn-lfs-us-1.huggingface.co` before). Any subdomain of these
# two zones is Hugging Face infrastructure; the exact list above stays for the hosts named in docs.
_MODEL_DOWNLOAD_ALLOWED_SUFFIXES = (".hf.co", ".huggingface.co")
# The hosts a Hugging Face token may be sent to: the hub itself, bare or www-prefixed as pasted
# from a browser, which checks the licence gate and answers with a pre-signed CDN URL. The CDN
# hop never needs the token, so it never gets it. hf-mirror.com stays downloadable (allowlist
# above) but never sees the token: it is a community mirror, and a bearer token handed to it
# could be replayed against huggingface.co.
_HF_TOKEN_HOSTS = {"huggingface.co", "www.huggingface.co"}


def _download_headers(url: str, hf_token: str) -> dict[str, str]:
    """Request headers for one hop of a model download: the token only when this hop is the hub."""
    headers = {"User-Agent": "ordo-ai-stack/1.0"}
    host = (urlparse(url).hostname or "").lower()
    if hf_token and host in _HF_TOKEN_HOSTS:
        headers["Authorization"] = f"Bearer {hf_token}"
    return headers

# One path segment of a ComfyUI custom-node pack. Deliberately narrower than the filesystem
# allows: the segment is interpolated into a container path that a pip invocation then reads.
_NODE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _validate_download_url(url: str) -> None:
    """Block SSRF: only allow HTTPS to known model-hosting domains, reject private IPs."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise ValueError("URL must use HTTPS on the standard port")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Cannot parse hostname from URL")
    allowed = host in _MODEL_DOWNLOAD_ALLOWED_HOSTS or host.endswith(_MODEL_DOWNLOAD_ALLOWED_SUFFIXES)
    if not allowed:
        raise ValueError(
            f"Host {host!r} not in allowed list. "
            f"Allowed: {', '.join(sorted(_MODEL_DOWNLOAD_ALLOWED_HOSTS))}, "
            f"and any subdomain of {', '.join(s.lstrip('.') for s in _MODEL_DOWNLOAD_ALLOWED_SUFFIXES)}"
        )
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local:
                raise ValueError(f"Host {host!r} resolves to private/reserved IP {addr}")
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host {host!r}: {exc}") from exc


def _validated_redirect_url(current_url: str, location: str) -> str:
    redirect_url = urljoin(current_url, location)
    _validate_download_url(redirect_url)
    return redirect_url


def _auto_detect_category(url: str, filename: str) -> str:
    """Auto-detect ComfyUI model category from URL/filename."""
    url_lower = url.lower()
    name_lower = filename.lower()
    for cat in sorted(COMFYUI_CATEGORIES, key=len, reverse=True):
        if cat in url_lower or cat in name_lower:
            return cat
    combined = f"{url_lower} {name_lower}"
    for keyword, category in (
        ("lora", "loras"),
        ("text_encoder", "text_encoders"),
        ("clip", "text_encoders"),
        ("vae", "vae"),
        ("unet", "unet"),
        ("controlnet", "controlnet"),
        ("upscale", "upscale_models"),
        ("embedding", "embeddings"),
    ):
        if keyword in combined:
            return category
    return "checkpoints"


class ModelDownloads:
    """At most one model download at a time, its state held in this process (not persisted)."""

    def __init__(self, models_dir: Callable[[], Path], hf_token: Callable[[], str] = lambda: ""):
        self._models_dir = models_dir
        # Read at download time, not construction: the token is a file secret that can be rotated
        # under a running control plane. "" means no token (ungated downloads still work).
        self._hf_token = hf_token
        self._lock = threading.Lock()
        self._status = {"running": False, "output": "", "done": True, "success": None, "progress": 0,
                        "filename": "", "category": ""}

    def start(self, body: dict[str, Any], worker: Callable[[str, str, str], None]) -> dict[str, Any]:
        """Validate a download request, claim the one download slot, and run `worker(url, category,
        filename)` on a background thread (the control plane passes its own `_run_model_download`,
        which calls `run`)."""
        url = str(body.get("url", "")).strip()
        if not url.startswith("https://"):
            return error(400, "URL must start with https://")
        try:
            _validate_download_url(url)
        except ValueError as e:
            return error(400, str(e))
        filename = str(body.get("filename", "")).strip() or url.split("/")[-1].split("?")[0]
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return error(400, "Invalid or undetectable filename")
        category = str(body.get("category", "")).strip()
        if category and category not in COMFYUI_CATEGORIES:
            return error(400, f"Invalid category. Must be one of: {COMFYUI_CATEGORIES}")
        if not category:
            category = _auto_detect_category(url, filename)
        with self._lock:
            # Checked and claimed under one lock: two concurrent requests cannot both start.
            if self._status.get("running"):
                return error(409, "A download is already in progress")
            self._status["running"] = True
        thread = threading.Thread(target=worker, args=(url, category, filename), daemon=True)
        thread.start()
        return {"status": "started", "category": category, "filename": filename}

    def status(self) -> dict[str, Any]:
        """Poll active download progress."""
        with self._lock:
            return dict(self._status)

    def run(self, url: str, category: str, filename: str) -> None:
        """Background download worker."""
        with self._lock:
            self._status.update({
                "running": True, "output": f"Starting: {filename}", "done": False,
                "success": None, "progress": 0, "filename": filename, "category": category,
            })
        dest_dir = self._models_dir() / category
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            with self._lock:
                self._status.update({
                    "output": f"Cannot create dir: {e}", "success": False,
                    "running": False, "done": True,
                })
            return

        dest = dest_dir / filename
        temp_path = dest.with_suffix(dest.suffix + ".tmp")
        try:
            import httpx
            start_byte = temp_path.stat().st_size if temp_path.exists() else 0
            hf_token = self._hf_token()
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                current_url = url
                for _ in range(10):
                    _validate_download_url(current_url)
                    # Headers are rebuilt per hop: the hub gets the token, the CDN it redirects to
                    # does not (a pre-signed URL needs none, and a token must not travel to a host
                    # that merely passed the allowlist).
                    req_headers = _download_headers(current_url, hf_token)
                    if start_byte > 0:
                        req_headers["Range"] = f"bytes={start_byte}-"
                    response = client.send(
                        client.build_request("GET", current_url, headers=req_headers),
                        stream=True,
                    )
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        raise ValueError("Redirect response did not include a location")
                    current_url = _validated_redirect_url(current_url, location)
                else:
                    raise ValueError("Too many redirects while downloading model")
                # A streamed httpx.Response is closed explicitly: it is not a context manager
                # (`with response:` raised TypeError on every download, 2026-10-16).
                try:
                    r = response
                    r.raise_for_status()
                    total = 0
                    total_header = r.headers.get("Content-Range") or r.headers.get("Content-Length")
                    if total_header and "/" in str(total_header):
                        total = int(str(total_header).split("/")[-1].strip())
                    elif r.headers.get("Content-Length"):
                        total = int(r.headers["Content-Length"]) + (start_byte or 0)
                    total_mb = total / (1024 * 1024) if total else 0
                    downloaded = start_byte
                    append = start_byte > 0 and r.status_code == 206
                    with open(temp_path, "ab" if append else "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            dl_mb = downloaded / (1024 * 1024)
                            pct = int(downloaded * 100 / total) if total else 0
                            msg = f"Downloading {filename} → {category}/\n"
                            msg += f"{dl_mb:.0f} / {total_mb:.0f} MB ({pct}%)" if total else f"{dl_mb:.0f} MB downloaded"
                            with self._lock:
                                self._status["output"] = msg
                                self._status["progress"] = pct
                finally:
                    response.close()
            temp_path.rename(dest)
            with self._lock:
                self._status["success"] = True
                self._status["output"] += f"\nDone — saved to {category}/{filename}"
        except Exception as e:
            with self._lock:
                self._status["output"] += f"\nError: {e}"
                self._status["success"] = False
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["done"] = True


def _validate_custom_node_path(node_path: str) -> str | None:
    """Relative path under ComfyUI custom_nodes. Returns None if it is not one."""
    cleaned = (node_path or "").strip().strip("/").replace("\\", "/")
    if not cleaned or len(cleaned) > 240 or ".." in cleaned:
        return None
    for segment in cleaned.split("/"):
        if not segment or not _NODE_PATH_SEGMENT.fullmatch(segment):
            return None
    return cleaned


class NodeRequirements:
    """`pip install -r` a custom node pack's requirements inside the running ComfyUI container."""

    def __init__(self, broker: Broker | None, custom_nodes_dir: Callable[[], Path],
                 container_name: Callable[[], str]):
        self.broker = broker
        self._custom_nodes_dir = custom_nodes_dir
        self._container_name = container_name

    def install(self, body: dict[str, Any] | None) -> dict[str, Any]:
        """pip install -r a custom node pack's requirements INSIDE the running comfyui container.

        Installing on the host would put the packages somewhere ComfyUI never imports from.
        """
        body = body or {}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        node_path = _validate_custom_node_path(body.get("node_path") or "")
        if node_path is None:
            return error(400, "Invalid node_path")
        requirements_on_host = self._custom_nodes_dir() / node_path / "requirements.txt"
        if not requirements_on_host.is_file():
            return error(
                404, f"No requirements.txt at custom_nodes/{node_path}/requirements.txt"
            )
        if not self.broker:
            return error(503, "no container backend")
        container = self._container_name()
        requirements_in_container = f"/root/ComfyUI/custom_nodes/{node_path}/requirements.txt"
        try:
            exit_code, output = self.broker.backend.exec_in(
                container,
                ["python3", "-m", "pip", "install", "-r", requirements_in_container],
            )
        except FileNotFoundError:
            return error(
                503, f"Container {container!r} not found - start comfyui first"
            )
        except Exception as exc:
            return error(500, f"exec failed: {exc}")
        if len(output) > 12000:
            output = output[:12000] + "\n... [truncated]"
        ok = exit_code == 0
        result: dict[str, Any] = {
            "ok": ok,
            "exit_code": exit_code,
            "output": output,
            "node_path": node_path,
        }
        if not ok:
            result["_status"] = 500
            result["error"] = f"pip install exited {exit_code}"
        return result
