-- Office.AI schema. CREATE TABLE IF NOT EXISTS only; no migrations.
-- A closed work and a merged or closed PR are deleted, not archived.

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('director', 'executor')),
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy')),
    model TEXT NOT NULL,
    effort TEXT,
    status TEXT NOT NULL DEFAULT 'provisioning'
        CHECK (status IN ('provisioning', 'ready', 'running', 'idle')),
    -- NULL means "no conversation to resume".
    session_id TEXT,
    context_used INTEGER,
    context_limit INTEGER,
    -- How far delivery has got.
    last_seen_message_id INTEGER,
    -- Where the message watermark stood when the CURRENT turn spawned. NULL means
    -- "no turn in flight": set when a turn spawns, cleared when it ends, however
    -- it ends.
    turn_start_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A workspace outlives its owner. Keyed by a stable id, never by path or by
-- agent name.
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    owner_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL
);

-- One workspace per agent.
-- Partial (WHERE NOT NULL).
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_one_per_agent
    ON workspaces(owner_agent_id) WHERE owner_agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'idea'
        CHECK (status IN ('idea', 'planned', 'needs_clarification', 'in_progress', 'paused', 'done')),
    position INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row: blocking_task_id must finish before blocked_task_id starts. No
-- cycle detection; a task cannot block itself.
CREATE TABLE IF NOT EXISTS task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blocking_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocked_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (blocking_task_id != blocked_task_id),
    UNIQUE (blocking_task_id, blocked_task_id)
);

CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    brief TEXT NOT NULL,
    -- The branch the DIRECTOR named when assigning this work. The agent creates it
    -- in its own workspace.
    branch TEXT NOT NULL,
    -- Nullable.
    workspace_id TEXT REFERENCES workspaces(id),
    -- A row that exists is a work that is OPEN; closing one deletes it.
    -- 'running' is the default the ordinary open work is written with.
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'failed', 'done')),
    -- hub_restart: every work still 'running' when the hub
    -- starts up is a process that died with it.
    fail_reason TEXT
        CHECK (fail_reason IN (
            'context_overflow', 'quota_exhausted', 'tool_error', 'crash', 'killed', 'hub_restart'
        )),
    -- Why a work is 'paused', and when it could resume.
    -- resume_after is epoch seconds UTC; NULL when there is no reset time.
    pause_reason TEXT,
    resume_after TEXT,
    -- Raw tail of stdout, only ever populated on a crash-like failure;
    -- lives only until the work record itself is deleted.
    output_tail TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Delivery read tracking lives on agents (last_seen_message_id). The owner's
-- reading position is a different question, kept in `owner_reading` below.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL CHECK (channel IN ('all', 'dm')),
    sender TEXT NOT NULL,
    recipient TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- How far the owner has READ each conversation: one row per conversation he can
-- open, a direct-message thread or the common chat.
--
-- Not the same kind of mark as agents.last_seen_message_id. Nothing is consumed
-- when this one moves, and it decides only what to highlight.
CREATE TABLE IF NOT EXISTS owner_reading (
    channel TEXT NOT NULL CHECK (channel IN ('all', 'dm')),
    peer TEXT NOT NULL,
    seen_message_id INTEGER NOT NULL,
    PRIMARY KEY (channel, peer)
);

-- A message somebody has asked to be sent later. office/bus.py's tick sends
-- every row whose `due_at` has passed and deletes it, so a row that exists is a
-- wake that has not happened yet.
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    due_at     TEXT NOT NULL,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row per mutation: what changed, and who changed it. Nothing reads this
-- table back. Writing a row is the signal: core's `_notify` pushes it to the
-- browser, and a page element then re-reads its own data from the tables that
-- hold it.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER,
    payload TEXT,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    author_agent_id INTEGER NOT NULL REFERENCES agents(id),
    -- Only 'open' can ever appear in this column: both ways a request is answered
    -- delete the row.
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'conflicted', 'merged', 'closed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pr_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- version: note() takes an expected version and rejects a mismatch. `path` is an
-- identifier, not a filesystem path.
CREATE TABLE IF NOT EXISTS wiki (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    -- The body this page had before its last write, and the whole of the wiki's
    -- history: one step back. NULL until the page is written twice.
    prev_body TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_by TEXT NOT NULL
);

-- Comments on a whole wiki page. `page_version` is the page's `version` at the
-- moment the comment was written.
CREATE TABLE IF NOT EXISTS wiki_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_id INTEGER NOT NULL REFERENCES wiki(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    page_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- `title` is one short line naming what the rule is about. Nullable: the column
-- reaches an existing database through office/db.py::_ADDED_COLUMNS.
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by TEXT NOT NULL
);

-- Saved hiring compositions the director can reuse: runtime, model and effort.
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy')),
    model TEXT NOT NULL,
    effort TEXT
);

-- Current snapshot only, no history. The key is (runtime, label).
-- remaining_fraction is always what is LEFT; reset_time is epoch seconds UTC,
-- or NULL when the vendor's answer could not be turned into an instant.
CREATE TABLE IF NOT EXISTS quota (
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy')),
    label TEXT NOT NULL,
    remaining_fraction REAL NOT NULL,
    reset_time TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (runtime, label)
);

-- What each runtime will actually accept as a model. Current snapshot only, no
-- history. `efforts` is NULL where the vendor publishes none, which is not the
-- same as "this model takes no effort".
CREATE TABLE IF NOT EXISTS models (
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy')),
    model_id TEXT NOT NULL,
    display_name TEXT,
    efforts TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (runtime, model_id)
);

-- A working tree the office owns and agents run commands on one at a time
-- (office/stages.py). `reason` is set only while `state` is 'broken'. The queue,
-- a pending reset or delete and the run in progress live in memory.
CREATE TABLE IF NOT EXISTS stages (
    name TEXT PRIMARY KEY,
    prepare TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'ready', 'broken')),
    reason TEXT
);

-- The chat and DM views read one page of history at a time, newest-first with a
-- LIMIT and an `id <` cursor; the bus scans for undelivered rows by id.
CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_id ON messages(recipient, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender, id);

-- Every ordering and every cursor in this office runs on `id`.

-- Director's runtime/model/effort/instructions and any other one-off key/value config.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Gate, not a message: a ticket addressed to a human can only be closed by that
-- human, and core.resolve_ticket enforces it for every caller.
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT,
    author TEXT NOT NULL,
    addressee TEXT NOT NULL,
    -- Open list ("question" | "bug" today) — informational only, no CHECK,
    -- so a third kind doesn't need a schema change.
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The owner's journal of system notices, and the source of the plate under the
-- director chat: when, how loud, and what happened in words.
--
--   'critical' — the director cannot answer him right now and nothing else on
--                any page would tell him so.
--   'info'     — everything else worth a line in his log.
--
-- Anything that needs no attention is not written here at all.
--
-- `seen_at` is stamped by the × on the plate and by nothing else. The journal
-- page ignores it: dismissing a notice hides the plate, never the record.
-- Kept forever.
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'info')),
    text TEXT NOT NULL,
    seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
