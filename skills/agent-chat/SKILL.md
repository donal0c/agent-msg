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

## Loop Triggers (Claude Code) — MUST Arm A Cron

Whenever the user or an incoming instruction contains ANY of the following, you MUST arm a recurring loop on the Claude Code side before ending the turn. This is not a judgment call.

- "keep your side running", "keep running", "keep going"
- "until DONE", "until done", "stop on DONE"
- "max N turns", "up to N turns", "before exceeding N replies"
- Any handoff produced by `agent-msg kickoff` or `agent-msg loop-prompt`
- Any message that tells you to join a thread *and* wait for the peer to reply again

If you are joining a thread and only one reply is needed with no waiting on the peer (e.g. "send Codex this one message"), do not arm a loop. Otherwise, arm one.

See "Claude Code Loop Mechanics" below for exactly how.

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
4. Start the recurring loop for the current runtime:
   - In Codex, create a heartbeat automation attached to this thread using the Codex prompt from `kickoff`.
   - In Claude Code, arm a cron via `CronCreate` per "Claude Code Loop Mechanics" below. Do this before confirming to the user.
5. Give the user only the peer-runtime natural-language instruction if another app needs manual setup. Do not give them a slash-command recipe.
   - Claude Code: `Use agent-chat to join the conversation in thread thr_ab12cd as claude with peer codex. Goal: decide the review strategy. Keep your side running until DONE or before exceeding 4 substantive replies.`
   - Codex: `Use agent-chat to continue thread thr_ab12cd as codex with peer claude. Goal: decide the review strategy. Run this as a Codex heartbeat. Stop on DONE, when the thread is closed, or before exceeding 4 substantive replies.`
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
2. Use `agent-msg brief <thread> --as <self> --peer <peer> --json` first to get the state of play.
3. Use `agent-msg read <thread> --as <self> --json` for unread messages.
4. Use `agent-msg show <thread> --as <self> --json` only when unread messages or the brief lack enough context.
5. If the user asked to continue, send one substantive reply or start/continue the bounded loop as appropriate.
6. If there are no new messages, answer from the brief and keep it short.

### Stop, Done, Or Summarise

Use when the user says things like:

- "stop that"
- "close the agent chat"
- "mark it done"
- "summarise where they landed"
- "what was the final recommendation?"

Behavior:

1. Resolve the thread. For "summarise", "where landed", or "final recommendation", include closed/done threads in resolution.
2. For "stop" or "close", use `agent-msg close <thread>`.
3. For "done", send a final `DONE:` message with `agent-msg done`.
4. For summaries, use `agent-msg brief <thread> --json` first. Use `agent-msg show <thread> --json` when the user wants the decision detail or the brief is not enough.
5. If a scheduler/heartbeat is running for the current runtime, stop it when the thread is done or closed. In Claude Code that means calling `CronDelete` for the job armed by this skill — check `CronList` if you do not already have the job id.

## Conversation Briefs

Use `agent-msg brief` whenever the user asks a state-of-play question:

- "what did Codex say?"
- "where are we?"
- "what is this waiting on?"
- "continue that"
- "summarise where they landed"

The brief gives thread topic, status, turn budget, latest useful point, who is waiting on whom, and the next likely action. Treat it as the first read model for natural-language continuity.

When answering from a brief, sound like a person:

> Claude replied on `thr_ab12cd`. It is waiting on us. Budget is 1 of 4 turns used. The latest point is that the skill UX matters more than CLI internals.

Do not paste the raw brief unless the user asks for debugging details.

## Joining A Running Thread

Use when the user or a scheduler says things like:

- "Use agent-chat to continue thread thr_ab12cd as claude with peer codex"
- "Join the conversation with Codex on the review strategy"
- "join thread thr_ab12cd"
- "keep the Claude side of thr_ab12cd running"

Behavior:

1. Run `agent-msg doctor` if not already done this turn.
2. Resolve the thread from the explicit id, peer/topic, or latest active thread.
3. Read the current state (`agent-msg brief <thread> --as <self> --peer <peer> --json`, then `agent-msg read` for unread).
4. Send at most one substantive reply right now if one is useful.
5. **Arm the loop before ending the turn** if any Loop Trigger phrase matched (see "Loop Triggers" section). In Claude Code that means `CronCreate`; see "Claude Code Loop Mechanics" below. In Codex, create a heartbeat automation.
6. On each subsequent tick (when the cron fires):
   - `agent-msg brief <thread> --as <self> --peer <peer> --json`
   - `agent-msg read <thread> --as <self> --json`
   - If no unread messages, stop the tick quietly (do not reply).
   - If the thread is done or closed, call `CronDelete` for this thread's job and stop.
   - If a reply is useful, send exactly one concise substantive reply.
   - If the conversation is complete, send the final reply prefixed with `DONE:` and then `CronDelete` the job.
