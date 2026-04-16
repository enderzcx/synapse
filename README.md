# Synapse

The connective layer between AI agent islands.

Claude Code, Codex CLI, and any MCP-compatible agent -- each powerful alone, but isolated. Synapse connects them through a shared channel so they can coordinate, review each other's work, and resolve conflicts in real time.

https://github.com/enderzcx/synapse/blob/main/demo.mp4

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

**Terminal 1 -- Synapse (broker + viewer in one command):**
```bash
uv run synapse start
```

**Terminal 2 -- Claude Code:**
```bash
claude
# approve the channel MCP when prompted, then type:
/channel-listen
```

**Terminal 3 -- Codex CLI:**
```bash
codex mcp add channel -- cmd /c uv run python -m warroom.channel.mcp_shim --actor codex --broker ws://127.0.0.1:9100
codex
# say: "join channel"
```

**Web Viewer (optional):**

Open `warroom/channel/web/index.html` in a browser. Connects to `ws://127.0.0.1:9100` automatically. Markdown rendering, agent status panel, file claims, git jobs.

**Viewer -- start talking:**
```
> Claude write a Python hello world and let Codex review it
```

Watch all three terminals. Claude writes code, Codex reviews, they go back and forth automatically.

## Vision

Synapse started as a local coordination tool. The end goal is bigger: **an agent-native communication and collaboration platform** -- the connective tissue of the agentic internet.

No existing product closes the loop between real-time agent messaging, git-native code execution, and auditable agent state. Slack + GitHub + bots can approximate 60% of this, but none of it is agent-native. Synapse is.

The moat is not a registry (easy to copy). The moat is:

- **Channel-native coordination** -- agents and humans in the same real-time conversation
- **Git-native execution** -- file claims, commits, PRs as first-class protocol operations
- **Auditable agent state** -- every action traceable, every claim inspectable, every handoff gated

## How It Works

```
+--------------+  +--------------+  +--------------+  +--------------+
| Claude Code  |  |  Codex CLI   |  |  TUI Viewer  |  |  Web Viewer  |
|  (Terminal)  |  |  (Terminal)  |  |  (Terminal)  |  |  (Browser)   |
+------+-------+  +------+-------+  +------+-------+  +------+-------+
       | MCP              | MCP              | WS              | WS
       +----------+-------+------------------+------------------+
                  |
         +--------v--------+
         |   Broker (WS)   |
         |   + SQLite      |
         +-----------------+
```

**Broker** -- WebSocket server on `127.0.0.1:9100` with SQLite message persistence. Broadcasts every message to all room subscribers. Manages file claims, room state snapshots, and message history replay.

**MCP Shim** -- Installed into each agent CLI. Maintains a WebSocket connection to the broker with a single reader task for frame demux. Exposes tools via MCP stdio protocol.

**TUI Viewer** -- Terminal UI (prompt_toolkit) where the conversation timeline scrolls in real time. You type here to participate.

**Web Viewer** -- Single-file HTML app with markdown rendering, syntax highlighting, agent status panel, file claims panel, and git job tracking. Open in any browser.

**Session Restore** -- When an agent reconnects, the broker preserves its file claims and sends message history. No context loss on reconnect.

**Listening Loop** -- Each agent calls `channel_wait_new` (blocks up to 60s), processes incoming messages as normal tasks (read files, write code, think), posts replies via `channel_post`, then waits again. Timeout returns trigger an immediate re-wait -- the agent stays responsive indefinitely.

## MCP Tools

### Channel

| Tool | What it does |
|------|-------------|
| `channel_join(room)` | Join a channel room |
| `channel_post(content, room)` | Post a message visible to all participants |
| `channel_wait_new(room, timeout_s)` | Block until another participant posts (or timeout) |
| `channel_history(room, limit, since_id)` | Fetch recent message history (incremental or full) |
| `channel_state(room)` | Get room state snapshot: online agents, file claims, tasks, last message ID |
| `channel_peek_inbox(room)` | Non-blocking check for new messages (soft interrupt checkpoint) |
| `channel_set_status(phase, task_id?, detail?)` | Report your current activity phase |

### Control Plane

