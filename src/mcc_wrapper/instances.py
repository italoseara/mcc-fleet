"""Lifecycle management for MCC subprocesses (spawn / readiness / stop)."""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config


@dataclass
class BotInstance:
    nick: str
    mcp_port: int
    workdir: Path
    log_path: Path
    proc: asyncio.subprocess.Process
    host: str
    port: int
    status: str = "starting"  # starting | ready | failed | stopped
    started_at: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.proc.returncode is None

    @property
    def uptime(self) -> float:
        return round(time.time() - self.started_at, 1)

    def public(self) -> dict:
        return {
            "nick": self.nick,
            "status": self.status,
            "mcp_port": self.mcp_port,
            "mcp_url": config.mcp_url(self.mcp_port),
            "host": self.host,
            "port": self.port,
            "pid": self.proc.pid,
            "running": self.running,
            "uptime_s": self.uptime,
            "log": str(self.log_path),
            "error": self.error,
        }


class InstanceManager:
    def __init__(self) -> None:
        self._bots: dict[str, BotInstance] = {}
        self._lock = asyncio.Lock()

    def get(self, nick: str) -> BotInstance | None:
        return self._bots.get(nick)

    def list(self) -> list[dict]:
        return [b.public() for b in self._bots.values()]

    def _reserved_ports(self) -> set[int]:
        return {b.mcp_port for b in self._bots.values()}

    async def spawn(
        self, nick: str, host: str | None = None, port: int | None = None
    ) -> BotInstance:
        config.validate_nick(nick)
        async with self._lock:
            existing = self._bots.get(nick)
            if existing and existing.running:
                raise ValueError(f"Bot {nick!r} is already running.")

            host = host or config.DEFAULT_HOST
            port = port or config.DEFAULT_PORT
            mcp_port = config.allocate_port(self._reserved_ports())
            ini_path = config.render_ini(nick, host, port, mcp_port)
            workdir = config.bot_workdir(nick)
            log_path = workdir / "mcc.log"

            log_file = log_path.open("w", encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                config.MCC_BINARY,
                str(ini_path),
                cwd=str(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
            bot = BotInstance(
                nick=nick,
                mcp_port=mcp_port,
                workdir=workdir,
                log_path=log_path,
                proc=proc,
                host=host,
                port=port,
            )
            self._bots[nick] = bot
            return bot

    async def wait_for_ready(self, bot: BotInstance, timeout: float = 60.0) -> BotInstance:
        """Poll the bot's MCP endpoint until it answers, or until timeout/crash."""
        url = config.mcp_url(bot.mcp_port)
        deadline = time.time() + timeout
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.time() < deadline:
                if not bot.running:
                    bot.status = "failed"
                    bot.error = self._tail_log(bot)
                    return bot
                try:
                    # The MCP HTTP endpoint exists only after world join. Any HTTP
                    # response (even 4xx/406) means the listener is up and serving.
                    resp = await client.post(
                        url,
                        json={
                            "jsonrpc": "2.0",
                            "id": "ping",
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "mcc-wrapper-probe", "version": "0"},
                            },
                        },
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                    if resp.status_code < 500:
                        bot.status = "ready"
                        return bot
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                    pass
                await asyncio.sleep(1.0)

        bot.status = "failed" if not bot.running else "starting"
        if bot.status == "failed":
            bot.error = self._tail_log(bot)
        else:
            bot.error = f"MCP endpoint not ready within {timeout:.0f}s (still connecting/joining?)."
        return bot

    @staticmethod
    def _tail_log(bot: BotInstance, lines: int = 25) -> str:
        try:
            text = bot.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log available)"
        return "\n".join(text.splitlines()[-lines:])

    async def stop(self, nick: str, timeout: float = 10.0) -> bool:
        bot = self._bots.get(nick)
        if bot is None:
            return False
        if bot.running:
            try:
                bot.proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(bot.proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                bot.proc.kill()
                await bot.proc.wait()
        bot.status = "stopped"
        del self._bots[nick]
        return True

    async def stop_all(self) -> None:
        for nick in list(self._bots.keys()):
            await self.stop(nick)
