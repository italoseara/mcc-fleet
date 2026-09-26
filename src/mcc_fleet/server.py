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
        "Each bot is independent; pass its nick to every per-bot tool. A bot's in-game tools "
        "exist only after it joins the world: if the server holds it first (a login plugin, a "
        "register/login dialog), read bot_log, answer with bot_console, then call wait_bot."
    ),
    lifespan=_lifespan,
)


@mcp.tool
async def spawn_bot(
    nick: str,
    host: str | None = None,
    port: int | None = None,
    version: str | None = None,
    wait: bool = True,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Start a new MCC bot that joins the server under `nick` (offline mode).

    With `wait` (default) it blocks until the bot's in-game MCP endpoint is ready and returns
    its status and the tools it exposes. Pass `wait=False` when the server gates the join (login
    plugin, dialog): it returns right after launch, so the gate can be cleared with bot_log and
    bot_console before wait_bot. `host`/`port` default to the configured target server;
    `version` is the Minecraft version MCC speaks ("auto" by default, or e.g. "1.21.4").
    """
    bot = await _manager.spawn(nick, host=host, port=port, mc_version=version)
    if not wait:
        return bot.public()
    return await _await_ready(nick, timeout)


@mcp.tool
async def wait_bot(nick: str, timeout: float = 60.0) -> dict[str, Any]:
    """Wait until a spawned bot's in-game MCP endpoint is ready, then return its status and tools.

    Use after spawn_bot(wait=False), or when spawn_bot timed out while the bot was still joining.
    """
    return await _await_ready(nick, timeout)


async def _await_ready(nick: str, timeout: float) -> dict[str, Any]:
    bot = await _manager.wait_for_ready(_manager.require(nick), timeout=timeout)
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
async def bot_console(nick: str, command: str) -> dict[str, Any]:
    """Type one line into a bot's MCC console, in any status while its process runs.

    MCC internal commands (e.g. `/dialog input <field> <text>`, `/dialog click <n>`) run in MCC;
    any other line is sent to the server as chat or a command. Output shows up in bot_log.
    """
    await _manager.send_console(nick, command)
    return {"nick": nick, "sent": command}


@mcp.tool
async def bot_log(nick: str, lines: int = 50, strip_ansi: bool = True) -> str:
    """Return the last `lines` lines of a bot's MCC output (connection, chat, dialogs, errors)."""
    return _manager.read_log(nick, lines, strip_ansi)


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