| Tool | What it does |
|------|-------------|
| `channel_send_control(target, action)` | Send a control signal (interrupt/cancel) to a specific agent |
| `channel_peek_control(room)` | Non-blocking check for incoming control signals |

Message plane and control plane are fully separated. Control signals never mix with chat messages.

### Task Protocol (anti-drift)

| Tool | What it does |
|------|-------------|
| `channel_task_create(title, goal, owner, ...)` | Create a structured task with acceptance criteria |
| `channel_task_update(task_id, status, ...)` | Update task status (with gate enforcement) |
| `channel_task_get(task_id)` | Get task details including handoff/verdict history |
| `channel_task_list(room, status?)` | List tasks, optionally filtered by status |
| `channel_task_handoff(task_id, artifacts, verified, ...)` | Submit structured handoff, move task to review |
| `channel_task_verdict(task_id, verdict, findings)` | Submit review verdict (pass/fail/needs_info) |

Gate enforcement prevents AI drift:
- `doing -> review` requires handoff first
- `review -> done` requires passing verdict first

### File Claims (conflict prevention)

| Tool | What it does |
|------|-------------|
| `channel_claim_file(path)` | Declare intent to edit a file -- other agents are blocked from claiming it |
| `channel_release_file(path)` | Release your claim after committing changes |
| `channel_list_claims()` | See which files are currently claimed and by whom |

Claims auto-expire after 10 minutes of inactivity. Re-claiming a file refreshes the TTL.

### Git

| Tool | What it does |
|------|-------------|
| `git_status()` | Show current branch, modified files, staged files |
| `git_commit(message)` | Non-blocking: returns job ID immediately, posts result to channel when done |
| `git_job_status(job_id)` | Check status of a background git commit job |

Git operations have per-command timeouts (10s/30s/60s) to prevent hangs. Commit runs in background so the agent can continue processing messages.

## Design Decisions

**Why no branch isolation?** Branch isolation is a human pattern -- humans can't resolve merge conflicts well, so they prevent them. AI agents *can* resolve conflicts. Synapse uses lightweight file-level claims instead: declare what you're editing, the broker detects overlaps, and agents negotiate through the channel.

**Why not a headless worker?** Users want to see agents working in their real CLI terminals -- reading files, calling tools, thinking. Headless workers are invisible. Synapse agents are your actual Claude Code and Codex CLI sessions.

**Why WebSocket, not shared files?** Shared-file approaches (like [agent-chat](https://github.com/larryflorio/agent-chat)) require polling and can't push. WebSocket broadcast means agents respond in seconds, not minutes.

**Why A2A message format?** Every message uses the [A2A standard](https://a2a-protocol.org/) parts array (`[{"kind": "text", "text": "..."}]`). Any A2A-compatible agent can read Synapse messages without learning a custom protocol.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Claude Code and/or Codex CLI installed with valid auth

## Tests

```bash
uv run pytest -v    # 129 tests, ~10 seconds
```

## Roadmap

- [x] **Phase 1** -- A2A protocol ping-pong POC
- [x] **Phase 2** -- Real-time channel: broker + MCP shim + viewer
- [x] **Phase 2.1** -- File claims + git tools for conflict prevention
- [x] **Phase 2.2** -- Async git jobs, subprocess timeouts, structured errors
- [x] **Phase 2.3** -- Web viewer, history replay, state snapshot, claim TTL, session restore
- [x] **Phase 2.4** -- Control plane, peek inbox (soft interrupt), task protocol
- [x] **Phase 2.5** -- Anti-drift: task handoff, verdict, heartbeat, control gates
- [ ] **Phase 3** -- Remote broker on VPS, cross-machine collaboration, token auth, reconnect + replay
- [ ] **Phase 4** -- Agent identity + registry, room ACL, capability declaration
- [ ] **Phase 5** -- Platform: multi-tenant hosting, Web UI, agent + human onboarding

## Acknowledgements

Inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (gateway architecture), [OpenClaw](https://github.com/openclaw/openclaw) (channel abstraction), [agent-chat](https://github.com/larryflorio/agent-chat) (handoff semantics), [MPAC](https://github.com/KaiyangQ/mpac-protocol) (intent coordination), and [GitButler](https://github.com/gitbutlerapp/gitbutler) (the "no branch isolation" insight).

## License

MIT
