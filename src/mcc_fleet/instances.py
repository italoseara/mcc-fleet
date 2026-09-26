"""Lifecycle management for MCC subprocesses (spawn / readiness / stop)."""

from __future__ import annotations

import asyncio
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class BotInstance:
    nick: str
    mcp_port: int
    workdir: Path
    log_path: Path
    proc: asyncio.subprocess.Process
    host: str
    port: int
    mc_version: str
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
            "mc_version": self.mc_version,
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
        self,
        nick: str,
        host: str | None = None,
        port: int | None = None,
        mc_version: str | None = None,
    ) -> BotInstance:
        config.validate_nick(nick)
        async with self._lock:
            existing = self._bots.get(nick)
            if existing and existing.running:
                raise ValueError(f"Bot {nick!r} is already running.")

            host = host or config.DEFAULT_HOST
            port = port or config.DEFAULT_PORT
            mc_version = mc_version or config.DEFAULT_MC_VERSION
            mcp_port = config.allocate_port(self._reserved_ports())
            ini_path = config.render_ini(nick, host, port, mcp_port, mc_version)
            workdir = config.bot_workdir(nick)
            log_path = workdir / "mcc.log"

            log_file = log_path.open("w", encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                config.MCC_BINARY,
                str(ini_path),
                cwd=str(workdir),
                # A pipe, not DEVNULL: send_console types into MCC's console, which is the only
                # way to act (login plugins, dialogs) before the MCP endpoint exists.
                stdin=asyncio.subprocess.PIPE,
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
                mc_version=mc_version,
            )
            self._bots[nick] = bot
            return bot

    def require(self, nick: str) -> BotInstance:
        bot = self._bots.get(nick)
        if bot is None:
            raise ValueError(f"Unknown bot {nick!r}. Spawn it first with spawn_bot.")
        return bot

    async def send_console(self, nick: str, command: str) -> None:
        """Type one line into the bot's MCC console, as if entered at its terminal.

        Lines starting with `/` that match an MCC internal command (`/dialog`, `/move`, ...) run
        in MCC; anything else goes to the server as chat or a server command. Works in any status,
        so it can clear login gates that hold the bot before its MCP endpoint exists.
        """
        bot = self.require(nick)
        if not bot.running or bot.proc.stdin is None:
            raise ValueError(f"Bot {nick!r} is not running.")
        if "\n" in command or "\r" in command:
            raise ValueError("Send one line per call: MCC runs console lines concurrently.")
        bot.proc.stdin.write((command + "\n").encode())
        await bot.proc.stdin.drain()

    def read_log(self, nick: str, lines: int = 50, strip_ansi: bool = True) -> str:
        return self._tail_log(self.require(nick), lines, strip_ansi)

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
                                "clientInfo": {"name": "mcc-fleet-probe", "version": "0"},
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
            bot.error = (
                f"MCP endpoint not ready within {timeout:.0f}s. The bot may still be joining, or "
                "held by a login plugin or dialog: check bot_log, answer with bot_console, then "
                "wait_bot."
            )
        return bot

    @staticmethod
    def _tail_log(bot: BotInstance, lines: int = 25, strip_ansi: bool = True) -> str:
        try:
            text = bot.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log available)"
        if strip_ansi:
            text = _ANSI.sub("", text)
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
