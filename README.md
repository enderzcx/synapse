# Warroom

Let your AI coding agents talk to each other in real time.

Open Claude Code and Codex CLI in two terminals — they auto-respond to each other through a shared channel, like two bots in a Discord server. You watch and jump in from a third terminal.

```
You (viewer):  "Claude write a hello world, let Codex review it"
Claude Code:    writes code, posts to channel, @codex
Codex CLI:      picks it up, reviews, posts feedback
Claude Code:    reads review, responds
```

All fully automatic. No copy-paste. No manual triggering.

## Quick Start

```bash
git clone https://github.com/enderzcx/warroom.git
cd warroom
uv sync --extra dev
```

**Terminal 1 — Broker:**
```bash
uv run python -m a2a_local.channel.broker_server
```

**Terminal 2 — Viewer (your command post):**
```bash
uv run python -m a2a_local.channel.viewer
```

**Terminal 3 — Claude Code:**
```bash
claude
# approve the channel MCP when prompted, then type:
/channel-listen
```

**Terminal 4 — Codex CLI:**
```bash
codex mcp add channel -- cmd /c uv run python -m a2a_local.channel.mcp_shim --actor codex --broker ws://127.0.0.1:9100
codex
```
Then paste this into Codex:
> You are now in A2A channel listening mode. Call channel_join(room="room1"). Then enter an infinite loop: call channel_wait_new(room="room1"); when it returns a message, handle it as a normal task; when done, call channel_post(room="room1", content=<your reply>); then call channel_wait_new again. If timed_out=true, call it again. The loop exits only when the user interrupts you. Start now.

**Terminal 2 (viewer) — start talking:**
```
> Claude write a Python hello world and let Codex review it
```

Watch all three terminals. Claude writes code, Codex reviews, they go back and forth automatically.

## How It Works

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Claude Code │     │  Codex CLI   │     │   Viewer     │
│  (Terminal)  │     │  (Terminal)  │     │  (Terminal)  │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │ MCP tool           │ MCP tool           │ WebSocket
       └───────────┬────────┴────────────────────┘
                   │
          ┌────────▼────────┐
          │  Broker (WS)    │
          │  + SQLite       │
          └─────────────────┘
```

- **Broker**: WebSocket server + SQLite message store
- **MCP shim**: 3 tools (`channel_join`, `channel_post`, `channel_wait_new`) installed into each agent CLI
- **Viewer**: terminal UI where you see all messages and type your own
- **Listening loop**: each agent blocks on `channel_wait_new(60s)` → processes message → posts reply → waits again

Agents respond automatically because they loop on `channel_wait_new`. When a message arrives, the tool returns instantly, the agent processes it in its own TUI (you see it thinking, reading files, writing code), then posts back.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `channel_join(room)` | Join a channel room |
| `channel_post(content, room)` | Send a message everyone can see |
| `channel_wait_new(room, timeout_s)` | Block until someone else posts (or timeout) |

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Claude Code and/or Codex CLI installed with valid auth

## Cross-Machine (coming soon)

The broker URL is a parameter. Change `ws://127.0.0.1:9100` to a remote address and agents on different machines join the same room. Zero code changes.

## Tests

```bash
uv run pytest -v    # 70 tests, ~8 seconds
```

## License

MIT
