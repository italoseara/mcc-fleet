"""FastMCP server (stdio) exposing fleet-management + generic proxy tools to Claude."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .instances import InstanceManager
from .proxy import BotProxy

_manager = InstanceManager()
_proxy = BotProxy(_manager)


@asynccontextmanager
async def _lifespan(_app: FastMCP):
    try:
        yield
    finally:
        # Never leave orphaned MCC processes behind.
        await _manager.stop_all()


mcp = FastMCP(
    name="mcc-fleet",
    instructions=(
        "Manage a fleet of Minecraft Console Client (MCC) bots. Use spawn_bot to "
        "start a bot (it logs into the server in offline mode with the given nick), "
        "then list_bot_tools to see what that bot can do, and bot_call to drive it. "
        "Each bot is independent; pass its nick to every per-bot tool."
    ),
    lifespan=_lifespan,
)


@mcp.tool
async def spawn_bot(
    nick: str, host: str | None = None, port: int | None = None, timeout: float = 60.0
) -> dict[str, Any]:
    """Start a new MCC bot that joins the server under `nick` (offline mode).

    Waits until the bot's in-game MCP endpoint is ready. Returns its status and the
    tools it exposes. `host`/`port` default to the configured target server.
    """
    bot = await _manager.spawn(nick, host=host, port=port)
    bot = await _manager.wait_for_ready(bot, timeout=timeout)
    info = bot.public()
    if bot.status == "ready":
        info["tools"] = await _proxy.list_tools(nick)
    return info


@mcp.tool
async def list_bots() -> list[dict[str, Any]]:
    """List all bots with their status, MCP port, pid and uptime."""
    return _manager.list()


@mcp.tool
async def list_bot_tools(nick: str) -> list[dict[str, Any]]:
    """List the in-game tools a ready bot exposes (discovered from its MCC instance)."""
    return await _proxy.list_tools(nick)


@mcp.tool
async def bot_call(
    nick: str, tool: str, arguments: dict[str, Any] | None = None
) -> Any:
    """Invoke one of a bot's in-game tools. `tool`/`arguments` come from list_bot_tools."""
    return await _proxy.call_tool(nick, tool, arguments)


@mcp.tool
async def stop_bot(nick: str) -> dict[str, Any]:
    """Disconnect and terminate a bot's MCC process."""
    stopped = await _manager.stop(nick)
    return {"nick": nick, "stopped": stopped}


@mcp.tool
async def stop_all() -> dict[str, Any]:
    """Disconnect and terminate every running bot."""
    count = len(_manager.list())
    await _manager.stop_all()
    return {"stopped": count}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
