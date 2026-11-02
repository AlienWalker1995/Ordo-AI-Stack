"""
OrdoProxyConfigNode — shared LiteLLM proxy configuration for all Ordo AI nodes.
"""

import logging

logger = logging.getLogger("Ordo")


def _normalize_litellm_proxy_base_url(url: str) -> str:
    """Return a base URL with a scheme so ``requests`` accepts it.

    Bare hostnames (no ``://``) are prefixed with ``http://`` to match the
    internal LiteLLM proxy. URLs that already include a scheme are unchanged.
    """
    s = url.strip()
    if not s:
        return s
    if "://" in s:
        return s
    return f"http://{s}"


class OrdoProxyConfigNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "proxy_url": ("STRING", {
                    "default": "",
                    "tooltip": "LiteLLM proxy base URL (bare host → http://…)",
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "tooltip": "Optional virtual API key for the proxy",
                }),
            }
        }

    RETURN_TYPES = ("ORDO_PROXY_CONFIG",)
    RETURN_NAMES = ("proxy_config",)
    FUNCTION = "build_config"
    CATEGORY = "Ordo/AI"
    DESCRIPTION = "Configure the LiteLLM proxy connection for Ordo AI nodes"

    def build_config(self, proxy_url: str, api_key: str):
        url = _normalize_litellm_proxy_base_url(proxy_url)
        if not url:
            raise ValueError("Proxy URL cannot be empty")
        return ({"url": url, "api_key": api_key.strip()},)
