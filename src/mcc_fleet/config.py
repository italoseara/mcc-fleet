"""Paths, port allocation, and per-bot ini rendering for mcc-fleet."""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

# Project root = two levels up from this file (src/mcc_fleet/config.py -> project/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Per-bot ini template and the directory holding each bot's workdir, ini and log.
TEMPLATE_PATH = Path(os.environ.get("MCC_TEMPLATE", PROJECT_ROOT / "mcc-template.ini"))
RUNTIME_DIR = Path(os.environ.get("MCC_RUNTIME_DIR", PROJECT_ROOT / "runtime"))

# Path to the mcc executable (overridable via env for non-standard installs).
MCC_BINARY = os.environ.get("MCC_BINARY", "mcc")

# Default target server. Override per-spawn or via env.
DEFAULT_HOST = os.environ.get("MCC_SERVER_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("MCC_SERVER_PORT", "25565"))

# Protocol version MCC speaks: "auto" asks the server, "1.X.X" pins it. Override per-spawn or via env.
DEFAULT_MC_VERSION = os.environ.get("MCC_MC_VERSION", "auto")

# MCP HTTP listener config. 33333 is left free for manual MCC use.
BIND_HOST = "127.0.0.1"
BASE_MCP_PORT = int(os.environ.get("MCC_BASE_MCP_PORT", "33334"))
MCP_ROUTE = "/mcp"

# Nicks: Minecraft usernames are 3-16 chars of [A-Za-z0-9_].
_NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def validate_nick(nick: str) -> None:
    if not _NICK_RE.match(nick):
        raise ValueError(
            f"Invalid nick {nick!r}: must be 3-16 chars of letters, digits or underscore."
        )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((BIND_HOST, port))
            return True
        except OSError:
            return False


def allocate_port(reserved: set[int]) -> int:
    """Return the next free TCP port at/above BASE_MCP_PORT not in `reserved`."""
    port = BASE_MCP_PORT
    while port < BASE_MCP_PORT + 1000:
        if port not in reserved and _port_is_free(port):
            return port
        port += 1
    raise RuntimeError("No free MCP port available in range.")


def bot_workdir(nick: str) -> Path:
    return RUNTIME_DIR / nick


def mcp_url(mcp_port: int) -> str:
    return f"http://{BIND_HOST}:{mcp_port}{MCP_ROUTE}"


def render_ini(nick: str, host: str, port: int, mcp_port: int, mc_version: str) -> Path:
    """Render the per-bot ini from the template and return its path."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = (
        template.replace("__NICK__", nick)
        .replace("__HOST__", host)
        .replace("__PORT__", str(port))
        .replace("__MCP_PORT__", str(mcp_port))
        .replace("__MC_VERSION__", mc_version)
    )
    workdir = bot_workdir(nick)
    workdir.mkdir(parents=True, exist_ok=True)
    ini_path = workdir / "MinecraftClient.ini"
    ini_path.write_text(rendered, encoding="utf-8")
    return ini_path
