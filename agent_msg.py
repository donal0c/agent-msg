#!/usr/bin/env python3
"""Small local mailbox CLI for agent-to-agent messages."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


APP_DIR = Path.home() / ".agent-msg"
DEFAULT_DB = APP_DIR / "messages.sqlite"
SCHEMA_VERSION = 2
TERMINAL_STATUSES = {"done", "closed"}


@dataclass
class Message:
    thread_id: str
    seq: int
    sender: str
    body: str
    created_at: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_agent() -> str:
    return os.environ.get("AGENT_MSG_AGENT") or os.environ.get("MSG_AGENT") or "agent"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            closed_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            max_turns INTEGER,
            goal TEXT,
            done_at TEXT,
            done_seq INTEGER
        );

        CREATE TABLE IF NOT EXISTS messages (
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, seq)
        );

        CREATE TABLE IF NOT EXISTS read_cursors (
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            agent TEXT NOT NULL,
            last_read_seq INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, agent)
        );

        CREATE TABLE IF NOT EXISTS thread_participants (
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            agent TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'participant',
            created_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, agent)
        );
        """
    )
    ensure_column(conn, "threads", "status", "TEXT NOT NULL DEFAULT 'open'")
    ensure_column(conn, "threads", "max_turns", "INTEGER")
    ensure_column(conn, "threads", "goal", "TEXT")
    ensure_column(conn, "threads", "done_at", "TEXT")
    ensure_column(conn, "threads", "done_seq", "INTEGER")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    now = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO thread_participants(thread_id, agent, role, created_at)
        SELECT id, created_by, 'owner', ?
        FROM threads
        WHERE created_by IS NOT NULL
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO thread_participants(thread_id, agent, role, created_at)
        SELECT DISTINCT thread_id, sender, 'participant', ?
        FROM messages
        """,
        (now,),
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def print_result(args: argparse.Namespace, data: Any, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(text)


def ensure_thread(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown thread: {thread_id}")
    return row


def add_participant(conn: sqlite3.Connection, thread_id: str, agent: str, role: str) -> None:
    conn.execute(
        """
        INSERT INTO thread_participants(thread_id, agent, role, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(thread_id, agent) DO NOTHING
        """,
        (thread_id, agent, role, now_iso()),
    )


def generate_thread_id(conn: sqlite3.Connection) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        suffix = "".join(secrets.choice(alphabet) for _ in range(6))
        thread_id = f"thr_{suffix}"
        exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if exists is None:
            return thread_id


def next_seq(conn: sqlite3.Connection, thread_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return int(row["next_seq"])


def set_cursor(conn: sqlite3.Connection, thread_id: str, agent: str, seq: int) -> None:
    conn.execute(
        """
        INSERT INTO read_cursors(thread_id, agent, last_read_seq, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(thread_id, agent) DO UPDATE SET
            last_read_seq = MAX(read_cursors.last_read_seq, excluded.last_read_seq),
            updated_at = excluded.updated_at
        """,
        (thread_id, agent, seq, now_iso()),
    )


def body_marks_done(body: str) -> bool:
    return body.lstrip().upper().startswith("DONE:")


def thread_status(row: sqlite3.Row) -> str:
    return row["status"] or ("closed" if row["closed_at"] else "open")


def insert_message(
    conn: sqlite3.Connection,
    thread_id: str,
    agent: str,
    body: str,
    *,
    allow_terminal: bool = False,
) -> Message:
    for _ in range(3):
        try:
            conn.execute("BEGIN IMMEDIATE")
            thread = ensure_thread(conn, thread_id)
            status = thread_status(thread)
            if status in TERMINAL_STATUSES and not allow_terminal:
                raise SystemExit(f"Thread {thread_id} is {status}. Reopen it before sending.")
            seq = next_seq(conn, thread_id)
            created_at = now_iso()
            conn.execute(
                """
                INSERT INTO messages(thread_id, seq, sender, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, seq, agent, body, created_at),
            )
            add_participant(conn, thread_id, agent, "participant")
            # A sender has already read their own message. This keeps later reads reply-focused.
            set_cursor(conn, thread_id, agent, seq)
            if body_marks_done(body):
                conn.execute(
                    """
                    UPDATE threads
                    SET status = 'done', done_at = ?, done_seq = ?
                    WHERE id = ? AND status != 'closed'
                    """,
                    (created_at, seq, thread_id),
                )
            conn.commit()
            return Message(thread_id, seq, agent, body, created_at)
        except sqlite3.IntegrityError:
            conn.rollback()
            time.sleep(0.05)
    raise SystemExit("Could not send message after retrying sequence allocation.")


def participant_rows(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.agent, p.role, p.created_at, COALESCE(rc.last_read_seq, 0) AS last_read_seq
        FROM thread_participants p
        LEFT JOIN read_cursors rc ON rc.thread_id = p.thread_id AND rc.agent = p.agent
        WHERE p.thread_id = ?
        ORDER BY p.created_at ASC, p.agent ASC
        """,
        (thread_id,),
    ).fetchall()


def thread_message_rows(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY seq ASC",
        (thread_id,),
    ).fetchall()


def thread_summary(conn: sqlite3.Connection, thread_id: str, agent: str | None = None) -> dict[str, Any]:
    thread = ensure_thread(conn, thread_id)
    messages = rows_to_messages(thread_message_rows(conn, thread_id))
    participants = [
        {
            "agent": row["agent"],
            "role": row["role"],
            "created_at": row["created_at"],
            "last_read_seq": int(row["last_read_seq"]),
        }
        for row in participant_rows(conn, thread_id)
    ]
    latest_seq = messages[-1].seq if messages else 0
    unread = 0
    if agent:
        cursor = conn.execute(
            "SELECT last_read_seq FROM read_cursors WHERE thread_id = ? AND agent = ?",
            (thread_id, agent),
        ).fetchone()
        last_read = int(cursor["last_read_seq"]) if cursor else 0
        unread = max(0, latest_seq - last_read)
    return {
        "id": thread["id"],
        "topic": thread["topic"],
        "created_at": thread["created_at"],
        "created_by": thread["created_by"],
        "closed_at": thread["closed_at"],
        "status": thread_status(thread),
        "max_turns": thread["max_turns"],
        "goal": thread["goal"],
        "done_at": thread["done_at"],
        "done_seq": thread["done_seq"],
        "latest_seq": latest_seq,
        "last_message_at": messages[-1].created_at if messages else None,
        "participants": participants,
        "unread": unread,
    }


def rows_to_messages(rows: list[sqlite3.Row]) -> list[Message]:
    return [
        Message(
            thread_id=row["thread_id"],
            seq=int(row["seq"]),
            sender=row["sender"],
            body=row["body"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def format_messages(messages: list[Message]) -> str:
    if not messages:
        return "No new messages."
    chunks = []
    for msg in messages:
        chunks.append(f"[{msg.thread_id} #{msg.seq} {msg.created_at} {msg.sender}]")
        chunks.append(msg.body)
    return "\n\n".join(chunks)


def unread_messages(
    conn: sqlite3.Connection,
    thread_id: str,
    agent: str,
    limit: int | None = None,
) -> list[Message]:
    cursor = conn.execute(
        "SELECT last_read_seq FROM read_cursors WHERE thread_id = ? AND agent = ?",
        (thread_id, agent),
    ).fetchone()
    last_read = int(cursor["last_read_seq"]) if cursor else 0
    params: tuple[Any, ...] = (thread_id, last_read)
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT ?"
        params = (*params, limit)
    rows = conn.execute(
        f"""
        SELECT * FROM messages
        WHERE thread_id = ? AND seq > ?
        ORDER BY seq ASC{limit_sql}
        """,
        params,
    ).fetchall()
    return rows_to_messages(rows)


def read_body(args: argparse.Namespace) -> str:
    if args.message and args.file:
        raise SystemExit("Provide message text or --file, not both.")
    if args.message:
        body = " ".join(args.message)
    elif args.file:
        body = Path(args.file).read_text()
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise SystemExit("Message body required. Pass text, --file PATH, or pipe stdin.")
    body = body.strip()
    if not body:
        raise SystemExit("Message body cannot be empty.")
    return body


def cmd_new(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        thread_id = generate_thread_id(conn)
        created_at = now_iso()
        conn.execute(
            """
            INSERT INTO threads(id, topic, created_at, created_by, max_turns, goal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, args.topic, created_at, agent, args.max_turns, args.goal),
        )
        add_participant(conn, thread_id, agent, "owner")
        conn.commit()
    data = {
        "id": thread_id,
        "topic": args.topic,
        "created_at": created_at,
        "created_by": agent,
        "status": "open",
        "max_turns": args.max_turns,
        "goal": args.goal,
    }
    print_result(args, data, f"{thread_id}  {args.topic}")


def cmd_send(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    body = read_body(args)
    with connect(args.db) as conn:
        message = insert_message(conn, args.thread_id, agent, body)
    data = asdict(message)
    print_result(args, data, f"sent {message.thread_id} #{message.seq} as {agent}")


def cmd_read(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        ensure_thread(conn, args.thread_id)
        if args.all:
            params: tuple[Any, ...] = (args.thread_id,)
            where = "thread_id = ?"
            limit_sql = ""
            if args.limit:
                limit_sql = " LIMIT ?"
                params = (*params, args.limit)
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY seq ASC{limit_sql}",
                params,
            ).fetchall()
            messages = rows_to_messages(rows)
        else:
            messages = unread_messages(conn, args.thread_id, agent, args.limit)
        if messages and not args.peek:
            set_cursor(conn, args.thread_id, agent, messages[-1].seq)
            conn.commit()
    data = {
        "thread_id": args.thread_id,
        "agent": agent,
        "peek": args.peek,
        "messages": [asdict(msg) for msg in messages],
    }
    print_result(args, data, format_messages(messages))


def cmd_wait(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    deadline = time.monotonic() + args.timeout
    messages: list[Message] = []
    timed_out = False

    while True:
        with connect(args.db) as conn:
            ensure_thread(conn, args.thread_id)
            messages = unread_messages(conn, args.thread_id, agent, args.limit)
            if messages:
                if not args.peek:
                    set_cursor(conn, args.thread_id, agent, messages[-1].seq)
                    conn.commit()
                break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(args.poll)

    data = {
        "thread_id": args.thread_id,
        "agent": agent,
        "peek": args.peek,
        "timed_out": timed_out,
        "messages": [asdict(msg) for msg in messages],
    }
    if timed_out:
        text = f"No new messages for {agent} on {args.thread_id} within {args.timeout}s."
    else:
        text = format_messages(messages)
    print_result(args, data, text)


def cmd_list(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        rows = conn.execute(
            """
            SELECT
                t.id,
                t.topic,
                t.created_at,
                t.created_by,
                t.closed_at,
                t.status,
                t.max_turns,
                t.goal,
                t.done_at,
                t.done_seq,
                COALESCE(MAX(m.seq), 0) AS latest_seq,
                COALESCE(rc.last_read_seq, 0) AS last_read_seq,
                MAX(m.created_at) AS last_message_at
            FROM threads t
            LEFT JOIN messages m ON m.thread_id = t.id
            LEFT JOIN read_cursors rc ON rc.thread_id = t.id AND rc.agent = ?
            WHERE (? OR t.status = 'open')
            GROUP BY t.id
            ORDER BY COALESCE(MAX(m.created_at), t.created_at) DESC
            LIMIT ?
            """,
            (agent, int(args.closed), args.limit),
        ).fetchall()
    threads = []
    for row in rows:
        latest_seq = int(row["latest_seq"])
        last_read_seq = int(row["last_read_seq"])
        threads.append(
            {
                "id": row["id"],
                "topic": row["topic"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "closed_at": row["closed_at"],
                "status": row["status"],
                "max_turns": row["max_turns"],
                "goal": row["goal"],
                "done_at": row["done_at"],
                "done_seq": row["done_seq"],
                "latest_seq": latest_seq,
                "last_read_seq": last_read_seq,
                "unread": max(0, latest_seq - last_read_seq),
                "last_message_at": row["last_message_at"],
            }
        )
    if args.json:
        print(json.dumps({"agent": agent, "threads": threads}, indent=2, sort_keys=True))
        return
    if not threads:
        print("No threads.")
        return
    for thread in threads:
        status = thread["status"] or ("closed" if thread["closed_at"] else "open")
        unread = f" unread={thread['unread']}" if thread["unread"] else ""
        print(f"{thread['id']}  {status}{unread}  {thread['topic']}")


def cmd_close(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        ensure_thread(conn, args.thread_id)
        closed_at = now_iso()
        conn.execute(
            "UPDATE threads SET status = 'closed', closed_at = COALESCE(closed_at, ?) WHERE id = ?",
            (closed_at, args.thread_id),
        )
        conn.commit()
    print_result(
        args,
        {"thread_id": args.thread_id, "status": "closed", "closed_at": closed_at},
        f"closed {args.thread_id}",
    )


def cmd_done(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    body = read_body(args)
    if not body_marks_done(body):
        body = f"DONE: {body}"
    with connect(args.db) as conn:
        message = insert_message(conn, args.thread_id, agent, body)
        summary = thread_summary(conn, args.thread_id, agent)
    data = {"message": asdict(message), "thread": summary}
    print_result(args, data, f"done {message.thread_id} #{message.seq} as {agent}")


def cmd_reopen(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        ensure_thread(conn, args.thread_id)
        conn.execute(
            """
            UPDATE threads
            SET status = 'open', closed_at = NULL, done_at = NULL, done_seq = NULL
            WHERE id = ?
            """,
            (args.thread_id,),
        )
        conn.commit()
        summary = thread_summary(conn, args.thread_id, args.as_agent or default_agent())
    print_result(args, summary, f"reopened {args.thread_id}")


def find_threads(
    conn: sqlite3.Connection,
    *,
    agent: str,
    peer: str | None,
    include_closed: bool,
    only_unread: bool,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [agent]
    where = []
    if not include_closed:
        where.append("t.status = 'open'")
    if peer:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM thread_participants pp
                WHERE pp.thread_id = t.id AND pp.agent = ?
            )
            """
        )
        params.append(peer)
    unread_where = ""
    if only_unread:
        unread_where = "HAVING latest_seq > last_read_seq"
    params.append(limit)
    sql = f"""
        SELECT
            t.id,
            t.topic,
            t.created_at,
            t.created_by,
            t.closed_at,
            t.status,
            t.max_turns,
            t.goal,
            t.done_at,
            t.done_seq,
            COALESCE(MAX(m.seq), 0) AS latest_seq,
            COALESCE(rc.last_read_seq, 0) AS last_read_seq,
            MAX(m.created_at) AS last_message_at
        FROM threads t
        LEFT JOIN messages m ON m.thread_id = t.id
        LEFT JOIN read_cursors rc ON rc.thread_id = t.id AND rc.agent = ?
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY t.id
        {unread_where}
        ORDER BY COALESCE(MAX(m.created_at), t.created_at) DESC
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    threads = []
    for row in rows:
        latest_seq = int(row["latest_seq"])
        last_read_seq = int(row["last_read_seq"])
        threads.append(
            {
                "id": row["id"],
                "topic": row["topic"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "closed_at": row["closed_at"],
                "status": row["status"],
                "max_turns": row["max_turns"],
                "goal": row["goal"],
                "done_at": row["done_at"],
                "done_seq": row["done_seq"],
                "latest_seq": latest_seq,
                "last_read_seq": last_read_seq,
                "unread": max(0, latest_seq - last_read_seq),
                "last_message_at": row["last_message_at"],
            }
        )
    return threads


def cmd_inbox(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        threads = find_threads(
            conn,
            agent=agent,
            peer=args.peer,
            include_closed=args.closed,
            only_unread=args.unread,
            limit=args.limit,
        )
    data = {"agent": agent, "peer": args.peer, "threads": threads}
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif not threads:
        print("No matching threads.")
    else:
        for thread in threads:
            unread = f" unread={thread['unread']}" if thread["unread"] else ""
            print(f"{thread['id']}  {thread['status']}{unread}  {thread['topic']}")


def cmd_latest(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        threads = find_threads(
            conn,
            agent=agent,
            peer=args.peer,
            include_closed=args.closed,
            only_unread=False,
            limit=1,
        )
        summary = thread_summary(conn, threads[0]["id"], agent) if threads else None
    data = {"agent": agent, "peer": args.peer, "thread": summary}
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif summary is None:
        print("No matching threads.")
    else:
        print(f"{summary['id']}  {summary['status']}  {summary['topic']}")


def cmd_show(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        summary = thread_summary(conn, args.thread_id, agent)
        messages = rows_to_messages(thread_message_rows(conn, args.thread_id))
    data = {
        "thread": summary,
        "messages": [asdict(message) for message in messages],
    }
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    participants = ", ".join(p["agent"] for p in summary["participants"]) or "none"
    lines = [
        f"{summary['id']}  {summary['status']}  {summary['topic']}",
        f"participants: {participants}",
    ]
    if summary["goal"]:
        lines.append(f"goal: {summary['goal']}")
    lines.append("")
    lines.append(format_messages(messages))
    print("\n".join(lines))


def compact_body(body: str, limit: int = 240) -> str:
    compact = " ".join(body.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}..."


def brief_for_thread(
    summary: dict[str, Any],
    messages: list[Message],
    agent: str,
    peer: str | None = None,
) -> dict[str, Any]:
    participants = [participant["agent"] for participant in summary["participants"]]
    named_peer = peer or next((participant for participant in participants if participant != agent), None)
    latest = messages[-1] if messages else None
    turns_used = max(0, len(messages) - 1)
    max_turns = summary["max_turns"]
    remaining = max(0, max_turns - turns_used) if max_turns is not None else None

    if summary["status"] == "done":
        waiting_on: list[str] = []
        state = "done"
        next_action = "Summarise the final recommendation or reopen the thread if more work is needed."
    elif summary["status"] == "closed":
        waiting_on = []
        state = "closed"
        next_action = "No reply is needed unless the user asks to reopen the thread."
    elif latest is None:
        waiting_on = [summary["created_by"] or agent]
        state = f"waiting on {waiting_on[0]}"
        next_action = "Send the opening message or close the empty thread."
    elif latest.sender == agent:
        waiting_on = [participant for participant in participants if participant != agent]
        state = f"waiting on {', '.join(waiting_on)}" if waiting_on else "waiting on peer"
        next_action = "Wait for the peer to reply, or ask the user before nudging."
    else:
        waiting_on = [agent]
        state = f"waiting on {agent}"
        next_action = "Read the latest message and decide whether one useful reply is needed."

    budget_label = "no turn budget"
    if max_turns is not None:
        budget_label = f"{turns_used} of {max_turns} turns used"
        if remaining == 0 and summary["status"] == "open":
            next_action = "The turn budget is exhausted; ask the user before continuing."

    latest_payload = None
    latest_point = "No messages yet."
    if latest is not None:
        latest_payload = asdict(latest)
        latest_point = compact_body(latest.body)

    title_parts = [summary["topic"]]
    if named_peer:
        title_parts.append(f"with {named_peer}")
    title = " ".join(part for part in title_parts if part)

    lines = [
        f"Thread: {title}",
        f"Status: {state}",
        f"Budget: {budget_label}",
        f"Latest: {latest_point}",
        f"Next: {next_action}",
    ]

    return {
        "title": title,
        "status": summary["status"],
        "state": state,
        "waiting_on": waiting_on,
        "budget": {
            "used": turns_used,
            "max": max_turns,
            "remaining": remaining,
            "label": budget_label,
        },
        "latest": latest_payload,
        "latest_point": latest_point,
        "next": next_action,
        "text": "\n".join(lines),
    }


def cmd_brief(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    with connect(args.db) as conn:
        thread_id = args.thread_id
        if thread_id is None:
            threads = find_threads(
                conn,
                agent=agent,
                peer=args.peer,
                include_closed=args.closed,
                only_unread=False,
                limit=1,
            )
            thread_id = threads[0]["id"] if threads else None
        if thread_id is None:
            data = {"agent": agent, "peer": args.peer, "thread": None, "brief": None}
            print_result(args, data, "No matching threads.")
            return
        summary = thread_summary(conn, thread_id, agent)
        messages = rows_to_messages(thread_message_rows(conn, thread_id))

    brief = brief_for_thread(summary, messages, agent, args.peer)
    data = {
        "agent": agent,
        "peer": args.peer,
        "thread": summary,
        "brief": brief,
    }
    print_result(args, data, brief["text"])


def cmd_doctor(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        thread_count = conn.execute("SELECT COUNT(*) AS n FROM threads").fetchone()["n"]
        message_count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        participant_count = conn.execute(
            "SELECT COUNT(*) AS n FROM thread_participants"
        ).fetchone()["n"]
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    data = {
        "db": str(args.db),
        "exists": args.db.exists(),
        "schema_version": int(schema_version),
        "journal_mode": journal_mode,
        "threads": int(thread_count),
        "messages": int(message_count),
        "participants": int(participant_count),
    }
    text = "\n".join(
        [
            f"db: {data['db']}",
            f"schema_version: {data['schema_version']}",
            f"journal_mode: {data['journal_mode']}",
            f"threads: {data['threads']}",
            f"messages: {data['messages']}",
            f"participants: {data['participants']}",
            "ok",
        ]
    )
    print_result(args, data, text)


AGENT_HELP = """\
agent-msg is a local mailbox for Claude Code, Codex, and other agents.

Use it when another agent/session needs to see a message without creating
handoff folders or ad hoc files.

Recommended agent workflow:

1. Set your identity for this terminal/session:
   export AGENT_MSG_AGENT=claude
   export AGENT_MSG_AGENT=codex
   export AGENT_MSG_AGENT=codex-reviewer

2. Start or find a thread for the user's natural-language intent:
   agent-msg kickoff "topic" --from claude --to codex "opening message"
   agent-msg latest --as claude --peer codex --json

3. Send or finish a message:
   agent-msg send thr_abc123 "Short message"
   cat plan.md | agent-msg send thr_abc123 --as claude
   agent-msg done thr_abc123 --as claude "Final recommendation..."

4. Read replies:
   agent-msg read thr_abc123
   agent-msg brief thr_abc123 --as claude --json
   agent-msg show thr_abc123 --json

5. Wait for the next reply when running inside a loop:
   agent-msg wait thr_abc123 --as claude --timeout 300

6. Generate bounded loop instructions for a runtime scheduler:
   agent-msg loop-prompt thr_abc123 --as claude --peer codex --max-turns 4

7. List active threads and unread counts:
   agent-msg inbox --as claude
   agent-msg brief --as claude --peer codex --json

Rules for agents:
- Prefer --as if AGENT_MSG_AGENT is not set.
- Use --json when you need machine-readable output.
- Use --peek to inspect messages without advancing your read cursor.
- Use wait when a scheduler should block until a peer message arrives.
- After sending, your cursor advances to your own message, so future reads
  show replies rather than echoing your own send.
- Share only the short thread id with the user, e.g. thr_abc123.
- Do not ask the user to run these commands unless setup/debugging failed.
"""


def cmd_help_agent(args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps({"help": AGENT_HELP}, indent=2))
    else:
        print(AGENT_HELP)


def cmd_loop_prompt(args: argparse.Namespace) -> None:
    agent = args.as_agent or default_agent()
    peer = args.peer or "the other agent"
    goal = args.goal or "coordinate with the peer agent and converge on a useful outcome"
    prompt = loop_prompt_text(args.thread_id, agent, peer, goal, args.max_turns)
    if args.json:
        print(
            json.dumps(
                {"thread_id": args.thread_id, "agent": agent, "peer": peer, "prompt": prompt},
                indent=2,
            )
        )
    else:
        print(prompt)


def loop_prompt_text(
    thread_id: str,
    agent: str,
    peer: str,
    goal: str,
    max_turns: int,
) -> str:
    return f"""\
You are participating in an agent-msg conversation as `{agent}`.

Thread: `{thread_id}`
Peer: {peer}
Goal: {goal}

On each loop tick:
1. Run: `agent-msg read {thread_id} --as {agent} --json`
2. If there are no messages, report briefly that there is no new message and stop this tick.
3. If there are messages, read them carefully and decide whether a reply is needed.
4. If a reply is needed, send exactly one concise message:
   `agent-msg send {thread_id} --as {agent} "your message"`
5. If the discussion is complete, include `DONE:` at the start of your final message and stop replying on later ticks unless new information appears.

Guardrails:
- Do not run more than {max_turns} substantive reply turns for this thread without asking the user.
- Do not reply just to acknowledge; only reply when you add useful information, ask a needed question, report progress, or mark the thread done.
- Keep messages short enough for the other agent to act on.
- Use `agent-msg read {thread_id} --as {agent} --all` if you need the full history.
"""


def codex_heartbeat_prompt(thread_id: str, agent: str, peer: str, goal: str, max_turns: int) -> str:
    return (
        f"Use agent-chat to continue thread {thread_id} as {agent} with peer {peer}. "
        f"Goal: {goal}. Run this as a Codex heartbeat. Stop on DONE, when the thread "
        f"is closed, or before exceeding {max_turns} substantive replies."
    )


def claude_loop_prompt(thread_id: str, agent: str, peer: str, goal: str, max_turns: int) -> str:
    return (
        f"Use agent-chat to join the conversation in thread {thread_id} as {agent} "
        f"with peer {peer}. Goal: {goal}. Keep your side running until DONE or before "
        f"exceeding {max_turns} substantive replies."
    )


def runtime_loop_prompt(thread_id: str, agent: str, peer: str, goal: str, max_turns: int) -> str:
    lower = agent.lower()
    if "codex" in lower:
        return codex_heartbeat_prompt(thread_id, agent, peer, goal, max_turns)
    if "claude" in lower:
        return claude_loop_prompt(thread_id, agent, peer, goal, max_turns)
    return loop_prompt_text(thread_id, agent, peer, goal, max_turns)


def cmd_kickoff(args: argparse.Namespace) -> None:
    from_agent = args.from_agent
    to_agent = args.to_agent
    goal = args.goal or args.topic
    body = read_body(args)
    with connect(args.db) as conn:
        thread_id = generate_thread_id(conn)
        created_at = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO threads(id, topic, created_at, created_by, max_turns, goal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, args.topic, created_at, from_agent, args.max_turns, goal),
        )
        add_participant(conn, thread_id, from_agent, "owner")
        add_participant(conn, thread_id, to_agent, "peer")
        seq = next_seq(conn, thread_id)
        conn.execute(
            "INSERT INTO messages(thread_id, seq, sender, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (thread_id, seq, from_agent, body, created_at),
        )
        set_cursor(conn, thread_id, from_agent, seq)
        conn.commit()

    prompts = {
        from_agent: runtime_loop_prompt(thread_id, from_agent, to_agent, goal, args.max_turns),
        to_agent: runtime_loop_prompt(thread_id, to_agent, from_agent, goal, args.max_turns),
    }
    data = {
        "thread_id": thread_id,
        "topic": args.topic,
        "created_at": created_at,
        "from": from_agent,
        "to": to_agent,
        "initial_message_seq": seq,
        "prompts": prompts,
    }
    text = f"""\
Thread: {thread_id}
Topic: {args.topic}

Initial message sent as {from_agent}.

Current runtime prompt ({from_agent}):
{prompts[from_agent]}

Peer runtime prompt ({to_agent}):
{prompts[to_agent]}
"""
    print_result(args, data, text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-msg",
        description="Local SQLite mailbox for messages between agent sessions.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("AGENT_MSG_DB", DEFAULT_DB)),
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_flags(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Print machine-readable JSON.",
        )

    p_new = sub.add_parser("new", help="Create a new message thread.")
    add_common_flags(p_new)
    p_new.add_argument("topic", help="Short thread topic.")
    p_new.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_new.add_argument("--goal", help="Goal for the conversation.")
    p_new.add_argument("--max-turns", type=int, help="Maximum substantive reply turns.")
    p_new.set_defaults(func=cmd_new)

    p_send = sub.add_parser("send", help="Send a message to a thread.")
    add_common_flags(p_send)
    p_send.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_send.add_argument("message", nargs="*", help="Message text. If omitted, stdin is read.")
    p_send.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_send.add_argument("--file", help="Read message body from a file.")
    p_send.set_defaults(func=cmd_send)

    p_read = sub.add_parser("read", help="Read unread messages from a thread.")
    add_common_flags(p_read)
    p_read.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_read.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_read.add_argument("--all", action="store_true", help="Read the full thread history.")
    p_read.add_argument("--peek", action="store_true", help="Do not advance the read cursor.")
    p_read.add_argument("--limit", type=int, help="Maximum messages to return.")
    p_read.set_defaults(func=cmd_read)

    p_wait = sub.add_parser("wait", help="Wait until unread messages arrive.")
    add_common_flags(p_wait)
    p_wait.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_wait.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_wait.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait.")
    p_wait.add_argument("--poll", type=float, default=2.0, help="Polling interval in seconds.")
    p_wait.add_argument("--peek", action="store_true", help="Do not advance the read cursor.")
    p_wait.add_argument("--limit", type=int, help="Maximum messages to return.")
    p_wait.set_defaults(func=cmd_wait)

    p_list = sub.add_parser("list", help="List threads and unread counts.")
    add_common_flags(p_list)
    p_list.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_list.add_argument("--closed", action="store_true", help="Include closed threads.")
    p_list.add_argument("--limit", type=int, default=50, help="Maximum threads to return.")
    p_list.set_defaults(func=cmd_list)

    p_close = sub.add_parser("close", help="Close a thread.")
    add_common_flags(p_close)
    p_close.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_close.set_defaults(func=cmd_close)

    p_done = sub.add_parser("done", help="Send a final DONE message and mark the thread done.")
    add_common_flags(p_done)
    p_done.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_done.add_argument("message", nargs="*", help="Final message. If omitted, stdin is read.")
    p_done.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_done.add_argument("--file", help="Read final message body from a file.")
    p_done.set_defaults(func=cmd_done)

    p_reopen = sub.add_parser("reopen", help="Reopen a done or closed thread.")
    add_common_flags(p_reopen)
    p_reopen.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_reopen.add_argument("--as", dest="as_agent", help="Agent identity for unread counts.")
    p_reopen.set_defaults(func=cmd_reopen)

    p_inbox = sub.add_parser("inbox", help="List active threads for natural-language routing.")
    add_common_flags(p_inbox)
    p_inbox.add_argument("--as", dest="as_agent", help="Agent identity for unread counts.")
    p_inbox.add_argument("--peer", help="Only include threads involving this peer agent.")
    p_inbox.add_argument("--closed", action="store_true", help="Include closed and done threads.")
    p_inbox.add_argument("--unread", action="store_true", help="Only include threads with unread messages.")
    p_inbox.add_argument("--limit", type=int, default=20, help="Maximum threads to return.")
    p_inbox.set_defaults(func=cmd_inbox)

    p_latest = sub.add_parser("latest", help="Return the latest matching thread.")
    add_common_flags(p_latest)
    p_latest.add_argument("--as", dest="as_agent", help="Agent identity for unread counts.")
    p_latest.add_argument("--peer", help="Only include threads involving this peer agent.")
    p_latest.add_argument("--closed", action="store_true", help="Include closed and done threads.")
    p_latest.set_defaults(func=cmd_latest)

    p_show = sub.add_parser("show", help="Show thread metadata, participants, and transcript.")
    add_common_flags(p_show)
    p_show.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_show.add_argument("--as", dest="as_agent", help="Agent identity for unread counts.")
    p_show.set_defaults(func=cmd_show)

    p_brief = sub.add_parser("brief", help="Show a compact state-of-play for a thread.")
    add_common_flags(p_brief)
    p_brief.add_argument("thread_id", nargs="?", help="Thread id. Defaults to latest matching thread.")
    p_brief.add_argument("--as", dest="as_agent", help="Agent identity for waiting-on status.")
    p_brief.add_argument("--peer", help="Resolve or label the brief with this peer agent.")
    p_brief.add_argument("--closed", action="store_true", help="Include closed and done threads when resolving.")
    p_brief.set_defaults(func=cmd_brief)

    p_doctor = sub.add_parser("doctor", help="Check database health and location.")
    add_common_flags(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_help_agent = sub.add_parser("help-agent", help="Print usage guidance for agents.")
    add_common_flags(p_help_agent)
    p_help_agent.set_defaults(func=cmd_help_agent)

    p_loop_prompt = sub.add_parser(
        "loop-prompt",
        help="Print a bounded recurring-check prompt for an agent runtime.",
    )
    add_common_flags(p_loop_prompt)
    p_loop_prompt.add_argument("thread_id", help="Thread id, e.g. thr_ab12cd.")
    p_loop_prompt.add_argument("--as", dest="as_agent", help="Agent identity for this command.")
    p_loop_prompt.add_argument("--peer", help="Peer agent name, e.g. codex or claude.")
    p_loop_prompt.add_argument("--goal", help="Short goal for the recurring conversation.")
    p_loop_prompt.add_argument(
        "--max-turns",
        type=int,
        default=4,
        help="Maximum substantive reply turns before asking the user.",
    )
    p_loop_prompt.set_defaults(func=cmd_loop_prompt)

    p_kickoff = sub.add_parser(
        "kickoff",
        help="Create a thread, send the first message, and print runtime loop prompts.",
    )
    add_common_flags(p_kickoff)
    p_kickoff.add_argument("topic", help="Short discussion topic.")
    p_kickoff.add_argument("message", nargs="*", help="Initial message. If omitted, stdin is read.")
    p_kickoff.add_argument("--from", dest="from_agent", required=True, help="Starting agent.")
    p_kickoff.add_argument("--to", dest="to_agent", required=True, help="Peer agent.")
    p_kickoff.add_argument("--file", help="Read initial message body from a file.")
    p_kickoff.add_argument("--goal", help="Goal for the discussion. Defaults to the topic.")
    p_kickoff.add_argument(
        "--max-turns",
        type=int,
        default=4,
        help="Maximum substantive reply turns before asking the user.",
    )
    p_kickoff.set_defaults(func=cmd_kickoff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except sqlite3.Error as exc:
        print(f"agent-msg: sqlite error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
