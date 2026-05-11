# agent-msg

`agent-msg` is a local mailbox for bounded conversations between Claude Code,
Codex, and other agent sessions.

The user-facing product is the `agent-chat` skill. The CLI is hidden transport:
agents use it to create threads, exchange messages, track unread state, and keep
multi-turn conversations bounded.

## Say Things Naturally

Use normal language with Codex or Claude Code:

```text
Chat to Codex about the dispatcher design and keep going until there is a recommendation.
```

```text
Ask Claude whether this review strategy is too narrow.
```

```text
What did Codex say?
```

```text
Continue that conversation and ask one more thing.
```

```text
Stop the agent chat and summarise where they landed.
```

The skill translates those requests into local mailbox operations. You should
not need to remember thread commands, cursor behavior, or loop prompts.

## What The Skill Does

- Creates short thread ids like `thr_ab12cd`
- Sends markdown/text messages between agent sessions
- Resolves phrases like "that conversation" or "the Codex chat"
- Tracks unread messages separately per agent
- Tracks participants, status, goals, and turn budgets
- Produces compact conversation briefs for "where are we?" moments
- Marks conversations `done` when a message begins with `DONE:`
- Generates bounded Claude `/loop` and Codex heartbeat prompts when needed
- Keeps the user-facing response short and human

It does **not** run unbounded autonomous chats. Conversations stop on `DONE:`,
when closed, or when the max-turn budget is reached.

## How Multi-Turn Discussion Works

`agent-msg` does not wake another app by itself. The skill starts the current
runtime's side when possible and gives you one paste-ready block for the other
runtime only when manual setup is needed.

Typical flow:

1. You ask naturally: "Chat to Claude about X until there is a recommendation."
2. The current agent creates a thread and sends the opening message.
3. The current runtime starts its bounded loop if possible.
4. You paste one generated block into the peer runtime if needed.
5. The agents exchange useful replies until one sends `DONE:` or the turn budget is reached.
6. You ask naturally for the result: "summarise where they landed."

## Conversation Briefs

The nicest part of the workflow is the brief: a compact state of play that the
skill can use whenever you ask "what did Codex say?", "continue that", or
"where are we?"

Example:

```text
Thread: dispatcher design with Claude
Status: waiting on codex
Budget: 1 of 4 turns used
Latest: Claude thinks the skill UX matters more than CLI internals.
Next: Read the latest message and decide whether one useful reply is needed.
```

The CLI generates the facts. The skill turns them into a natural reply.

## Install The CLI

Clone the repo:

```bash
git clone https://github.com/donal0c/agent-msg.git
cd agent-msg
```

Use the wrapper directly:

```bash
./agent-msg --help
```

Or symlink it onto your `PATH`:

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/agent-msg" ~/.local/bin/agent-msg
```

Verify:

```bash
agent-msg doctor
```

## Install The Skill

The shared skill lives inside this package:

```text
skills/agent-chat/SKILL.md
```

Install it into both runtimes with symlinks:

```bash
mkdir -p ~/.codex/skills ~/.claude/skills
ln -sf "$(pwd)/skills/agent-chat" ~/.codex/skills/agent-chat
ln -sf "$(pwd)/skills/agent-chat" ~/.claude/skills/agent-chat
```

This keeps this repo as the source of truth: edit the skill here and both Codex
and Claude Code see the same workflow.

## Developer CLI

Most users should interact through natural language, but the transport is still
inspectable when debugging.

Create a bounded thread and first message:

```bash
agent-msg kickoff "dispatcher design" --from claude --to codex \
  --max-turns 4 \
  "Discuss this and converge on a recommendation."
```

Read unread messages:

```bash
agent-msg read thr_ab12cd --as codex --json
```

Show thread metadata and transcript:

```bash
agent-msg show thr_ab12cd --as codex --json
```

Get a compact state-of-play brief:

```bash
agent-msg brief thr_ab12cd --as codex --peer claude --json
agent-msg brief --as codex --peer claude --json
agent-msg brief --as codex --peer claude --closed --json
```

Resolve recent work for natural-language references:

```bash
agent-msg latest --as codex --peer claude --json
agent-msg inbox --as codex --json
```

Finish or stop:

```bash
agent-msg done thr_ab12cd --as codex "Final recommendation..."
agent-msg close thr_ab12cd
```

## Storage

By default, messages live in:

```text
~/.agent-msg/messages.sqlite
```

Override per command:

```bash
agent-msg --db /path/to/messages.sqlite inbox
```

Or per shell:

```bash
export AGENT_MSG_DB=/path/to/messages.sqlite
```

## Design Principles

- Natural language first
- Local only
- SQLite only
- No daemon
- No network
- No task management
- No knowledge store
- Bounded loops only
- CLI as transparent plumbing, not the product surface

When an agent sends a message, its own read cursor advances to that message.
Future reads show replies rather than echoing the sender's own message.

## Development

Run tests:

```bash
python3 -m pytest -q
python3 -m py_compile agent_msg.py test_agent_msg.py
```

Package layout:

```text
agent-msg/
├── agent-msg
├── agent_msg.py
├── test_agent_msg.py
├── README.md
└── skills/
    └── agent-chat/
        └── SKILL.md
```
