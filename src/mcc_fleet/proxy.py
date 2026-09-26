"""Per-bot MCP client that forwards tool calls to each MCC's embedded /mcp endpoint."""

from __future__ import annotations

from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from . import config
from .instances import InstanceManager


class BotProxy:
    """Forwards MCP tool discovery/calls to the MCC instance for a given nick.

    A fresh Client is opened per request. MCC's embedded server is lightweight and
    this keeps us robust to bots that come and go; the connection cost is negligible
    next to in-game actions.
    """

    def __init__(self, manager: InstanceManager) -> None:
        self._manager = manager

    def _client(self, nick: str) -> Client:
        bot = self._manager.require(nick)
        if bot.status != "ready":
            raise ValueError(
                f"Bot {nick!r} is not ready (status={bot.status!r}). "
                "Use wait_bot until it reports 'ready'."
            )
        return Client(StreamableHttpTransport(url=config.mcp_url(bot.mcp_port)))

    async def list_tools(self, nick: str) -> list[dict[str, Any]]:
        async with self._client(nick) as client:
            tools = await client.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tools
            ]

    async def call_tool(
        self, nick: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        async with self._client(nick) as client:
            result = await client.call_tool(tool, arguments or {})
            # Prefer structured content when present, else fall back to text blocks.
            if getattr(result, "structured_content", None) is not None:
                return result.structured_content
            return getattr(result, "data", None) or [
                getattr(block, "text", str(block)) for block in result.content
            ]
