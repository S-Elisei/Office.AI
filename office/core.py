"""Domain operations: plain functions over the schema in schema.sql."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path

from office import db, git
from office.config import Config

log = logging.getLogger(__name__)

TASK_STATUSES = ("idea", "planned", "needs_clarification", "in_progress", "paused", "done")

DEFAULT_OWNER_NAME = "Owner"

#: The owner's standing brief to the director.
DIRECTOR_INSTRUCTIONS_SETTING = "director_instructions"

# The office's own name in the participant namespace.
OFFICE_SENDER = "office"

# The sender of a message a local service posts through the hub is this prefix
# plus the service's name.
HOOK_SENDER_PREFIX = "hook:"


# --------------------------------------------------------------------------- events


def _default_notifier(kind: str, payload: dict) -> None:
    """No-op until hub wires a real one."""


# A plain synchronous callable. Bridging to the event loop is hub's job when
# it installs the real notifier.
_notify = _default_notifier


def set_notifier(fn) -> None:
    """Call once, at hub startup, with a sync callable(kind, payload).

    Passing None restores the no-op.
    """
    global _notify
    _notify = fn or _default_notifier


def _emit(
    conn,
    kind: str,
    subject_type: str,
    subject_id=None,
    payload: dict | None = None,
    actor: str | None = None,
) -> None:
    """Record one delta and push it live.

    Call this inside the same db.transaction() as the mutation it describes. The
    `_notify` call happens after the INSERT succeeds and before the COMMIT.

    `actor` is the participant who caused this. Leave it None only when nothing
    participant-shaped caused the write.
    """
    db.execute(
        conn,
        "INSERT INTO events (kind, subject_type, subject_id, payload, actor) VALUES (?, ?, ?, ?, ?)",
        (kind, subject_type, subject_id, json.dumps(payload) if payload is not None else None, actor),
    )
    _prune_events(conn)
    _notify(kind, {"subject_type": subject_type, "subject_id": subject_id, **(payload or {})})


# How many of the most recent event rows to keep.
EVENT_RETENTION = 1000


def _prune_events(conn) -> None:
    """Keep the newest EVENT_RETENTION rows and delete the rest.

    Runs inside every _emit()'s own transaction: core owns the only writer into
    `events`, and nothing reads the table back.
    """
    db.execute(
        conn,
        "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
        (EVENT_RETENTION,),
    )


def _row(conn, table: str, id_col: str, id_value) -> dict | None:
    row = db.query_one(conn, f"SELECT * FROM {table} WHERE {id_col} = ?", (id_value,))
    return dict(row) if row is not None else None


def _agent_name(conn, agent_id: int) -> str:
    return db.query_one(conn, "SELECT name FROM agents WHERE id = ?", (agent_id,))["name"]


# --------------------------------------------------------------------------- settings


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = db.query_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row is not None else default


def set_setting(conn, key: str, value: str, actor: str | None = None) -> None:
    previous = get_setting(conn, key, None)
    with db.transaction(conn):
        db.execute(
            conn,
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        _emit(conn, "settings", "setting", None, {"key": key}, actor=actor)
    # The standing brief is printed into the director's system prompt, which is
    # fixed when its session is created. The new text is sent, whole, to whoever
    # is working under it.
    if key == DIRECTOR_INSTRUCTIONS_SETTING and value != previous:
        # To the director and nobody else.
        try:
            row = db.query_one(conn, "SELECT name FROM agents WHERE kind = 'director'")
            if row is not None and row["name"] != actor:
                tell_quietly(
                    conn, row["name"],
                    "[office] Your standing instructions have been rewritten. This replaces what "
                    "stands under Instructions in your system prompt — work from this text from "
                    "now on." + chr(10) + chr(10) + value,
                )
        except Exception:
            log.exception("could not tell the director its instructions changed")


def get_owner_name(conn) -> str:
    """The human owner's name in the shared participant namespace."""
    return get_setting(conn, "owner_name", DEFAULT_OWNER_NAME)


# --------------------------------------------------------------------------- messages
#
# The only write path into `messages`. Who gets woken, when, and how a burst
# coalesces into one turn is the bus's job, reading this same table. This
# module never imports office.bus.


def send_message(conn, sender: str, recipient: str, body: str, actor: str | None = None) -> dict:
    """say(to, text).

    `recipient` is a participant name, or 'all' for the common chat; the channel
    is derived from it here. The recipient is validated against the participant
    namespace before anything is written.
    """
    channel = "all" if recipient == "all" else "dm"
    recipient_col = None if channel == "all" else recipient
    if channel == "dm":
        _check_participant(conn, recipient_col)
    with db.transaction(conn):
        message_id = _write_message(
            conn, channel, sender, recipient_col, body, actor if actor is not None else sender
        )
    return _row(conn, "messages", "id", message_id)


def _write_message(conn, channel: str, sender: str, recipient: str | None, body: str,
                   actor: str) -> int:
    """Insert one message and emit it. Call inside db.transaction()."""
    cur = db.execute(
        conn,
        "INSERT INTO messages (channel, sender, recipient, body) VALUES (?, ?, ?, ?)",
        (channel, sender, recipient, body),
    )
    _emit(
        conn, "messages", "message", cur.lastrowid,
        {"channel": channel, "sender": sender, "recipient": recipient},
        actor=actor,
    )
    return cur.lastrowid


def _check_participant(conn, recipient: str) -> None:
    """Refuse a direct-message recipient that is not the owner or an agent."""
    if recipient.startswith(HOOK_SENDER_PREFIX):
        raise ValueError(
            f"'{recipient}' is a service, not a participant, and no message reaches it — "
            "answer it through its own API"
        )
    known = recipient == get_owner_name(conn) or db.query_one(
        conn, "SELECT 1 FROM agents WHERE name = ?", (recipient,)
    )
    if not known:
        raise ValueError(f"no participant named '{recipient}' — check the name and try again")


CHAT_PAGE_SIZE = 30


def chat_history(conn, before_id: int | None = None, limit: int = CHAT_PAGE_SIZE) -> tuple[list[dict], bool]:
    """One page of the COMMON chat, oldest-first for reading, and whether anything
    older exists.

    Newest page by default; with `before_id`, the page immediately older than
    that id. Common chat only.

    Taken by id, not created_at. One row more than the page is fetched and
    thrown away, which is how "is there anything older" is answered.
    """
    # `recipient IS NULL` is what keeps the room the room: a common-channel row
    # WITH a recipient is one person's quiet line (tell_quietly).
    where = "channel = 'all' AND recipient IS NULL"
    params: list = []
    if before_id is not None:
        where += " AND id < ?"
        params.append(before_id)
    params.append(limit + 1)
    rows = db.query(
        conn,
        f"SELECT * FROM (SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        tuple(params),
    )
    has_older = len(rows) > limit
    return [dict(r) for r in (rows[1:] if has_older else rows)], has_older


def see_conversation(conn, *, channel: str, peer: str, up_to_message_id: int) -> None:
    """Move the owner's reading mark for one conversation up to a message he has
    actually seen.

    `channel`/`peer` address a row of `owner_reading`: 'dm' with the other
    participant's name, or 'all' with an empty peer for the common chat.

    Written only by the route that takes the browser's report
    (office/web/routes/reading.py). Nothing reached by a GET calls this.

    The mark only ever moves forward.
    """
    db.execute(
        conn,
        "INSERT INTO owner_reading (channel, peer, seen_message_id) VALUES (?, ?, ?) "
        "ON CONFLICT(channel, peer) DO UPDATE SET seen_message_id = excluded.seen_message_id "
        "WHERE excluded.seen_message_id > owner_reading.seen_message_id",
        (channel, peer, up_to_message_id),
    )


# --------------------------------------------------------------------------- deferred messages
#
# The office's alarm. The `remind` tool sets one, office/bus.py's tick takes
# what is due and sends it through send_message above, and from there it is
# an ordinary message.
#
# The only write path into `scheduled_messages`.

#: The furthest ahead a wake may be set.
MAX_WAKE_SECONDS = 7 * 24 * 60 * 60


def schedule_message(conn, sender: str, recipient: str, body: str, in_seconds: int) -> dict:
    """remind(to, text, in_seconds) - one message, written now and sent later,
    waking whoever it is addressed to when it arrives.

    The recipient is validated here as send_message validates its own, and again
    when the row fires. `all` is refused outright.

    `due_at` is computed here, by SQLite's own clock and in SQLite's own format.
    """
    in_seconds = _seconds_ahead("in_seconds", in_seconds)
    if recipient == "all":
        raise ValueError(
            "'all' is the common chat and it wakes nobody, so a deferred post to it would arrive "
            "and do nothing — name the participant who should be woken"
        )
    _check_participant(conn, recipient)
    with db.transaction(conn):
        cur = db.execute(
            conn,
            "INSERT INTO scheduled_messages (due_at, sender, recipient, body) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), ?, ?, ?)",
            (f"+{in_seconds} seconds", sender, recipient, body),
        )
        wake_id = cur.lastrowid
        _emit(
            conn, "messages", "scheduled_message", wake_id,
            {"sender": sender, "recipient": recipient},
            actor=sender,
        )
    return _row(conn, "scheduled_messages", "id", wake_id)