7. Never let the loop exceed the stated turn budget. Count substantive replies. When the budget is reached, stop replying, `CronDelete` the job, and tell the user.

This is the abstraction layer for runtime loops. The user should be able to say "join the conversation with Codex on X" and trust the skill to start the loop.

## Claude Code Loop Mechanics

The Claude Code loop runs on top of `CronCreate` (session-only by default). Use this exact recipe.

### Step 1 — Load the scheduling tools if deferred

`CronCreate`, `CronDelete`, and `CronList` may be deferred in this session. If they are not already callable, fetch their schemas first:

```
ToolSearch query="select:CronCreate,CronDelete,CronList"
```

You only need to do this once per session.

### Step 2 — Get the tick prompt

```
agent-msg loop-prompt <thread_id> --as <self> --peer <peer> --max-turns <N>
```

The output is the prompt body. It is NOT a loop — it is what the cron will run on each tick. Capture the full text.

### Step 3 — Arm the cron

Call `CronCreate` with:

- `cron`: `"*/2 * * * *"` for active multi-turn discussions (every 2 minutes). Use `"*/5 * * * *"` for slower-paced threads. Never 0 or 30 on the minute.
- `prompt`: the text from Step 2, verbatim.
- `recurring`: `true`
- `durable`: `false` (session-only; the cron should not outlive the conversation)

Record the returned job id. Mention the thread id in a comment inside the prompt so future `CronList` output makes the link obvious.

### Step 4 — Stop conditions

Call `CronDelete` with the job id when ANY of these happen:

- You send a reply beginning with `DONE:`.
- A peer message begins with `DONE:` (the thread is finished).
- The thread status becomes `closed` or `done`.
- The substantive-reply budget is reached.
- The user says "stop", "close", or "cancel the loop".

If you are unsure whether a job is still live, call `CronList` and match by thread id in the prompt.

### Notes

- `CronCreate` jobs only fire while the REPL is idle. That is fine — the peer's reply can wait a couple of minutes.
- Do not use `ScheduleWakeup`. That tool is for `/loop` dynamic-pacing mode, not for this skill.
- Do not set `durable: true`. These loops are tied to the current session.

## Reference Resolution

The user will often say "that", "it", "the Codex chat", or "the previous conversation".

Resolution order:

1. Explicit thread id in the user message.
2. Latest active thread with the named peer.
3. Latest active thread for the current agent.
4. Ask a short clarifying question if multiple active threads are plausible.

Use `agent-msg inbox --as <self> --json` or `agent-msg latest --as <self> --peer <peer> --json` to resolve these references. Add `--closed` when the user is asking about a completed or stopped conversation. Do not ask the user for a thread id unless the local state is genuinely ambiguous.

## Human-Facing Output

Keep confirmations short and natural. Prefer:

> I started a bounded Claude conversation about the dispatcher design. Thread: `thr_ab12cd`. I will keep my side active for up to 4 turns and stop when a final recommendation lands.

Avoid exposing command recipes unless setup failed or the user explicitly asks for the CLI details.

When another runtime needs manual setup, give exactly one natural-language instruction for that runtime and explain where to say it. Do not include slash commands unless the user explicitly asks for them.

## Guardrails

- Never create an unbounded loop. Always pair `CronCreate` with a clear stop condition and budget.
- If a Loop Trigger phrase was present, arming the cron is mandatory, not optional. Ending the turn without arming it is a failure of this skill.
- Send at most one substantive reply per loop tick.
- Do not reply just to acknowledge.
- Stop once a message begins with `DONE:` or the thread is closed. Stopping means `CronDelete`, not just "don't reply".
- Ask the user before exceeding the max-turn budget.
- Keep cross-runtime handoffs as natural-language instructions; route behavior through this skill rather than pasting slash commands or command recipes.
- Prefer `--json` whenever parsing output.
- Use `agent-msg brief --json` before answering state-of-play questions.
- Use `agent-msg show --json` for summaries and reference recovery.
- Treat the CLI as plumbing. The user-facing interface is natural language.
