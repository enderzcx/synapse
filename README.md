# Synapse

The connective layer between AI agent islands.

Claude Code, Codex CLI, and any MCP-compatible agent — each powerful alone, but isolated. Synapse connects them through a shared channel so they can coordinate, review each other's work, and resolve conflicts in real time. You watch and jump in from a viewer terminal.

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
git clone https://github.com/enderzcx/synapse.git
cd synapse
uv sync --extra dev
```

**Terminal 1 — Synapse (broker + viewer in one command):**
```bash
uv run synapse start
```

**Terminal 2 — Claude Code:**
```bash
claude
# approve the channel MCP when prompted, then type:
/channel-listen
```

**Terminal 3 — Codex CLI:**
```bash
codex mcp add channel -- cmd /c uv run python -m warroom.channel.mcp_shim --actor codex --broker ws://127.0.0.1:9100
codex
# say: "join channel"
```

**Viewer — start talking:**
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
       │ MCP                │ MCP                │ WebSocket
       └───────────┬────────┴────────────────────┘
                   │
          ┌────────▼────────┐
          │  Broker (WS)    │
          │  + SQLite       │
          └─────────────────┘
```

**Broker** — WebSocket server on `127.0.0.1:9100` with SQLite message persistence. Broadcasts every message to all room subscribers. Manages file claims to prevent edit conflicts.

**MCP Shim** — Installed into each agent CLI. Maintains a WebSocket connection to the broker with a single reader task for frame demux. Exposes 8 tools via MCP stdio protocol.

**Viewer** — Terminal UI (prompt_toolkit) where the conversation timeline scrolls in real time. You type here to participate.

**Listening Loop** — Each agent calls `channel_wait_new` (blocks up to 60s), processes incoming messages as normal tasks (read files, write code, think), posts replies via `channel_post`, then waits again. Timeout returns trigger an immediate re-wait — the agent stays responsive indefinitely.

## MCP Tools

### Channel

| Tool | What it does |
|------|-------------|
| `channel_join(room)` | Join a channel room |
| `channel_post(content, room)` | Post a message visible to all participants |
| `channel_wait_new(room, timeout_s)` | Block until another participant posts (or timeout) |

### File Claims (conflict prevention)

| Tool | What it does |
|------|-------------|
| `channel_claim_file(path)` | Declare intent to edit a file — other agents are blocked from claiming it |
| `channel_release_file(path)` | Release your claim after committing changes |
| `channel_list_claims()` | See which files are currently claimed and by whom |

### Git

| Tool | What it does |
|------|-------------|
| `git_status()` | Show current branch, modified files, staged files |
| `git_commit(message)` | Stage all changes and commit |

## Design Decisions

**Why no branch isolation?** Branch isolation is a human pattern — humans can't resolve merge conflicts well, so they prevent them. AI agents *can* resolve conflicts. Synapse uses lightweight file-level claims instead: declare what you're editing, the broker detects overlaps, and agents negotiate through the channel.

**Why not a headless worker?** Users want to see agents working in their real CLI terminals — reading files, calling tools, thinking. Headless workers are invisible. Synapse agents are your actual Claude Code and Codex CLI sessions.

**Why WebSocket, not shared files?** Shared-file approaches (like [agent-chat](https://github.com/larryflorio/agent-chat)) require polling and can't push. WebSocket broadcast means agents respond in seconds, not minutes.

**Why A2A message format?** Every message uses the [A2A standard](https://a2a-protocol.org/) parts array (`[{"kind": "text", "text": "..."}]`). Any A2A-compatible agent can read Synapse messages without learning a custom protocol.

## Cross-Machine (Phase 3)

The broker URL is a parameter. Change `ws://127.0.0.1:9100` to a remote address (e.g. `wss://synapse.yourdomain.com/`) and agents on different machines join the same room. Zero code changes.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Claude Code and/or Codex CLI installed with valid auth

## Tests

```bash
uv run pytest -v    # 84 tests, ~10 seconds
```

## Roadmap

- [x] **Phase 1** — A2A protocol ping-pong POC (27 tests)
- [x] **Phase 2** — Real-time channel: broker + MCP shim + viewer (43 tests)
- [x] **Phase 2.1** — File claims + git tools for conflict prevention (12 tests)
- [ ] **Phase 3** — Cross-machine: remote broker (wss) + VPS agents (TradeAgent, nmem)
- [ ] **Phase 4** — Intent declaration layer (inspired by [MPAC](https://github.com/KaiyangQ/mpac-protocol))

## Acknowledgements

Inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (gateway architecture), [OpenClaw](https://github.com/openclaw/openclaw) (channel abstraction), [agent-chat](https://github.com/larryflorio/agent-chat) (handoff semantics), [MPAC](https://github.com/KaiyangQ/mpac-protocol) (intent coordination), and [GitButler](https://github.com/gitbutlerapp/gitbutler) (the "no branch isolation" insight).

## License

MIT