def _seconds_ahead(arg: str, value) -> int:
    """`value` as a whole number of seconds from 0 to MAX_WAKE_SECONDS. Raises
    ValueError naming `arg` otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError(f"{arg} must be a whole number of seconds, not '{value}'")
    value = int(value)
    if value < 0:
        raise ValueError(f"{arg} cannot be negative — it counts forward from now")
    if value > MAX_WAKE_SECONDS:
        raise ValueError(
            f"{arg} is at most {MAX_WAKE_SECONDS} (seven days); {value} is further off than "
            "the office will hold it"
        )
    return value


def due_scheduled_messages(conn) -> list[dict]:
    """Every wake whose moment has passed, soonest first.

    "Has passed", never "passed since the last look": one that came due while
    the hub was down is returned by the first call after it starts.
    """
    return [
        dict(r)
        for r in db.query(
            conn,
            "SELECT * FROM scheduled_messages WHERE due_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "ORDER BY due_at, id",
        )
    ]


def drop_scheduled_message(conn, wake_id: int, actor: str | None = None) -> None:
    """Forget one wake.

    The bus calls this the moment it has sent one; the owner's works page calls
    it to cancel one that has not fired.
    """
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM scheduled_messages WHERE id = ?", (wake_id,))
        _emit(conn, "messages", "scheduled_message", wake_id, {"deleted": True}, actor=actor)


def scheduled_messages(conn, sender: str | None = None) -> list[dict]:
    """Wakes that have not fired, soonest first: all of them, or one sender's."""
    if sender is None:
        rows = db.query(conn, "SELECT * FROM scheduled_messages ORDER BY due_at, id")
    else:
        rows = db.query(
            conn, "SELECT * FROM scheduled_messages WHERE sender = ? ORDER BY due_at, id", (sender,)
        )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- expectations
#
# An answer an agent waits for from a local service. The `expect` tool opens
# one, office/web/routes/hooks.py takes the answer, and office/bus.py's tick
# closes one whose due time has passed.
#
# The only write path into `expectations`.

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def open_expectation(conn, agent: str, about: str, within_seconds) -> dict:
    """expect(about, within_seconds): an open expectation for `agent`, due
    `within_seconds` from now.

    Returns the row. Its `token` is the last segment of the address the answer
    is posted to.
    """
    if not isinstance(about, str) or not about.strip():
        raise ValueError("about must name what you are waiting for — the job or the batch")
    within_seconds = _seconds_ahead("within_seconds", within_seconds)
    with db.transaction(conn):
        agent_id = db.query_one(conn, "SELECT id FROM agents WHERE name = ?", (agent,))["id"]
        cur = db.execute(
            conn,
            "INSERT INTO expectations (token, agent_id, about, due_at) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))",
            (secrets.token_urlsafe(24), agent_id, about.strip(), f"+{within_seconds} seconds"),
        )
        expectation_id = cur.lastrowid
        _emit(conn, "agents", "expectation", expectation_id, {"agent": agent, "state": "open"},
              actor=agent)
    return _row(conn, "expectations", "id", expectation_id)


def answer_expectation(conn, token: str, service: str, text: str) -> dict | None:
    """Take a service's answer at `token`: close the expectation and send `text`
    to its agent as a direct message from `hook:<service>`, headed with the
    expectation's `about`. One transaction.

    Returns None, writing nothing, when no expectation is open at `token`.
    """
    sender = HOOK_SENDER_PREFIX + service
    with db.transaction(conn):
        row = db.query_one(
            conn,
            "SELECT e.id, e.about, a.name FROM expectations e JOIN agents a ON a.id = e.agent_id "
            f"WHERE e.token = ? AND e.closed_at IS NULL AND e.due_at > {_NOW}",
            (token,),
        )
        if row is None:
            return None
        db.execute(conn, f"UPDATE expectations SET closed_at = {_NOW} WHERE id = ?", (row["id"],))
        _emit(conn, "agents", "expectation", row["id"], {"agent": row["name"], "state": "answered"},
              actor=sender)
        message_id = _write_message(
            conn, "dm", sender, row["name"], f"[about: {row['about']}]\n{text}", sender
        )
    return _row(conn, "messages", "id", message_id)


def due_expectations(conn) -> list[dict]:
    """Every open expectation whose due time has passed, soonest first, with its
    agent's name."""
    return [
        dict(r)
        for r in db.query(
            conn,
            "SELECT e.id, e.about, a.name AS agent FROM expectations e "
            "JOIN agents a ON a.id = e.agent_id "
            f"WHERE e.closed_at IS NULL AND e.due_at <= {_NOW} ORDER BY e.due_at, e.id",
        )
    ]


def expire_expectation(conn, expectation_id: int, notice: str) -> None:
    """Close one expectation whose due time has passed and send `notice` to its
    agent as a direct message from `office`. One transaction.

    Writes nothing when the row is gone: its agent was fired.
    """
    with db.transaction(conn):
        row = db.query_one(
            conn,
            "SELECT a.name FROM expectations e JOIN agents a ON a.id = e.agent_id "
            "WHERE e.id = ? AND e.closed_at IS NULL",
            (expectation_id,),
        )
        if row is None:
            return
        db.execute(conn, f"UPDATE expectations SET closed_at = {_NOW} WHERE id = ?",
                   (expectation_id,))
        _emit(conn, "agents", "expectation", expectation_id,
              {"agent": row["name"], "state": "expired"}, actor=OFFICE_SENDER)
        _write_message(conn, "dm", OFFICE_SENDER, row["name"], notice, OFFICE_SENDER)


def drop_closed_expectations(conn, agent: str) -> None:
    """Delete `agent`'s closed expectations."""
    db.execute(
        conn,
        "DELETE FROM expectations WHERE closed_at IS NOT NULL "
        "AND agent_id = (SELECT id FROM agents WHERE name = ?)",
        (agent,),
    )


def open_expectations(conn) -> list[dict]:
    """Every open expectation, oldest first."""
    return [
        dict(r)
        for r in db.query(
            conn,
            "SELECT a.name AS agent, e.about, e.opened_at, e.due_at FROM expectations e "
            "JOIN agents a ON a.id = e.agent_id WHERE e.closed_at IS NULL ORDER BY e.id",
        )
    ]


# --------------------------------------------------------------------------- notices
#
# The owner's journal of system notices, and the only source of the plate
# under the director chat. The only write path into `notices`.

def record_notice(conn, severity: str, text: str, actor: str | None = None) -> dict:
    """One line in the owner's journal.

    `severity` decides whether it also reaches him as a plate, and the choice
    belongs to the caller:

      'critical' - the owner's own counterpart cannot answer him right now and
                   no page would tell him so.
      'info'     - everything else worth a line in his log.

    A thing that needs no attention at all is not written here by anybody.
    """
    with db.transaction(conn):
        cur = db.execute(
            conn, "INSERT INTO notices (severity, text) VALUES (?, ?)", (severity, text)
        )
        notice_id = cur.lastrowid
        _emit(conn, "notices", "notice", notice_id, {"severity": severity}, actor=actor)
    return _row(conn, "notices", "id", notice_id)


#: How much of the journal the notices page shows. There is no cursor back
#: past it.
NOTICE_PAGE_SIZE = 200


def list_notices(conn) -> list[dict]:
    """The journal, newest first. By id, not created_at."""
    return [
        dict(row)
        for row in db.query(
            conn, "SELECT * FROM notices ORDER BY id DESC LIMIT ?", (NOTICE_PAGE_SIZE,)
        )
    ]


#: How many critical notices the plate carries at once.
PLATE_SIZE = 3


def plate_notices(conn) -> tuple[list[dict], int]:
    """What the plate shows: critical notices the owner has not dismissed, newest
    first, and how many more there are behind the ones returned.
    """
    rows = db.query(
        conn,
        "SELECT * FROM notices WHERE severity = 'critical' AND seen_at IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (PLATE_SIZE,),
    )
    total = db.query_one(
        conn,
        "SELECT COUNT(*) AS n FROM notices WHERE severity = 'critical' AND seen_at IS NULL",
    )["n"]
    return [dict(row) for row in rows], total - len(rows)


def dismiss_notices(conn, actor: str | None = None) -> int:
    """The owner says he has seen the plate. Stamps every critical notice that is
    still unseen and returns how many.

    The only writer of `seen_at`, and the only thing that makes the plate go
    away. The journal keeps every one of these rows and ignores the column.
    """
    with db.transaction(conn):
        cur = db.execute(
            conn,
            "UPDATE notices SET seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE severity = 'critical' AND seen_at IS NULL",
        )
        count = cur.rowcount
        if count:
            _emit(conn, "notices", "notice", None, {"dismissed": count}, actor=actor)
    return count


# --------------------------------------------------------------------------- hire profiles


def save_profile(conn, name: str, *, runtime: str, model: str, effort: str | None) -> dict:
    """A reusable hiring composition: runtime, model and effort - exactly the three
    things hire() takes.

    No event: no page in the interface lists profiles, and an agent is not shown
    events.
    """
    with db.transaction(conn):
        db.execute(
            conn,
            """
            INSERT INTO profiles (name, runtime, model, effort) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                runtime = excluded.runtime, model = excluded.model, effort = excluded.effort
            """,
            (name, runtime, model, effort),
        )
    return get_profile(conn, name)


def get_profile(conn, name: str) -> dict | None:
    return _row(conn, "profiles", "name", name)


def list_profiles(conn) -> list[dict]:
    return [dict(r) for r in db.query(conn, "SELECT * FROM profiles ORDER BY name COLLATE NOCASE")]


# --------------------------------------------------------------------------- agents & workspaces


