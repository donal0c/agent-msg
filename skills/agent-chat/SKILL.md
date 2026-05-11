---
name: agent-chat
description: Use this skill when the user wants Claude Code, Codex, or another local agent session to send messages, check messages, or hold a bounded multi-turn discussion through natural language. Triggers include "agent message", "agent-msg", "send this to Codex", "ask Claude", "chat to Codex", "talk to Claude", "discuss this with Codex", "coordinate with Claude", "check what Codex said", "continue that conversation", "stop the agent chat", or "summarise where they landed".
---

# Agent Chat

You are the natural-language interface for bounded agent-to-agent collaboration.
The user should not need to know, type, or remember `agent-msg` commands. Use the CLI yourself as a hidden local transport, then report back in plain language.

## First Check

Run `agent-msg doctor` before the first transport action in a turn. If it fails, tell the user the CLI is unavailable and include the failing command/output. Do not continue as though the message was sent.

## Runtime Identity

Use the current runtime identity unless the user explicitly says otherwise:

- Codex: `--as codex`
- Claude Code: `--as claude`

When talking to the other common runtime:

- In Codex, the peer is usually `claude`.
- In Claude Code, the peer is usually `codex`.

If the peer is ambiguous and the action would send or schedule a message to the wrong agent, ask one concise clarifying question.

## Intent Router

Map the user's natural language to one of these workflows.

### One-Off Send

Use when the user says things like:

- "ask Codex what it thinks about X"
- "send this to Claude"
- "tell Codex I disagree and ask it to revisit the risk"

Behavior:

1. If the user gave a thread id, send to that thread.
2. If they refer to "that chat", "the Codex conversation", or similar, resolve it with `agent-msg latest --as <self> --peer <peer> --json`.
3. If there is no resolvable thread, create a fresh thread or use `kickoff` when a reply is expected.
4. Send exactly the user's intended message, with enough context for the peer to act.
5. Return the thread id and a short human confirmation. Do not show the command.

### Start A Bounded Discussion

Use when the user says things like:

- "chat to Codex about X"
- "discuss this with Claude"
- "coordinate with Codex until there is a recommendation"
- "get Claude and Codex to decide"
- "keep going until you agree"

Behavior:

1. Use `agent-msg kickoff` with `--from <self>`, `--to <peer>`, and a short topic.
2. Default to `--max-turns 4` unless the user gives a different budget.
3. Include the user's goal in the opening message. If the user asks for a recommendation, decision, division of work, or critique, make that deliverable explicit.
4. Start the recurring loop for the current runtime when possible:
   - In Codex, create a heartbeat automation attached to this thread using the Codex prompt from `kickoff`.
   - In Claude Code, use the generated `/loop` prompt when available.
5. Give the user only the peer-runtime paste block if another app needs manual setup.
6. Confirm in human language: topic, peer, thread id, turn budget, and stop condition.

Do not create unbounded autonomous chats.

### Check Or Continue

Use when the user says things like:

- "what did Codex say?"
- "check the Claude chat"
- "continue that conversation"
- "ask it one more thing"

Behavior:

1. Resolve the target thread with an explicit thread id or `agent-msg latest --as <self> --peer <peer> --json`.
2. Use `agent-msg read <thread> --as <self> --json` for unread messages.
3. Use `agent-msg show <thread> --as <self> --json` only when unread messages lack enough context.
4. If the user asked to continue, send one substantive reply or start/continue the bounded loop as appropriate.
5. If there are no new messages, say that plainly and keep it short.

### Stop, Done, Or Summarise

Use when the user says things like:

- "stop that"
- "close the agent chat"
- "mark it done"
- "summarise where they landed"
- "what was the final recommendation?"

Behavior:

1. Resolve the thread.
2. For "stop" or "close", use `agent-msg close <thread>`.
3. For "done", send a final `DONE:` message with `agent-msg done`.
4. For summaries, use `agent-msg show <thread> --json`, then give the user a concise synthesis with the actual thread id.
5. If a scheduler/heartbeat is running for the current runtime, stop it when the thread is done or closed.

## Reference Resolution

The user will often say "that", "it", "the Codex chat", or "the previous conversation".

Resolution order:

1. Explicit thread id in the user message.
2. Latest active thread with the named peer.
3. Latest active thread for the current agent.
4. Ask a short clarifying question if multiple active threads are plausible.

Use `agent-msg inbox --as <self> --json` or `agent-msg latest --as <self> --peer <peer> --json` to resolve these references. Do not ask the user for a thread id unless the local state is genuinely ambiguous.

## Human-Facing Output

Keep confirmations short and natural. Prefer:

> I started a bounded Claude conversation about the dispatcher design. Thread: `thr_ab12cd`. I will keep my side active for up to 4 turns and stop when a final recommendation lands.

Avoid exposing command recipes unless setup failed or the user explicitly asks for the CLI details.

When another runtime needs manual setup, give exactly one paste-ready block for that runtime and explain why it is needed.

## Guardrails

- Never create an unbounded loop.
- Send at most one substantive reply per loop tick.
- Do not reply just to acknowledge.
- Stop once a message begins with `DONE:` or the thread is closed.
- Ask the user before exceeding the max-turn budget.
- Prefer `--json` whenever parsing output.
- Use `agent-msg show --json` for summaries and reference recovery.
- Treat the CLI as plumbing. The user-facing interface is natural language.
