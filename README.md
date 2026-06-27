# mcc-fleet

An MCP server that manages a **fleet** of [Minecraft Console Client](https://github.com/MCCTeam/Minecraft-Console-Client)
(MCC) instances and lets an MCP client (e.g. Claude) drive each bot individually.

MCC already ships an embedded MCP server, but each `mcc` process controls only one
account. This wrapper:

1. Spawns/stops multiple `mcc` processes — each with its own nick, working directory,
   and MCP HTTP port.
2. Proxies the in-game tools of each MCC instance, addressed by nick.

Targets **offline-mode** servers (login by nick, no Microsoft auth) and MCC v26.1.

## How it works

```
Claude ──stdio──▶ mcc-fleet (this wrapper) ──HTTP /mcp──▶ mcc (Bot1)
                                            ──HTTP /mcp──▶ mcc (Bot2)
                                            ...
```

Each bot's MCC config is rendered from `mcc-template.ini` into `runtime/<nick>/MinecraftClient.ini`.
MCC's embedded MCP endpoint only comes up *after* the bot joins the world, so `spawn_bot`
polls it until ready.

## Tools exposed to the client

| Tool | Purpose |
|------|---------|
| `spawn_bot(nick, host?, port?, timeout?)` | Start a bot, wait until ready, return its tools |
| `list_bots()` | Status, MCP port, pid, uptime of all bots |
| `list_bot_tools(nick)` | Discover a bot's in-game tools |
| `bot_call(nick, tool, arguments)` | Invoke one of a bot's in-game tools |
| `stop_bot(nick)` / `stop_all()` | Disconnect/terminate bot(s) |

## Setup

```bash
uv sync
```

Configure the target server (defaults: `localhost:25565`):

```bash
export MCC_SERVER_HOST=play.example.net
export MCC_SERVER_PORT=25565
# optional:
export MCC_BINARY=mcc            # path to the mcc executable
export MCC_BASE_MCP_PORT=33334   # first MCP port to allocate
```

## Register with Claude Code

```bash
claude mcp add mcc-fleet \
  -e MCC_SERVER_HOST=play.example.net -e MCC_SERVER_PORT=25565 \
  -- uv run --directory /path/to/mcc-fleet mcc-wrapper
```

Then ask Claude to `spawn_bot("Bot1")`, `bot_call("Bot1", ...)`, etc.

## Notes

- The server **must** be in offline-mode for nick-only login.
- On shutdown the wrapper kills all child `mcc` processes (`stop_all`).