def hire(
    conn,
    config: Config,
    *,
    name: str,
    runtime: str,
    model: str,
    effort: str | None,
    kind: str = "executor",
    adopt_workspace_id: str | None = None,
    actor: str | None = None,
) -> dict:
    """Create the agent row (provisioning), obtain a workspace, flip to ready.

    Blocking: call this off the event loop, never from an async request handler.

    The hire composition is exactly {runtime, model, effort}; there is no toolset
    and no MCP server to choose. `adopt_workspace_id` takes over an existing
    workspace instead of cloning a new one, and leaves it standing where the
    previous owner left it, branch and uncommitted changes alike.

    A fresh workspace is handed out on the source repository's own default
    branch, and nothing here moves it.

    Two names are refused outright: the owner's, and `office`.
    """
    _check_model_choice(conn, runtime, model, effort)
    owner_name = get_owner_name(conn)
    if name == owner_name:
        raise ValueError(
            f"'{name}' is the owner's own name — an agent cannot be hired under it "
            "(it would inherit the right to resolve tickets addressed to him)"
        )
    if name == OFFICE_SENDER:
        raise ValueError(
            f"'{name}' is the office's own name — an agent cannot be hired under it "
            "(it would be able to sign messages as the office)"
        )
    if ":" in name:
        raise ValueError(
            f"'{name}' cannot contain a colon — a name is also a URL segment, a log field "
            "and part of the mark the office tracks that agent's processes by, where a "
            "colon separates one agent from another. Pick a name without one."
        )
    existing = db.query_one(conn, "SELECT id FROM agents WHERE name = ?", (name,))
    if existing is not None:
        raise ValueError(f"an agent named '{name}' already exists")

    with db.transaction(conn):
        cur = db.execute(
            conn,
            """
            INSERT INTO agents (name, kind, runtime, model, effort, status,
                                last_seen_message_id)
            VALUES (?, ?, ?, ?, ?, 'provisioning',
                    (SELECT COALESCE(MAX(id), 0) FROM messages))
            """,
            (name, kind, runtime, model, effort),
        )
        agent_id = cur.lastrowid
        # The watermark starts at "everything so far", not at NULL: a brand-new
        # agent's first session gets the full snapshot, not a replay.
        _emit(conn, "agents", "agent", agent_id, {"name": name, "status": "provisioning"}, actor=actor)

    # Filled the moment the clone's intake is taken, which is before anything in
    # the clone can fail.
    intake = None

    def _took(taken: git.Intake) -> None:
        nonlocal intake
        intake = taken

    try:
        if adopt_workspace_id is not None:
            ws_row = db.query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (adopt_workspace_id,))
            if ws_row is None:
                raise ValueError(f"no such workspace '{adopt_workspace_id}'")
            git.transfer_workspace(ws_row["path"], name)
            workspace_id, path = ws_row["id"], ws_row["path"]
        else:
            ws = git.create_workspace(config, name, on_intake=_took)
            workspace_id, path = ws.id, ws.path
    except Exception:
        # Never leave a half-hired agent behind: nothing usable was created.
        with db.transaction(conn):
            db.execute(conn, "DELETE FROM agents WHERE id = ?", (agent_id,))
            _emit(conn, "agents", "agent", agent_id, {"name": name, "status": "hire_failed"}, actor=actor)
        _announce_intake_quietly(conn, intake, f"the hire of {name}")
        raise
    # Outside the block above: the clone stands, and a message that will not go
    # out must not take it down with it.
    announce_intake(conn, intake)

    with db.transaction(conn):
        if adopt_workspace_id is not None:
            db.execute(
                conn, "UPDATE workspaces SET owner_agent_id = ? WHERE id = ?",
                (agent_id, workspace_id),
            )
        else:
            db.execute(
                conn,
                "INSERT INTO workspaces (id, path, owner_agent_id) VALUES (?, ?, ?)",
                (workspace_id, path, agent_id),
            )
        db.execute(conn, "UPDATE agents SET status = 'ready' WHERE id = ?", (agent_id,))
        _emit(conn, "agents", "agent", agent_id, {"name": name, "status": "ready"}, actor=actor)

    _resolve_pending_work(conn, agent_id)
    return _row(conn, "agents", "id", agent_id)


def fire(conn, agent_id: int, actor: str | None = None) -> None:
    """Remove an agent.

    Refuses while it still has an active work; works.agent_id cascades on agent
    deletion.

    The workspace is never deleted: workspaces.owner_agent_id -> NULL, so it
    sits free for the next hire, standing on whatever branch its last owner left
    it on. Nothing git-side happens here, which is why this takes no Config.
    """
    agent = _row(conn, "agents", "id", agent_id)
    # 'done' is here because a work that has reported and has not been closed is
    # an unanswered result; agents.id is an ON DELETE CASCADE parent of works.
    active = db.query(
        conn,
        "SELECT id FROM works WHERE agent_id = ? AND status IN ('running', 'paused', 'done')",
        (agent_id,),
    )
    if active:
        raise ValueError(
            f"agent '{agent['name']}' has {len(active)} active work(s) — reassign, close or "
            "finish them before firing"
        )
    # `prs.author_agent_id` is NOT NULL and carries no ON DELETE clause, so
    # deleting an agent that opened a PR still standing fails the foreign key.
    # Asked here instead, it is a sentence naming the PR. No status filter: a row
    # that still exists is a pull request that still stands.
    authored = db.query(
        conn, "SELECT id, title FROM prs WHERE author_agent_id = ? ORDER BY id", (agent_id,)
    )
    if authored:
        named = ", ".join(f"#{p['id']} '{p['title']}'" for p in authored)
        raise ValueError(
            f"agent '{agent['name']}' still has {named} standing — merge or close it before "
            "firing them; a pull request keeps the name of whoever wrote it"
        )
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM agents WHERE id = ?", (agent_id,))
        _emit(conn, "agents", "agent", agent_id, {"name": agent["name"], "status": "fired"}, actor=actor)


def set_agent_status(conn, agent_id: int, status: str, actor: str | None = None) -> None:
    with db.transaction(conn):
        db.execute(conn, "UPDATE agents SET status = ? WHERE id = ?", (status, agent_id))
        _emit(conn, "agents", "agent", agent_id, {"status": status}, actor=actor)


