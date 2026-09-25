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
_DIALOG_START = re.compile(r"Dialog #\d+")
_TEXT_INPUT = re.compile(r"^\s+(\w+) \(Text\)")


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
    dialog_task: asyncio.Task | None = None

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
                # A pipe, not DEVNULL: the login dialog is answered by typing MCC's own
                # /dialog commands, before the MCP endpoint exists.
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
            )
            bot.dialog_task = asyncio.create_task(self._answer_login_dialogs(bot))
            self._bots[nick] = bot
            return bot

    async def _answer_login_dialogs(self, bot: BotInstance) -> None:
        """Fill and submit every login dialog the server shows while the bot is connecting.

        The network's Auth plugin opens a dialog before the player reaches a server: a register
        form for a new account (password + confirm), a login form afterwards (password). MCC only
        prints it, and the bot's MCP endpoint does not exist yet, so the answer goes in through
        MCC's console. Every text input gets the same password, then the first action is clicked,
        which is "Registrar" / "Entrar" on those forms.
        """
        offset = 0
        pending: list[str] | None = None
        while bot.running and bot.status != "stopped":
            await asyncio.sleep(0.5)
            try:
                with bot.log_path.open("r", encoding="utf-8", errors="replace") as log:
                    log.seek(offset)
                    chunk = log.read()
                    offset = log.tell()
            except OSError:
                continue
            for raw in chunk.splitlines():
                line = _ANSI.sub("", raw)
                if _DIALOG_START.search(line):
                    pending = []
                elif pending is not None:
                    match = _TEXT_INPUT.match(line)
                    if match:
                        pending.append(match.group(1))
                    elif line.startswith("Use /dialog help"):
                        await self._submit_dialog(bot, pending)
                        pending = None

    async def _submit_dialog(self, bot: BotInstance, inputs: list[str]) -> None:
        if bot.proc.stdin is None:
            return
        commands = [f"/dialog input {name} {config.BOT_PASSWORD}" for name in inputs]
        commands.append("/dialog click 1")
        for command in commands:
            bot.proc.stdin.write((command + "\n").encode())
        await bot.proc.stdin.drain()

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
        if bot.dialog_task is not None:
            bot.dialog_task.cancel()
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
