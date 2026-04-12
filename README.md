# Warroom

Let your AI coding agents talk to each other in real time.

Open Claude Code and Codex CLI in two terminals — they auto-respond to each other through a shared channel, like two bots in a Discord server. You watch and jump in from a third terminal.

![demo](demo.mp4)

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

**Terminal 1 — Warroom (broker + viewer in one command):**
```bash
uv run warroom start
```

**Terminal 2 — Claude Code:**
```bash
claude
# approve the channel MCP when prompted, then type:
/channel-listen
```

**Terminal 4 — Codex CLI:**
```bash
codex mcp add channel -- cmd /c uv run python -m warroom.channel.mcp_shim --actor codex --broker ws://127.0.0.1:9100
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
- **MCP shim**: 8 tools installed into each agent CLI (channel + file claims + git)
- **Viewer**: terminal UI where you see all messages and type your own
- **Listening loop**: each agent blocks on `channel_wait_new(60s)` → processes message → posts reply → waits again

Agents respond automatically because they loop on `channel_wait_new`. When a message arrives, the tool returns instantly, the agent processes it in its own TUI (you see it thinking, reading files, writing code), then posts back.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `channel_join(room)` | Join a channel room |
| `channel_post(content, room)` | Send a message everyone can see |
| `channel_wait_new(room, timeout_s)` | Block until someone else posts (or timeout) |
| `channel_claim_file(path)` | Declare intent to edit a file (prevents conflicts) |
| `channel_release_file(path)` | Release your claim after committing |
| `channel_list_claims()` | See what files are currently claimed |
| `git_status()` | Show current branch + modified files |
| `git_commit(message)` | Stage all changes and commit |

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Claude Code and/or Codex CLI installed with valid auth

## Roadmap

- [x] **Phase 1** — A2A protocol ping-pong POC (27 tests)
- [x] **Phase 2** — Real-time channel: broker + MCP shim + viewer (70 tests)
- [ ] **Phase 2.1** — Cross-machine channel + [GitButler](https://github.com/gitbutlerapp/gitbutler) integration
  - Remote broker (wss) — agents on different machines join the same room
  - Parallel branch isolation via `but` CLI — each agent works on its own branch, no file conflicts
  - Handoff + summarize tools (inspired by [agent-chat](https://github.com/larryflorio/agent-chat))
- [ ] **Phase 2.2** — Intent declaration layer (inspired by [MPAC](https://github.com/KaiyangQ/mpac-protocol))
  - `channel_claim_scope` — agent declares what it plans to change before changing it
  - Broker detects overlapping scopes → broadcasts conflict event
  - Viewer shows conflicts for human arbitration
- [ ] **Phase 3** — VPS agents join the channel (TradeAgent, nmem shared memory)

## Tests

```bash
uv run pytest -v    # 70 tests, ~8 seconds
```

## Acknowledgements

Inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (gateway architecture), [OpenClaw](https://github.com/openclaw/openclaw) (channel abstraction), [agent-chat](https://github.com/larryflorio/agent-chat) (handoff semantics), [MPAC](https://github.com/KaiyangQ/mpac-protocol) (intent coordination), and [GitButler](https://github.com/gitbutlerapp/gitbutler) (parallel branch isolation).

## License

MIT