def update_agent_model(
    conn,
    agent_id: int,
    *,
    runtime: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Change a live agent's runtime/model/effort.

    hire() sets these once; this is the only other way they change. Each is
    independently optional - pass only what changed, and None means "leave it".

    The cost of the change is classified here and returned with the result;
    `dry_run` asks for the classification without writing anything.
    """
    agent = _row(conn, "agents", "id", agent_id)
    if agent is None:
        raise ValueError(f"no such agent {agent_id}")

    new_runtime = runtime if runtime is not None else agent["runtime"]
    new_model = model if model is not None else agent["model"]
    new_effort = effort if effort is not None else agent["effort"]

    runtime_changed = new_runtime != agent["runtime"]
    model_changed = new_model != agent["model"] or new_effort != agent["effort"]

    if runtime_changed:
        cost = "fresh_session"
        note = (
            f"runtime change ({agent['runtime']} -> {new_runtime}): the session cannot continue — "
            "context is lost. The next turn starts a fresh session from the full state snapshot."
        )
    elif model_changed:
        cost = "full_reread"
        note = (
            f"model change ({agent['model']} -> {new_model}): the session continues and context "
            "survives, but the prompt cache is tied to the model — the next turn pays a full re-read."
        )
    else:
        cost = "none"
        note = "no change"

    # Validated on the RESULTING triple, not on the arguments: each of the three
    # is independently optional. Skipped when nothing changed; a call that DOES
    # change something is refused. Checked before the dry_run return as well.
    if cost != "none":
        _check_model_choice(conn, new_runtime, new_model, new_effort)

    if dry_run:
        return {"agent": agent, "cost": cost, "note": note}

    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE agents SET runtime = ?, model = ?, effort = ? WHERE id = ?",
            (new_runtime, new_model, new_effort, agent_id),
        )
        if runtime_changed:
            # Not portable across vendors. In the same transaction as the runtime column.
            db.execute(conn, "UPDATE agents SET session_id = NULL WHERE id = ?", (agent_id,))
        _emit(
            conn, "agents", "agent", agent_id,
            {
                "name": agent["name"],
                "runtime": new_runtime, "model": new_model, "effort": new_effort,
                "runtime_changed": runtime_changed, "model_changed": model_changed,
                "cost": cost,
            },
            actor=actor,
        )
    return {"agent": _row(conn, "agents", "id", agent_id), "cost": cost, "note": note}


def start_new_session(conn, agent_id: int) -> dict:
    """agent(op=new_session). Drop the conversation, keep everything physical.

    Nulling session_id IS the operation. The workspace, its branch and its
    working tree are untouched, and the next turn finds nothing to resume, so it
    starts a fresh session and the bus hands it the full state snapshot.
    """
    agent = _row(conn, "agents", "id", agent_id)
    if agent is None:
        raise ValueError(f"no such agent {agent_id}")
    with db.transaction(conn):
        db.execute(conn, "UPDATE agents SET session_id = NULL WHERE id = ?", (agent_id,))
    return _row(conn, "agents", "id", agent_id)



def record_quota(conn, snapshots, actor: str | None = None) -> int:
    """Store a poll's worth of quota buckets, and tell the page about it.

    One row per (runtime, label), shared by every agent of that runtime.

    Emits only when a figure actually moved. Returns how many rows changed, a
    deletion counted as a change like any other.

    A successful poll also deletes that runtime's buckets the answer does not
    mention.
    """
    changed = 0
    # Which labels each polled runtime reported this round, keyed by runtime:
    # nothing here may touch a runtime this answer says nothing about.
    live_labels: dict[str, set[str]] = {}
    with db.transaction(conn):
        for snap in snapshots:
            live_labels.setdefault(snap.runtime, set()).add(snap.label)
            reset = str(snap.reset_time) if snap.reset_time else None
            current = db.query_one(
                conn,
                "SELECT remaining_fraction, reset_time FROM quota WHERE runtime = ? AND label = ?",
                (snap.runtime, snap.label),
            )
            if (
                current is not None
                and current["remaining_fraction"] == snap.remaining_fraction
                and current["reset_time"] == reset
            ):
                continue
            if current is None:
                db.execute(
                    conn,
                    "INSERT INTO quota (runtime, label, remaining_fraction, reset_time) "
                    "VALUES (?, ?, ?, ?)",
                    (snap.runtime, snap.label, snap.remaining_fraction, reset),
                )
            else:
                db.execute(
                    conn,
                    "UPDATE quota SET remaining_fraction = ?, reset_time = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE runtime = ? AND label = ?",
                    (snap.remaining_fraction, reset, snap.runtime, snap.label),
                )
            changed += 1
        for runtime, labels in live_labels.items():
            placeholders = ", ".join("?" for _ in labels)
            cur = db.execute(
                conn,
                f"DELETE FROM quota WHERE runtime = ? AND label NOT IN ({placeholders})",
                (runtime, *labels),
            )
            changed += cur.rowcount
        if changed:
            _emit(conn, "quota", "quota", None, {"runtime": snapshots[0].runtime}, actor=actor)
    return changed


def record_models(conn, runtime: str, models) -> int:
    """Store one runtime's catalogue, replacing whatever it had. Returns changes.

    The runtime is an argument here rather than read off the rows: a catalogue
    can legitimately come back empty, and an empty one is stored as empty.
    """
    incoming = {m.model_id: m for m in models}
    changed = 0
    with db.transaction(conn):
        existing = {
            r["model_id"]: r
            for r in db.query(conn, "SELECT * FROM models WHERE runtime = ?", (runtime,))
        }
        for model_id, info in incoming.items():
            # NULL and "" are different answers - the vendor said nothing versus the
            # vendor listed no levels - so an empty tuple becomes NULL.
            efforts = ",".join(info.efforts) if info.efforts else None
            current = existing.get(model_id)
            if (
                current is not None
                and current["display_name"] == info.display_name
                and current["efforts"] == efforts
            ):
                continue
            if current is None:
                db.execute(
                    conn,
                    "INSERT INTO models (runtime, model_id, display_name, efforts) VALUES (?, ?, ?, ?)",
                    (runtime, model_id, info.display_name, efforts),
                )
            else:
                db.execute(
                    conn,
                    "UPDATE models SET display_name = ?, efforts = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE runtime = ? AND model_id = ?",
                    (info.display_name, efforts, runtime, model_id),
                )
            changed += 1
        gone = [model_id for model_id in existing if model_id not in incoming]
        if gone:
            placeholders = ", ".join("?" for _ in gone)
            cur = db.execute(
                conn,
                f"DELETE FROM models WHERE runtime = ? AND model_id IN ({placeholders})",
                (runtime, *gone),
            )
            changed += cur.rowcount
    return changed


def list_models(conn, runtime: str | None = None) -> list[dict]:
    """The stored catalogue, ordered by runtime then model id.

    `efforts` comes back as a list of strings, empty when the vendor did not
    say: no effort to validate, NOT "no effort allowed" (see
    _check_model_choice).
    """
    if runtime is None:
        rows = db.query(conn, "SELECT * FROM models ORDER BY runtime, model_id")
    else:
        rows = db.query(conn, "SELECT * FROM models WHERE runtime = ? ORDER BY model_id", (runtime,))
    out = []
    for row in rows:
        item = dict(row)
        item["efforts"] = [e for e in (row["efforts"] or "").split(",") if e]
        out.append(item)
    return out


def models_catalogue(conn, runtime: str | None = None) -> str:
    """The catalogue as text, for a human-or-model reader.

    One renderer for every place the catalogue is printed, so no two of them can
    drift apart. Models are grouped by the effort list they share.
    """
    rows = list_models(conn, runtime)
    if not rows:
        return (
            f"(no model catalogue for {runtime})" if runtime
            else "(no model catalogue)"
        )
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for row in rows:
        groups.setdefault((row["runtime"], tuple(row["efforts"])), []).append(row["model_id"])
    lines = []
    for (rt, efforts), ids in groups.items():
        effort_text = ", ".join(efforts) if efforts else "not published by the vendor"
        lines.append(f"- {rt} — effort: {effort_text} — {', '.join(ids)}")
    return "\n".join(lines)


# A string starting with `claude-` is accepted without being checked;
# everything else on this runtime must be an alias in the catalogue.
_CLAUDE_FULL_ID = re.compile(r"^claude-\S+$")


def _check_model_choice(conn, runtime: str, model: str, effort: str | None) -> None:
    """Refuse a model this runtime's catalogue does not have. Raises ValueError.

    The refusal text contains the catalogue.

    Two holes: with no catalogue for a runtime nothing on it is checked, and a
    full claude model id passes unchecked.
    """
    catalogue = list_models(conn, runtime)
    if not catalogue:
        return  # hole 1
    if runtime == "claude" and _CLAUDE_FULL_ID.match(model or ""):
        return  # hole 2
    row = next((r for r in catalogue if r["model_id"] == model), None)
    if row is None:
        extra = (
            " A full model id (claude-…) is also accepted on claude and cannot be checked here."
            if runtime == "claude" else ""
        )
        raise ValueError(
            f"'{model}' is not a model {runtime} offers.{extra} What it does offer:\n"
            f"{models_catalogue(conn, runtime)}"
        )
    if effort and row["efforts"] and effort not in row["efforts"]:
        raise ValueError(
            f"'{effort}' is not an effort level {runtime}/{model} takes — "
            f"it takes: {', '.join(row['efforts'])}"
        )


def quota_buckets(conn) -> list[dict]:
    """Every stored bucket, as it was last read, ordered by runtime then label.

    No derived figure rides along.
    """
    return [dict(r) for r in db.query(conn, "SELECT * FROM quota ORDER BY runtime, label")]


def stray_workspace_dirs(conn, ws_dir) -> list[str]:
    """Directory names under ws/ that no `workspaces` row accounts for.

    Compared by NAME, never by resolved path.
    """
    try:
        known = {Path(row["path"]).name for row in db.query(conn, "SELECT path FROM workspaces")}
        with os.scandir(ws_dir) as entries:
            return sorted(e.name for e in entries if e.name not in known and e.is_dir())
    except (OSError, sqlite3.Error):
        log.exception("could not list %s for stray directories", ws_dir)
        return []


def ownerless_workspaces(conn) -> list[dict]:
    """The workspaces nobody owns: a tree the office made and still keeps.

    Firing an agent nulls owner_agent_id and leaves both the row and the
    directory, so a successor can take the tree over as it stands -
    hire(adopt_workspace_id=...), which is the one route to it.
    """
    return [
        dict(r)
        for r in db.query(
            conn, "SELECT * FROM workspaces WHERE owner_agent_id IS NULL ORDER BY id"
        )
    ]


def roster(conn) -> dict:
    """roster(): who exists, who's busy, quota, context fill.

    `works` carries every work that is still on the books - a row exists exactly
    as long as the work is open, so there is no status filter here at all - with
    the reason each one stopped and, for a pause, the earliest it could resume.
    """
    agents = [
        dict(r)
        for r in db.query(
            conn,
            "SELECT id, name, kind, runtime, model, effort, status, context_used, context_limit "
            "FROM agents ORDER BY (kind = 'director') DESC, name COLLATE NOCASE",
        )
    ]
    quota = quota_buckets(conn)
    works = [
        dict(r)
        for r in db.query(
            conn,
            "SELECT works.id, works.agent_id, agents.name AS agent_name, works.brief, works.branch, "
            "works.status, works.fail_reason, works.pause_reason, works.resume_after "
            "FROM works JOIN agents ON agents.id = works.agent_id ORDER BY works.id",
        )
    ]
    # A task marked 'planned'/'in_progress' whose blocker isn't 'done' looks
    # ready but isn't.
    blocked_tasks = [
        dict(r)
        for r in db.query(
            conn,
            "SELECT DISTINCT t.id, t.title, t.status, blocker.id AS blocking_task_id, blocker.title AS blocking_title "
            "FROM tasks t "
            "JOIN task_dependencies td ON td.blocked_task_id = t.id "
            "JOIN tasks blocker ON blocker.id = td.blocking_task_id "
            "WHERE t.status IN ('planned', 'in_progress') AND blocker.status != 'done'",
        )
    ]
    return {"agents": agents, "quota": quota, "works": works, "blocked_tasks": blocked_tasks}


# --------------------------------------------------------------------------- tasks


def create_task(conn, title: str, body: str | None = None, status: str = "idea", actor: str | None = None) -> dict:
    if status not in TASK_STATUSES:
        raise ValueError(f"bad task status '{status}'")
    with db.transaction(conn):
        row = db.query_one(conn, "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM tasks WHERE status = ?", (status,))
        cur = db.execute(
            conn,
            "INSERT INTO tasks (title, body, status, position) VALUES (?, ?, ?, ?)",
            (title, body, status, row["p"]),
        )
        task_id = cur.lastrowid
        _emit(conn, "tasks", "task", task_id, {"title": title, "status": status}, actor=actor)
    return _row(conn, "tasks", "id", task_id)


def update_task(
    conn, task_id: int, *, title: str | None = None, body: str | None = None, actor: str | None = None
) -> dict:
    fields, params = [], []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if body is not None:
        fields.append("body = ?")
        params.append(body)
    if not fields:
        return _row(conn, "tasks", "id", task_id)
    fields.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    with db.transaction(conn):
        db.execute(conn, f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", (*params, task_id))
        _emit(conn, "tasks", "task", task_id, {"title": title} if title is not None else {}, actor=actor)
    return _row(conn, "tasks", "id", task_id)


def move_task(conn, task_id: int, status: str, position: int | None = None, actor: str | None = None) -> dict:
    """Move a task to one of the six kanban columns.

    For a terminal move to 'done' with a result line, use close_task(); this one
    only ever changes status/position.
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"bad task status '{status}'")
    with db.transaction(conn):
        if position is None:
            row = db.query_one(conn, "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM tasks WHERE status = ?", (status,))
            position = row["p"]
        db.execute(
            conn,
            "UPDATE tasks SET status = ?, position = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (status, position, task_id),
        )
        _emit(conn, "tasks", "task", task_id, {"status": status, "position": position}, actor=actor)
    return _row(conn, "tasks", "id", task_id)


