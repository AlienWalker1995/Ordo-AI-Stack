"""The Hermes image bakes in upstream's Bot Desktop packages (one headless Xfce screen per profile).

Upstream lists them in tools/bot_desktop/runtime.py (PACKAGES["apt"]) and execs the binaries in
REQUIRED_BINARIES; the image must install that list at build time (never apt-get into a running
container) and fail the build if any binary is missing.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")

UPSTREAM_APT_PACKAGES = [
    "tigervnc-standalone-server", "xfce4-panel", "xfwm4", "xfdesktop4", "xfce4-settings",
    "xfce4-terminal", "dbus-x11", "x11-xserver-utils", "x11-utils", "x11-xkb-utils", "xauth",
    "fonts-dejavu-core",
]
UPSTREAM_REQUIRED_BINARIES = ["Xvnc", "xfwm4", "xfce4-panel", "xfdesktop", "xfsettingsd",
                              "dbus-run-session", "xauth", "xdpyinfo", "setxkbmap", "xprop"]


def bot_desktop_block() -> str:
    start = DOCKERFILE.index("# Bot Desktop:")
    return DOCKERFILE[start:DOCKERFILE.index("\n\n", start)]


def test_installs_every_upstream_bot_desktop_package():
    block = bot_desktop_block()
    assert "--no-install-recommends" in block
    missing = [p for p in UPSTREAM_APT_PACKAGES if f" {p}" not in block.replace("\\\n", " ")]
    assert not missing, f"Bot Desktop packages missing from the image: {missing}"


def test_build_fails_when_a_launcher_binary_is_missing():
    block = bot_desktop_block()
    assert "command -v" in block and "exit 1" in block
    missing = [b for b in UPSTREAM_REQUIRED_BINARIES if f" {b}" not in block]
    assert not missing, f"binaries not checked at build time: {missing}"
