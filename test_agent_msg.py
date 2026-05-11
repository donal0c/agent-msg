import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CLI = ROOT / "agent_msg.py"
SKILL = ROOT / "skills" / "agent-chat" / "SKILL.md"


def run_cmd(tmp_path, *args, input_text=None):
    db = tmp_path / "messages.sqlite"
    cmd = [sys.executable, str(CLI), "--db", str(db), *args]
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )


def run_raw(tmp_path, *args, input_text=None):
    db = tmp_path / "messages.sqlite"
    cmd = [sys.executable, str(CLI), "--db", str(db), *args]
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def test_thread_send_read_cursor_flow(tmp_path):
    created = run_cmd(tmp_path, "--json", "new", "handoff", "--as", "claude")
    thread_id = json.loads(created.stdout)["id"]

    run_cmd(tmp_path, "send", thread_id, "--as", "claude", "Plan from Claude")

    codex_read = run_cmd(tmp_path, "--json", "read", thread_id, "--as", "codex")
    messages = json.loads(codex_read.stdout)["messages"]
    assert [message["body"] for message in messages] == ["Plan from Claude"]

    codex_second_read = run_cmd(tmp_path, "--json", "read", thread_id, "--as", "codex")
    assert json.loads(codex_second_read.stdout)["messages"] == []

    claude_read = run_cmd(tmp_path, "--json", "read", thread_id, "--as", "claude")
    assert json.loads(claude_read.stdout)["messages"] == []


def test_stdin_send_and_unread_list(tmp_path):
    created = run_cmd(tmp_path, "--json", "new", "stdin test", "--as", "claude")
    thread_id = json.loads(created.stdout)["id"]

    run_cmd(tmp_path, "send", thread_id, "--as", "claude", input_text="From stdin\n")

    listed = run_cmd(tmp_path, "--json", "list", "--as", "codex")
    threads = json.loads(listed.stdout)["threads"]
    assert threads[0]["id"] == thread_id
    assert threads[0]["unread"] == 1


def test_wait_returns_existing_unread_and_advances_cursor(tmp_path):
    created = run_cmd(tmp_path, "--json", "new", "wait test", "--as", "claude")
    thread_id = json.loads(created.stdout)["id"]
    run_cmd(tmp_path, "send", thread_id, "--as", "claude", "Ready for Codex")

    waited = run_cmd(tmp_path, "--json", "wait", thread_id, "--as", "codex", "--timeout", "0")
    payload = json.loads(waited.stdout)
    assert payload["timed_out"] is False
    assert [message["body"] for message in payload["messages"]] == ["Ready for Codex"]

    waited_again = run_cmd(
        tmp_path,
        "--json",
        "wait",
        thread_id,
        "--as",
        "codex",
        "--timeout",
        "0",
    )
    second_payload = json.loads(waited_again.stdout)
    assert second_payload["timed_out"] is True
    assert second_payload["messages"] == []


def test_loop_prompt_names_thread_agent_and_guardrails(tmp_path):
    result = run_cmd(
        tmp_path,
        "loop-prompt",
        "thr_test12",
        "--as",
        "claude",
        "--peer",
        "codex",
        "--max-turns",
        "3",
    )
    assert "Thread: `thr_test12`" in result.stdout
    assert "as `claude`" in result.stdout
    assert "Peer: codex" in result.stdout
    assert "more than 3 substantive reply turns" in result.stdout


def test_kickoff_creates_thread_sends_message_and_prints_prompts(tmp_path):
    result = run_cmd(
        tmp_path,
        "--json",
        "kickoff",
        "api shape",
        "--from",
        "claude",
        "--to",
        "codex",
        "--max-turns",
        "2",
        "Please discuss the API shape.",
    )
    payload = json.loads(result.stdout)
    thread_id = payload["thread_id"]
    assert payload["from"] == "claude"
    assert payload["to"] == "codex"
    assert "heartbeat" in payload["prompts"]["codex"]
    assert "/loop 2m" in payload["prompts"]["claude"]

    read = run_cmd(tmp_path, "--json", "read", thread_id, "--as", "codex")
    messages = json.loads(read.stdout)["messages"]
    assert [message["body"] for message in messages] == ["Please discuss the API shape."]


def test_kickoff_records_participants_and_latest_resolves_peer_thread(tmp_path):
    result = run_cmd(
        tmp_path,
        "--json",
        "kickoff",
        "review strategy",
        "--from",
        "codex",
        "--to",
        "claude",
        "--max-turns",
        "4",
        "Discuss the review strategy.",
    )
    thread_id = json.loads(result.stdout)["thread_id"]

    latest = run_cmd(tmp_path, "--json", "latest", "--as", "codex", "--peer", "claude")
    thread = json.loads(latest.stdout)["thread"]

    assert thread["id"] == thread_id
    assert thread["status"] == "open"
    assert thread["max_turns"] == 4
    participants = {participant["agent"]: participant["role"] for participant in thread["participants"]}
    assert participants == {"codex": "owner", "claude": "peer"}


def test_done_marks_thread_terminal_and_blocks_later_send(tmp_path):
    created = run_cmd(tmp_path, "--json", "new", "done test", "--as", "codex")
    thread_id = json.loads(created.stdout)["id"]

    done = run_cmd(tmp_path, "--json", "done", thread_id, "--as", "codex", "Final answer")
    payload = json.loads(done.stdout)
    assert payload["thread"]["status"] == "done"
    assert payload["message"]["body"] == "DONE: Final answer"

    blocked = run_raw(tmp_path, "send", thread_id, "--as", "claude", "One more thought")
    assert blocked.returncode != 0
    assert "is done" in blocked.stderr


def test_show_returns_metadata_and_transcript_for_summaries(tmp_path):
    created = run_cmd(tmp_path, "--json", "new", "summary test", "--as", "claude")
    thread_id = json.loads(created.stdout)["id"]
    run_cmd(tmp_path, "send", thread_id, "--as", "claude", "First")
    run_cmd(tmp_path, "send", thread_id, "--as", "codex", "Second")

    shown = run_cmd(tmp_path, "--json", "show", thread_id, "--as", "claude")
    payload = json.loads(shown.stdout)

    assert payload["thread"]["id"] == thread_id
    assert payload["thread"]["latest_seq"] == 2
    assert [message["body"] for message in payload["messages"]] == ["First", "Second"]


def test_agent_chat_skill_is_natural_language_first():
    text = SKILL.read_text()

    assert "The user should not need to know, type, or remember `agent-msg` commands" in text
    assert "Intent Router" in text
    assert "Reference Resolution" in text
    assert "Treat the CLI as plumbing. The user-facing interface is natural language." in text