# There is no reorder_tasks(): task(op=move) carries the one position anybody
# sets.


def close_task(conn, task_id: int, result: str, actor: str | None = None) -> dict:
    """Close a task with a one-line result: the works behind it collapse into that
    line and their rows are deleted.

    Refuses on a work that has REPORTED and not been closed, and names it. A
    still-running work is not checked.
    """
    reported = db.query_one(
        conn, "SELECT id FROM works WHERE task_id = ? AND status = 'done' ORDER BY id", (task_id,)
    )
    if reported is not None:
        raise ValueError(
            f"work {reported['id']} on task {task_id} has reported and is waiting to be closed — "
            "close it (close_work) and then close the task"
        )
    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE tasks SET status = 'done', result = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ?",
            (result, task_id),
        )
        db.execute(conn, "DELETE FROM works WHERE task_id = ?", (task_id,))
        _emit(conn, "tasks", "task", task_id, {"status": "done", "result": result}, actor=actor)
    return _row(conn, "tasks", "id", task_id)


def link_tasks(
    conn, blocking_task_id: int, blocked_task_id: int, actor: str | None = None
) -> dict:
    """task(op=link): blocking_task_id must finish before blocked_task_id can
    start. A pair is the whole of the relationship, so linking the same two
    tasks twice writes nothing the second time.
    """
    if blocking_task_id == blocked_task_id:
        raise ValueError("a task cannot depend on itself")
    blocking = _row(conn, "tasks", "id", blocking_task_id)
    if blocking is None:
        raise ValueError(f"no such task {blocking_task_id}")
    blocked = _row(conn, "tasks", "id", blocked_task_id)
    if blocked is None:
        raise ValueError(f"no such task {blocked_task_id}")
    with db.transaction(conn):
        db.execute(
            conn,
            "INSERT INTO task_dependencies (blocking_task_id, blocked_task_id) VALUES (?, ?) "
            "ON CONFLICT(blocking_task_id, blocked_task_id) DO NOTHING",
            (blocking_task_id, blocked_task_id),
        )
        _emit(
            conn, "tasks", "task_dependency", blocked_task_id,
            {"blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id},
            actor=actor,
        )
    return {"blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id}


def unlink_tasks(conn, blocking_task_id: int, blocked_task_id: int, actor: str | None = None) -> None:
    with db.transaction(conn):
        db.execute(
            conn, "DELETE FROM task_dependencies WHERE blocking_task_id = ? AND blocked_task_id = ?",
            (blocking_task_id, blocked_task_id),
        )
        _emit(
            conn, "tasks", "task_dependency", blocked_task_id,
            {"blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id, "deleted": True},
            actor=actor,
        )



# --------------------------------------------------------------------------- works


def assign_work(
    conn, *, agent_id: int, brief: str, task_id: int | None, branch: str, actor: str | None = None
) -> dict:
    """assign(agent, brief, task, branch).

    Branch is mandatory; the office never guesses one and never acts on one.
    This writes a work row and sends the brief: no git call, no checkout, no
    reservation. It takes no Config.

    The name lives in works.branch as what the director asked for; the agent
    creates the branch itself.
    """
    agent = _row(conn, "agents", "id", agent_id)
    # 'done' belongs in this tuple: a work that has reported and has not been
    # closed is still an obligation on both sides. 'failed' is deliberately NOT
    # here.
    active = db.query_one(
        conn,
        "SELECT id FROM works WHERE agent_id = ? AND status IN ('running', 'paused', 'done')",
        (agent_id,),
    )
    if active is not None:
        raise ValueError(f"agent '{agent['name']}' already has an active work ({active['id']})")

    ws_row = workspace_of(conn, agent_id) if agent["status"] != "provisioning" else None
    workspace_id = ws_row["id"] if ws_row is not None else None

    with db.transaction(conn):
        cur = db.execute(
            conn,
            "INSERT INTO works (task_id, agent_id, brief, branch, workspace_id, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (task_id, agent_id, brief, branch, workspace_id),
        )
        work_id = cur.lastrowid
        _emit(
            conn, "works", "work", work_id,
            {"agent_id": agent_id, "task_id": task_id, "branch": branch, "pending": workspace_id is None},
            actor=actor,
        )
    # Outside the transaction above: send_message opens its own, and a connection
    # holds one at a time. Assigning work to yourself sends nothing.
    if actor != agent["name"]:
        send_message(conn, actor, agent["name"], f"{brief}\n\nBranch for this work: {branch}")
    return _row(conn, "works", "id", work_id)


def _resolve_pending_work(conn, agent_id: int) -> None:
    """Attach the workspace to a work that was assigned before it existed.

    assign_work() against a 'provisioning' agent records the work with
    workspace_id NULL; hire() calls this once it reaches ready. At most one such
    work can exist.
    """
    work = db.query_one(
        conn, "SELECT * FROM works WHERE agent_id = ? AND workspace_id IS NULL AND status = 'running'", (agent_id,)
    )
    if work is None:
        return
    ws_row = workspace_of(conn, agent_id)
    with db.transaction(conn):
        db.execute(conn, "UPDATE works SET workspace_id = ? WHERE id = ?", (ws_row["id"], work["id"]))


def workspace_of(conn, agent_id: int) -> dict | None:
    """The one workspace an agent owns, or None.

    There is exactly one row to find: the partial unique index on
    workspaces(owner_agent_id) makes two-at-once an impossible state.
    """
    row = db.query_one(
        conn, "SELECT * FROM workspaces WHERE owner_agent_id = ?", (agent_id,)
    )
    return dict(row) if row is not None else None


def current_work(conn, agent_id: int) -> dict | None:
    """The work this agent is on, or None.

    **No status filter at all**: a row that exists is a work that is not closed,
    and every state it can be in is still the agent's assignment. Newest first.
    """
    row = db.query_one(
        conn,
        "SELECT * FROM works WHERE agent_id = ? ORDER BY id DESC",
        (agent_id,),
    )
    return dict(row) if row is not None else None


def finish_work(conn, work_id: int, summary: str | None = None, actor: str | None = None) -> dict:
    """work(op=finish). Any PR creation is the caller's job.

    The summary reaches the director as a direct message from the agent that
    finished. The row is marked before the message is sent.

    Silent with no director hired, and silent for a director finishing its own
    work.

    The row is marked `done`, not deleted, and nothing is written to the task:
    close_work() does both.
    """
    work = _row(conn, "works", "id", work_id)
    if work is None:
        raise ValueError(f"no such work {work_id}")
    agent_name = _agent_name(conn, work["agent_id"])
    with db.transaction(conn):
        # The three reason columns go with the status: a row describes the state it
        # is IN, never a state it was in. The output tail stays.
        db.execute(
            conn,
            "UPDATE works SET status = 'done', fail_reason = NULL, pause_reason = NULL, "
            "resume_after = NULL WHERE id = ?",
            (work_id,),
        )
        _emit(conn, "works", "work", work_id, {"status": "done", "agent": agent_name}, actor=actor)
    # Outside the transaction: send_message opens its own. The sender is the
    # agent the work belonged to rather than `actor`.
    director = db.query_one(conn, "SELECT name FROM agents WHERE kind = 'director'")
    if summary and director is not None and director["name"] != agent_name:
        send_message(conn, agent_name, director["name"], summary)
    return _row(conn, "works", "id", work_id)


def close_work(conn, work_id: int, summary: str | None = None, actor: str | None = None) -> dict:
    """Accept a reported work and close it: the row goes, and the accepted summary
    becomes the work's one line on its task.

    The only thing that deletes a work that did not fail.

    `summary` is the ACCEPTED result in the closer's own words. The append
    happens here and nowhere else.

    Optional: a work need not belong to a task.

    One state is refused, and only one: `failed`. That deletion is
    dismiss_work().
    """
    work = _row(conn, "works", "id", work_id)
    if work is None:
        raise ValueError(f"no such work {work_id}")
    if work["status"] == "failed":
        raise ValueError(
            f"work {work_id} died ({work['fail_reason']}) — it delivered no result and cannot be "
            "closed as though it had. Write it off with work_dismiss once you have read its "
            "output tail, or hand the tree on with work_reassign(workspace='inherit')"
        )
    agent_name = _agent_name(conn, work["agent_id"])
    with db.transaction(conn):
        if summary and work["task_id"] is not None:
            db.execute(
                conn,
                "UPDATE tasks SET result = TRIM(COALESCE(result || char(10), '') || ?), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (summary, work["task_id"]),
            )
        db.execute(conn, "DELETE FROM works WHERE id = ?", (work_id,))
        _emit(conn, "works", "work", work_id, {"status": "closed", "agent": agent_name}, actor=actor)
    # The one who did it is told, quietly.
    closed_by = f" by {actor}" if actor else ""
    try:
        tell_quietly(
            conn, agent_name,
            f"[office] Work {work_id} is closed{closed_by} — it is off your hands. Nothing "
            "further is expected on it and it is not reported again; anything more on this "
            "subject would come as a new assignment."
            + (f" Accepted as: {summary}" if summary else ""),
        )
    except Exception:
        # The work IS closed; the row is gone and the task has its line.
        log.exception("could not tell %s that work %s was closed", agent_name, work_id)
    # The row no longer exists, so this is the last copy of it.
    return {**work, "status": "closed"}


def fail_work(conn, work_id: int, reason: str, output_tail: str | None = None, actor: str | None = None) -> dict:
    """Record a classified death.

    output_tail is the caller's ring-buffer snapshot. actor is None except for
    'killed', where it is whoever stopped it.

    Touches nothing outside the work row: the workspace is left standing on its
    branch with its uncommitted changes.
    """
    work = _row(conn, "works", "id", work_id)
    agent_name = _agent_name(conn, work["agent_id"])
    with db.transaction(conn):
        db.execute(
            conn, "UPDATE works SET status = 'failed', fail_reason = ?, output_tail = ? WHERE id = ?",
            (reason, output_tail, work_id),
        )
        _emit(conn, "works", "work", work_id, {"status": "failed", "fail_reason": reason, "agent": agent_name}, actor=actor)
    return _row(conn, "works", "id", work_id)


def pause_work(
    conn, work_id: int, reason: str, resume_after: int | None = None, actor: str | None = None
) -> dict:
    """Stand a work down without calling it a failure.

    The working tree, the branch and the brief are intact. `resume_after` is
    epoch seconds UTC, or None when there is no reset time.
    """
    work = _row(conn, "works", "id", work_id)
    if work is None:
        raise ValueError(f"no such work {work_id}")
    agent_name = _agent_name(conn, work["agent_id"])
    resume = str(resume_after) if resume_after else None
    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE works SET status = 'paused', pause_reason = ?, resume_after = ? WHERE id = ?",
            (reason, resume, work_id),
        )
        _emit(
            conn, "works", "work", work_id,
            {"status": "paused", "pause_reason": reason, "resume_after": resume, "agent": agent_name},
            actor=actor,
        )
    return _row(conn, "works", "id", work_id)


def dismiss_work(conn, work_id: int, actor: str | None = None) -> dict:
    """Write off a work that died: delete the row and the output tail with it.

    The one way a failed work stops being state, and it takes only a work whose
    status is `failed`. A running work is stopped; a paused or reported one is
    closed with close_work().

    Not a task closure: the task is left where it is.
    """
    work = _row(conn, "works", "id", work_id)
    if work is None:
        raise ValueError(f"no such work {work_id}")
    if work["status"] != "failed":
        raise ValueError(
            f"work {work_id} is {work['status']}, not failed — only a work that has already died "
            "can be written off (stop a running one, reassign or close a paused one)"
        )
    agent_name = _agent_name(conn, work["agent_id"])
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM works WHERE id = ?", (work_id,))
        _emit(
            conn, "works", "work", work_id,
            {"status": "dismissed", "agent": agent_name, "fail_reason": work["fail_reason"]},
            actor=actor,
        )
    # The row is gone, so this is the last copy of it.
    return work


def quota_reset_for(conn, runtime: str) -> int | None:
    """When this runtime's tightest known bucket resets, as epoch seconds.

    The tightest rather than "the one that ran out": which bucket actually
    emptied is not something the office knows. Named as an estimate wherever it
    is shown.
    """
    row = db.query_one(
        conn,
        "SELECT reset_time FROM quota WHERE runtime = ? AND reset_time IS NOT NULL "
        "ORDER BY remaining_fraction ASC",
        (runtime,),
    )
    return int(row["reset_time"]) if row is not None else None


def reassign_work(
    conn, config: Config, work_id: int, to_agent_id: int, *, workspace: str = "inherit", actor: str | None = None
) -> dict:
    """work_reassign(work, to_agent, workspace=inherit|fresh).

    'inherit' hands the same workspace on, uncommitted changes and all; 'fresh'
    clones a clean one, which arrives on the repository's default branch.

    In both modes the recipient's own previous workspace becomes ownerless
    rather than deleted.

    The work is revived: status 'running', and every reason column cleared.
    """
    if workspace not in ("inherit", "fresh"):
        raise ValueError("workspace must be 'inherit' or 'fresh'")
    work = _row(conn, "works", "id", work_id)
    if work is None:
        raise ValueError(f"no such work {work_id}")
    to_agent = _row(conn, "agents", "id", to_agent_id)
    # The same tuple assign_work() refuses on, 'done' included.
    active = db.query_one(
        conn,
        "SELECT id FROM works WHERE agent_id = ? AND status IN ('running', 'paused', 'done') AND id != ?",
        (to_agent_id, work_id),
    )
    if active is not None:
        raise ValueError(f"agent '{to_agent['name']}' already has an active work ({active['id']})")

    old_ws = db.query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (work["workspace_id"],)) if work["workspace_id"] else None

    if workspace == "inherit":
        if old_ws is None:
            raise ValueError(f"work {work_id} has no workspace yet to inherit")
        git.transfer_workspace(old_ws["path"], to_agent["name"])
        new_workspace_id = old_ws["id"]
        existing = workspace_of(conn, to_agent_id)
        with db.transaction(conn):
            if existing is not None and existing["id"] != old_ws["id"]:
                # Freed before the new one is attached: the unique index would refuse the
                # second owner otherwise.
                db.execute(
                    conn, "UPDATE workspaces SET owner_agent_id = NULL WHERE id = ?", (existing["id"],)
                )
            db.execute(conn, "UPDATE workspaces SET owner_agent_id = ? WHERE id = ?", (to_agent_id, old_ws["id"]))
    else:
        existing = workspace_of(conn, to_agent_id)
        if existing is not None:
            # Same rule as 'inherit': whatever the recipient held becomes unowned.
            with db.transaction(conn):
                db.execute(
                    conn, "UPDATE workspaces SET owner_agent_id = NULL WHERE id = ?", (existing["id"],)
                )
        # The intake is held as it is taken rather than read off the result: a clone
        # that fails must still leave everyone told that the branch moved.
        intake: git.Intake | None = None

        def _took(taken: git.Intake) -> None:
            nonlocal intake
            intake = taken

        try:
            new_ws = git.create_workspace(config, to_agent["name"], on_intake=_took)
        except Exception:
            _announce_intake_quietly(conn, intake, f"the reassignment of work {work_id}")
            raise
        announce_intake(conn, intake)
        with db.transaction(conn):
            db.execute(
                conn, "INSERT INTO workspaces (id, path, owner_agent_id) VALUES (?, ?, ?)",
                (new_ws.id, new_ws.path, to_agent_id),
            )
        new_workspace_id = new_ws.id

    with db.transaction(conn):
        # fail_reason goes with the status: a revived work carries no reason for a
        # death it was brought back from.
        db.execute(
            conn,
            "UPDATE works SET agent_id = ?, workspace_id = ?, status = 'running', "
            "fail_reason = NULL, pause_reason = NULL, resume_after = NULL "
            "WHERE id = ?",
            (to_agent_id, new_workspace_id, work_id),
        )
        _emit(conn, "works", "work", work_id, {"agent_id": to_agent_id, "workspace": workspace}, actor=actor)
    return _row(conn, "works", "id", work_id)


def publish_workspace(conn, config: Config, agent_id: int) -> git.PublishResult:
    """Push the caller's own workspace to project.git (git.publish()).

    Which branch is not asked and not stored: git.publish() reads it off the
    working tree and names it in the result.
    """
    ws = workspace_of(conn, agent_id)
    if ws is None:
        raise ValueError(f"agent {agent_id} has no workspace to publish")
    return git.publish(config, ws["path"])


# --------------------------------------------------------------------------- tickets
#
# A ticket is a gate, not a message. The authority rule is NOT symmetric:
#
#   addressed to the human owner -> only that human may resolve
#   addressed to an agent        -> that agent, OR the human owner


def create_ticket(conn, *, title: str, body: str | None, author: str, addressee: str, kind: str, task_id: int | None = None) -> dict:
    with db.transaction(conn):
        cur = db.execute(
            conn,
            "INSERT INTO tickets (title, body, author, addressee, kind, task_id) VALUES (?, ?, ?, ?, ?, ?)",
            (title, body, author, addressee, kind, task_id),
        )
        ticket_id = cur.lastrowid
        _emit(
            conn, "tickets", "ticket", ticket_id,
            {"title": title, "author": author, "addressee": addressee, "kind": kind},
            actor=author,
        )
    return _row(conn, "tickets", "id", ticket_id)


def comment_ticket(conn, ticket_id: int, author: str, body: str) -> dict:
    ticket = _row(conn, "tickets", "id", ticket_id)
    if ticket is None:
        raise ValueError(f"no such ticket {ticket_id}")
    with db.transaction(conn):
        cur = db.execute(conn, "INSERT INTO ticket_comments (ticket_id, author, body) VALUES (?, ?, ?)", (ticket_id, author, body))
        comment_id = cur.lastrowid
        _emit(conn, "tickets", "ticket_comment", ticket_id, {"comment_id": comment_id, "author": author}, actor=author)
    return _row(conn, "ticket_comments", "id", comment_id)


def list_tickets(conn, *, status: str | None = None, addressee: str | None = None) -> list[dict]:
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if addressee is not None:
        clauses.append("addressee = ?")
        params.append(addressee)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # By id, not created_at: a burst filed in one turn can share a timestamp.
    rows = db.query(conn, f"SELECT * FROM tickets {where} ORDER BY id ASC", tuple(params))
    return [dict(r) for r in rows]


def get_ticket(conn, ticket_id: int) -> dict | None:
    """One ticket with its body and every comment on it, oldest first. None when
    there is no such ticket.
    """
    ticket = _row(conn, "tickets", "id", ticket_id)
    if ticket is None:
        return None
    comments = db.query(
        conn,
        "SELECT id, author, body, created_at FROM ticket_comments WHERE ticket_id = ? ORDER BY id ASC",
        (ticket_id,),
    )
    return {**dict(ticket), "comments": [dict(r) for r in comments]}


def resolve_ticket(conn, ticket_id: int, *, resolved_by: str, resolution: str) -> dict:
    """The authority rule, enforced here so no caller can bypass it: the addressee
    may always resolve their own ticket, and the human owner may resolve
    anyone's. One thing is forbidden: an agent resolving a ticket addressed to
    the human owner.

    Resolving wakes nobody and sends nothing.
    """
    ticket = _row(conn, "tickets", "id", ticket_id)
    if ticket is None:
        raise ValueError(f"no such ticket {ticket_id}")
    owner_name = get_owner_name(conn)
    if resolved_by != ticket["addressee"] and resolved_by != owner_name:
        raise PermissionError(
            f"ticket {ticket_id} is addressed to '{ticket['addressee']}'; "
            f"only they or the owner ('{owner_name}') can resolve it — '{resolved_by}' cannot"
        )
    if ticket["status"] == "resolved":
        raise ValueError(f"ticket {ticket_id} is already resolved")

    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE tickets SET status = 'resolved', resolution = ?, resolved_by = ?, "
            "resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (resolution, resolved_by, ticket_id),
        )
        _emit(
            conn, "tickets", "ticket", ticket_id,
            {"status": "resolved", "resolved_by": resolved_by, "resolution": resolution},
            actor=resolved_by,
        )
    return _row(conn, "tickets", "id", ticket_id)


def link_ticket_task(conn, ticket_id: int, task_id: int, actor: str | None = None) -> dict:
    """ticket(op=link): task_id is a ticket's own optional link, set at creation or
    here. Lives on the ticket side, not on task(op=link), which is task-to-task
    sequencing.
    """
    ticket = _row(conn, "tickets", "id", ticket_id)
    if ticket is None:
        raise ValueError(f"no such ticket {ticket_id}")
    task = _row(conn, "tasks", "id", task_id)
    if task is None:
        raise ValueError(f"no such task {task_id}")
    with db.transaction(conn):
        db.execute(conn, "UPDATE tickets SET task_id = ? WHERE id = ?", (task_id, ticket_id))
        _emit(conn, "tickets", "ticket", ticket_id, {"task_id": task_id}, actor=actor)
    return _row(conn, "tickets", "id", ticket_id)


# --------------------------------------------------------------------------- PRs


def create_pr(
    conn, *, title: str, body: str | None, source_branch: str, target_branch: str, author_agent_id: int
) -> dict:
    """pr(op=create).

    The caller passes the branch that publish_workspace() just pushed
    (git.PublishResult.branch, read off that working tree).
    """
    with db.transaction(conn):
        cur = db.execute(
            conn,
            "INSERT INTO prs (title, body, source_branch, target_branch, author_agent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, body, source_branch, target_branch, author_agent_id),
        )
        pr_id = cur.lastrowid
        _emit(
            conn, "prs", "pr", pr_id,
            {"title": title, "source_branch": source_branch, "target_branch": target_branch},
            actor=_agent_name(conn, author_agent_id),
        )
    return _row(conn, "prs", "id", pr_id)


def comment_pr(conn, pr_id: int, author: str, body: str) -> dict:
    pr = _row(conn, "prs", "id", pr_id)
    if pr is None:
        raise ValueError(f"no such PR {pr_id}")
    with db.transaction(conn):
        cur = db.execute(conn, "INSERT INTO pr_comments (pr_id, author, body) VALUES (?, ?, ?)", (pr_id, author, body))
        comment_id = cur.lastrowid
        _emit(conn, "prs", "pr_comment", pr_id, {"comment_id": comment_id, "author": author}, actor=author)
    return _row(conn, "pr_comments", "id", comment_id)


def list_prs(conn) -> list[dict]:
    """Every open PR, newest first, with its author and how many comments it
    carries. The bodies of those comments are get_pr()'s job.
    """
    rows = db.query(
        conn,
        """
        SELECT prs.*, agents.name AS author_name,
               (SELECT COUNT(*) FROM pr_comments WHERE pr_comments.pr_id = prs.id) AS comment_count
        FROM prs JOIN agents ON agents.id = prs.author_agent_id
        WHERE prs.status = 'open'
        ORDER BY prs.id DESC
        """,
    )
    return [dict(r) for r in rows]


def get_pr(conn, pr_id: int) -> dict | None:
    """One PR with its review: the row, its author's name, and every comment on it
    in the order they were written. None when there is no such PR.
    """
    pr = db.query_one(
        conn,
        "SELECT prs.*, agents.name AS author_name FROM prs "
        "JOIN agents ON agents.id = prs.author_agent_id WHERE prs.id = ?",
        (pr_id,),
    )
    if pr is None:
        return None
    comments = db.query(
        conn,
        "SELECT id, author, body, created_at FROM pr_comments WHERE pr_id = ? ORDER BY id ASC",
        (pr_id,),
    )
    return {**dict(pr), "comments": [dict(r) for r in comments]}


def _comment_digest(conn, pr_id: int) -> str:
    """The review, oldest first, for the merge commit message."""
    rows = db.query(conn, "SELECT author, body FROM pr_comments WHERE pr_id = ? ORDER BY id ASC", (pr_id,))
    return "\n".join(f"- {r['author']}: {r['body']}" for r in rows)


# --------------------------------------------------------------------------- delivery

# Settings rows rather than columns: schema.sql is CREATE TABLE IF NOT
# EXISTS with no migrations.
#
# Neither half of the address is stored. WHERE work is delivered is
# git.owner_repo(config); WHICH branch is git.default_branch(config).
DELIVERY_LAST_SETTING = "delivery_last"


def get_delivery(conn, config: Config) -> dict:
    """Where finished work goes, and what happened the last time it went."""
    return {
        "repo": str(git.owner_repo(config)),
        "branch": git.default_branch(config),
        "last": json.loads(get_setting(conn, DELIVERY_LAST_SETTING, "") or "{}"),
    }


def take_owner_changes(conn, config: Config) -> git.Intake:
    """Bring the owner's own branch into the office (git.take_from_owner()).

    The button on the main page is this; the merge and a new workspace do their
    own intake and hand the result here to be announced.
    """
    intake = git.take_from_owner(config, branch=git.default_branch(config))
    announce_intake(conn, intake)
    return intake


def tell_quietly(conn, recipient: str, body: str) -> None:
    """One line for one participant that does NOT buy them a turn.

    A `messages` row on the common channel WITH a recipient: delivered the way a
    common-chat post is, and read by nobody else.
    """
    with db.transaction(conn):
        _write_message(conn, "all", OFFICE_SENDER, recipient, body, OFFICE_SENDER)


def announce_intake(conn, intake: git.Intake | None) -> None:
    """Say in the common chat that the main branch has moved.

    Only a branch that actually moved is announced.
    """
    if intake is None or intake.status != "moved":
        return
    send_message(
        conn,
        OFFICE_SENDER,
        "all",
        f"The main branch '{intake.branch}' has moved to {intake.commit[:10]}: work done outside "
        "the office has come in. Take it into anything you have open before you publish again "
        f"(git fetch origin, then merge or rebase onto origin/{intake.branch}); a branch that "
        "does not contain it cannot be merged.",
    )


def _announce_intake_quietly(conn, intake: git.Intake | None, what: str) -> None:
    """announce_intake on a path that is already on its way out with a failure.

    That failure is the one to report, so a second one raised in here must not
    replace it.
    """
    try:
        announce_intake(conn, intake)
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("could not announce the intake taken before %s failed", what)


def _record_delivery(conn, delivery: git.Delivery) -> dict:
    """Write the outcome of one delivery where the settings page will print it."""
    payload = {
        "status": delivery.status,
        "detail": delivery.detail,
        "repo": delivery.repo,
        "branch": delivery.branch,
    }
    stamp = db.query_one(conn, "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS at")
    set_setting(conn, DELIVERY_LAST_SETTING, json.dumps({**payload, "at": stamp["at"]}))
    return payload


def merge_pr(
    conn,
    config: Config,
    pr_id: int,
    actor: str | None = None,
    *,
    delete_branch: bool,
) -> dict:
    """pr(op=merge) - the merge itself is git.merge()'s.

    A merge writes the PR's description and a digest of its comments into the
    merge-commit message and deletes the row.

    'behind', 'blocked', 'diverged' and 'missing' all leave the PR open and hand
    the sentence back to whoever merged. 'up_to_date' deletes the row like a
    merged one and says in its own words that this merge is not what put the
    work there. 'merged' can carry a detail of its own: the delivery went
    through and the office's own branch did not move.

    `delete_branch` takes the source branch out of the office's repository on
    the two outcomes that delete the row, and on no other.
    """
    pr = _row(conn, "prs", "id", pr_id)
    if pr is None:
        raise ValueError(f"no such PR {pr_id}")

    digest = _comment_digest(conn, pr_id)
    parts = [pr["title"], "", pr["body"] or ""]
    if digest:
        parts += ["", "Comments:", digest]
    message = "\n".join(parts).strip() + "\n"

    result = git.merge(
        config,
        pr["source_branch"],
        pr["target_branch"],
        message=message,
        delete_source=delete_branch,
    )
    announce_intake(conn, result.intake)

    delivery = {}
    if result.delivery is not None and result.delivery.status != "off":
        delivery = _record_delivery(conn, result.delivery)

    if result.status == "merged":
        with db.transaction(conn):
            db.execute(conn, "DELETE FROM prs WHERE id = ?", (pr_id,))
            _emit(conn, "prs", "pr", pr_id, {"status": "merged", "commit": result.commit}, actor=actor)
        # The detail is empty on an ordinary merge and carries a sentence when
        # the office could not move its own branch onto the delivered commit.
        return {
            "status": "merged",
            "commit": result.commit,
            "delivery": delivery,
            "detail": result.detail,
            "source_branch": pr["source_branch"],
            "branch_deleted": result.source_deleted,
        }

    if result.status == "up_to_date":
        # The work is in the target branch, so the row goes the way a merged one
        # does. Reported as its own status, not as 'merged'.
        with db.transaction(conn):
            db.execute(conn, "DELETE FROM prs WHERE id = ?", (pr_id,))
            _emit(conn, "prs", "pr", pr_id, {"status": "up_to_date"}, actor=actor)
        return {
            "status": "up_to_date",
            "detail": result.detail,
            "source_branch": pr["source_branch"],
            "branch_deleted": result.source_deleted,
        }

    # 'behind', 'blocked', 'diverged' or 'missing' — the row is untouched and the
    # caller is told why in words it can pass on.
    return {"status": result.status, "detail": result.detail}


def close_pr(conn, pr_id: int, actor: str | None = None) -> dict:
    """pr(op=close) - abandon a request without merging it.

    The row is deleted and the event carries status 'closed'. Nothing durable is
    written: the branch stands and the workspace is untouched.
    """
    pr = _row(conn, "prs", "id", pr_id)
    if pr is None:
        raise ValueError(f"no such PR {pr_id}")
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM prs WHERE id = ?", (pr_id,))
        _emit(conn, "prs", "pr", pr_id, {"status": "closed", "title": pr["title"]}, actor=actor)
    return {"status": "closed", "title": pr["title"], "source_branch": pr["source_branch"]}


# --------------------------------------------------------------------------- wiki & rules


def get_wiki_page(conn, path: str) -> dict | None:
    """One page with its discussion: body, version, and every comment on it in the
    order they were written. What note(op=read) returns. None when there is no
    such page.

    The version is part of the answer: note(op=write) refuses a mismatch.
    """
    page = _row(conn, "wiki", "path", path)
    if page is None:
        return None
    comments = db.query(
        conn,
        "SELECT id, author, body, page_version, created_at FROM wiki_comments WHERE wiki_id = ? ORDER BY id ASC",
        (page["id"],),
    )
    return {**page, "comments": [dict(r) for r in comments]}


def list_wiki_pages(conn) -> list[dict]:
    """The index: every page, with how many comments it carries, ordered the way
    the wiki page and note(op=list) both show them. No bodies.
    """
    return [
        dict(r)
        for r in db.query(
            conn,
            """
            SELECT wiki.id, wiki.path, wiki.category, wiki.title, wiki.version,
                   wiki.updated_at, wiki.updated_by,
                   (SELECT COUNT(*) FROM wiki_comments WHERE wiki_comments.wiki_id = wiki.id) AS comment_count
            FROM wiki
            ORDER BY wiki.category COLLATE NOCASE, wiki.title COLLATE NOCASE
            """,
        )
    ]


def comment_wiki_page(conn, path: str, author: str, body: str) -> dict:
    """A remark about a whole page, kept beside it instead of in it.

    The page's current version is stamped onto the comment here and is not an
    argument. Commenting sends nobody anything.
    """
    page = db.query_one(conn, "SELECT id, version FROM wiki WHERE path = ?", (path,))
    if page is None:
        raise ValueError(f"no wiki page '{path}'")
    with db.transaction(conn):
        cur = db.execute(
            conn,
            "INSERT INTO wiki_comments (wiki_id, author, body, page_version) VALUES (?, ?, ?, ?)",
            (page["id"], author, body, page["version"]),
        )
        comment_id = cur.lastrowid
        _emit(
            conn, "wiki", "wiki_comment", page["id"],
            {"comment_id": comment_id, "author": author, "path": path},
            actor=author,
        )
    return _row(conn, "wiki_comments", "id", comment_id)


def note_wiki(conn, *, path: str, category: str, title: str, body: str, updated_by: str, expected_version: int | None) -> dict:
    """note(op=write, kind=wiki) - the one write path into the wiki.

    expected_version must match the page's current version; for a brand-new page
    pass None or 0. A mismatch raises rather than overwriting.
    """
    existing = db.query_one(conn, "SELECT * FROM wiki WHERE path = ?", (path,))
    if existing is None:
        if expected_version not in (None, 0):
            raise ValueError(f"wiki page '{path}' does not exist yet (got expected_version={expected_version})")
        with db.transaction(conn):
            cur = db.execute(
                conn,
                "INSERT INTO wiki (path, category, title, body, updated_by, version) VALUES (?, ?, ?, ?, ?, 1)",
                (path, category, title, body, updated_by),
            )
            wiki_id = cur.lastrowid
            _emit(conn, "wiki", "wiki", wiki_id, {"path": path, "version": 1}, actor=updated_by)
    else:
        if expected_version != existing["version"]:
            raise ValueError(
                f"wiki page '{path}' is at version {existing['version']}, not {expected_version} — reread before writing"
            )
        new_version = existing["version"] + 1
        with db.transaction(conn):
            db.execute(
                conn,
                "UPDATE wiki SET category = ?, title = ?, body = ?, prev_body = ?, updated_by = ?, "
                "version = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE path = ?",
                (category, title, body, existing["body"], updated_by, new_version, path),
            )
            _emit(conn, "wiki", "wiki", existing["id"], {"path": path, "version": new_version}, actor=updated_by)
    return get_wiki_page(conn, path)


def undo_wiki_page(conn, path: str, actor: str | None = None) -> dict:
    """Put back the body this page had before its last write.

    One step: one step is all the wiki keeps (wiki.prev_body). An ordinary
    write, so the version goes up and an undo can itself be undone. Category,
    title and author are left as they are.
    """
    page = db.query_one(conn, "SELECT * FROM wiki WHERE path = ?", (path,))
    if page is None:
        raise ValueError(f"no wiki page '{path}'")
    if page["prev_body"] is None:
        raise ValueError(
            f"wiki page '{path}' has only ever been written once — there is nothing behind it"
        )
    new_version = page["version"] + 1
    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE wiki SET body = ?, prev_body = ?, updated_by = ?, version = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE path = ?",
            (page["prev_body"], page["body"], actor or page["updated_by"], new_version, path),
        )
        _emit(conn, "wiki", "wiki", page["id"], {"path": path, "version": new_version, "undo": True}, actor=actor)
    return get_wiki_page(conn, path)


def delete_wiki_page(conn, path: str, expected_version: int, actor: str | None = None) -> None:
    existing = db.query_one(conn, "SELECT * FROM wiki WHERE path = ?", (path,))
    if existing is None:
        raise ValueError(f"no such wiki page '{path}'")
    if expected_version != existing["version"]:
        raise ValueError(f"wiki page '{path}' is at version {existing['version']}, not {expected_version}")
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM wiki WHERE path = ?", (path,))
        _emit(conn, "wiki", "wiki", existing["id"], {"path": path, "deleted": True}, actor=actor)


# There is no list_rules(). Every rule is already in every system prompt
# verbatim, and the knowledge page runs its own query.


def _announce_rule(conn, actor: str | None, line: str) -> None:
    """Tell every agent that the project's rules have changed.

    A rule reaches an agent through its system prompt, and a system prompt is
    fixed when the session is created, so a change is invisible to every session
    already open until this line arrives.

    Never raises: the rule is written either way.
    """
    try:
        for row in db.query(conn, "SELECT name FROM agents"):
            if row["name"] != actor:
                tell_quietly(conn, row["name"], f"[office] {line}")
    except Exception:
        log.exception("could not announce a rule change")


def create_rule(conn, text: str, created_by: str, title: str) -> dict:
    """A rule is a title and a paragraph, and both callers require both. The column
    is nullable all the same: NULL means "written before titles existed"."""
    with db.transaction(conn):
        cur = db.execute(
            conn, "INSERT INTO rules (title, text, created_by) VALUES (?, ?, ?)", (title, text, created_by)
        )
        rule_id = cur.lastrowid
        _emit(conn, "rules", "rule", rule_id, {"title": title, "text": text}, actor=created_by)
    _announce_rule(
        conn, created_by,
        f"A project rule was added by {created_by} and stands over the whole office from now on "
        f"— [{rule_id}] {title}: {text}",
    )
    return _row(conn, "rules", "id", rule_id)


def update_rule(conn, rule_id: int, text: str, title: str | None = None, actor: str | None = None) -> dict:
    """title=None means LEAVE THE TITLE ALONE, not "clear it" - COALESCE, not a
    plain assignment.
    """
    with db.transaction(conn):
        db.execute(
            conn, "UPDATE rules SET title = COALESCE(?, title), text = ? WHERE id = ?", (title, text, rule_id)
        )
        _emit(conn, "rules", "rule", rule_id, {"title": title, "text": text}, actor=actor)
    rule = _row(conn, "rules", "id", rule_id)
    _announce_rule(
        conn, actor,
        f"A project rule was changed by {actor or 'the owner'} — [{rule_id}] "
        f"{rule['title'] or 'untitled'}: {rule['text']}",
    )
    return rule


def delete_rule(conn, rule_id: int, actor: str | None = None) -> None:
    rule = _row(conn, "rules", "id", rule_id)
    with db.transaction(conn):
        db.execute(conn, "DELETE FROM rules WHERE id = ?", (rule_id,))
        _emit(conn, "rules", "rule", rule_id, {"deleted": True}, actor=actor)
    if rule is not None:
        _announce_rule(
            conn, actor,
            f"A project rule was withdrawn by {actor or 'the owner'} and no longer stands — "
            f"[{rule_id}] {rule['title'] or 'untitled'}: {rule['text']}",
        )
