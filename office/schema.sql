-- Office.AI schema. CREATE TABLE IF NOT EXISTS only; no migrations.
-- A closed work and a merged or closed PR are deleted, not archived; work_log keeps a work's record.

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('director', 'lead', 'executor')),
    -- The agent this one reports to, the director or a lead. NULL for the director
    -- and for nobody else.
    manager_agent_id INTEGER REFERENCES agents(id),
    -- One line, set by the hire. NULL for the director.
    title TEXT,
    -- Standing instructions, printed into this agent's system prompt. NULL when
    -- there are none.
    instructions TEXT,
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
    -- The task this one sits under. NULL for a top-level task.
    parent_task_id INTEGER REFERENCES tasks(id),
    status TEXT NOT NULL DEFAULT 'idea'
        CHECK (status IN ('idea', 'planned', 'needs_clarification', 'in_progress', 'paused', 'done', 'cancelled')),
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
    -- The assigner: whoever last called assign or work_reassign on this work. The
    -- report and the failure and pause notices go to it.
    assigned_by_agent_id INTEGER NOT NULL REFERENCES agents(id),
    brief TEXT NOT NULL,
    -- The branch the assigner named. The agent creates it in its own workspace.
    branch TEXT NOT NULL,
    -- Nullable.
    workspace_id TEXT REFERENCES workspaces(id),
    -- A row that exists is a work that is OPEN; closing one deletes it.
    -- 'running' is the default the ordinary open work is written with.
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'failed', 'done')),
    -- hub_restart: the assignee's turn died with the hub.
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

-- An answer an agent waits for from a local service, reached at the address
-- /hooks/<token>. A row with `closed_at` NULL is open. `closed_at` is set when
-- the answer is taken or `due_at` passes; a closed row is deleted when its
-- agent's turn ends.
CREATE TABLE IF NOT EXISTS expectations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    token     TEXT NOT NULL UNIQUE,
    agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    about     TEXT NOT NULL,
    opened_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    due_at    TEXT NOT NULL,
    closed_at TEXT
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

-- The office's copy of each wiki page in each workspace's sandbox,
-- <sandbox>/wiki/<path>.md: the version and the body the office last wrote
-- there. A file whose text differs from `body` holds edits its agent has not
-- published; `body` is what a publish of that file is merged from.
CREATE TABLE IF NOT EXISTS wiki_copies (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (workspace_id, path)
);

-- `title` is one short line naming what the rule is about.
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by TEXT NOT NULL
);

-- Saved hiring compositions every manager can reuse: runtime, model and effort.
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy')),
    model TEXT NOT NULL,
    effort TEXT
);

-- The current snapshot; quota_polls holds the history. The key is (runtime,
-- label). remaining_fraction is always what is LEFT, and 1.0 for a bucket whose
-- reset time has passed; reset_time is epoch seconds UTC, or NULL when the
-- vendor's answer could not be turned into an instant or it has passed.
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

-- The owner's price list of the works a manager assigns, in the order the owner
-- set. `price` is the owner's own text; the distinct roles are the roles assign
-- accepts.
CREATE TABLE IF NOT EXISTS price_works (
    position INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    complexity TEXT NOT NULL,
    size TEXT NOT NULL,
    model TEXT NOT NULL,
    price TEXT NOT NULL
);

-- The owner's price list of what a lead spends on its own, in the order the
-- owner set. `price` is the owner's own text.
CREATE TABLE IF NOT EXISTS price_own (
    position INTEGER PRIMARY KEY,
    what TEXT NOT NULL,
    size TEXT NOT NULL,
    price TEXT NOT NULL
);

-- One-off key/value config.
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

-- One row per work, written when it is assigned and updated as it goes; no
-- code path deletes a row, and no foreign key reaches it. Numbers and names
-- only; no text of any brief or report. The owner reads it with SQL.
CREATE TABLE IF NOT EXISTS work_log (
    work_id INTEGER PRIMARY KEY,
    task_id INTEGER,
    -- The work's current holder: the assignee, and the agent it was last
    -- reassigned to.
    agent TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    -- The agent that assigned the work first.
    assigned_by TEXT NOT NULL,
    branch TEXT NOT NULL,
    -- NULL for a work assigned to a lead or the director.
    role TEXT,
    complexity TEXT,
    assigned_at TEXT NOT NULL,
    -- How many times the holder reported it with work(op=finish).
    reports INTEGER NOT NULL DEFAULT 0,
    -- The latest merged PR from the work's branch, its merge commit, and the size
    -- of every merge of that branch added up: files changed, and insertions plus
    -- deletions. NULL for a work with no merge; the size is also NULL where git
    -- refused to measure it.
    pr_id INTEGER,
    merge_commit TEXT,
    files_changed INTEGER,
    lines_changed INTEGER,
    -- NULL while the work is open. 'closed', 'dismissed', 'failed' or
    -- 'task_closed' (deleted with its task); a failed work that is reassigned
    -- is open again.
    ended_at TEXT,
    ended_as TEXT
);

-- One vendor process an agent ran: a turn, a compaction the office asked for,
-- or a keep-alive ping. Numbers and names only; no text of any message, prompt
-- or tool. The office reads it for the keep-alive ping; the owner reads the
-- rest with SQL.
CREATE TABLE IF NOT EXISTS usage_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT,
    session_id TEXT,
    process TEXT NOT NULL CHECK (process IN ('turn', 'compact')),
    -- 1 when the turn opened its session.
    new_session INTEGER NOT NULL DEFAULT 0,
    -- The agent's running work and its task as the process started; no foreign
    -- key, since a closed work's row is deleted.
    work_id INTEGER,
    task_id INTEGER,
    -- The distinct senders of the direct messages the turn started with, in
    -- order, comma-separated: a participant, 'office', 'hook:<service>', or the
    -- agent's own name for a wake it set.
    woken_by TEXT,
    chat_lines INTEGER NOT NULL DEFAULT 0,
    quiet_lines INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    -- 'clean', or the death's reason.
    outcome TEXT NOT NULL,
    context_used INTEGER,
    -- The process's own totals: as the vendor printed them, except agy's, which
    -- are the sums of the process's requests, NULL for a process with none; NULL
    -- where the vendor prints none.
    input_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_write_1h_tokens INTEGER,
    output_tokens INTEGER,
    thinking_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_usage_turns_agent ON usage_turns(agent, id);

-- One model request inside a process, in order.
CREATE TABLE IF NOT EXISTS usage_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL REFERENCES usage_turns(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    model TEXT,
    -- Input not read from the cache.
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    -- NULL where the vendor prints no cache write (agy).
    cache_write_tokens INTEGER,
    -- claude: the part of cache_write_tokens written with the one-hour TTL.
    cache_write_1h_tokens INTEGER,
    -- NULL for claude.
    output_tokens INTEGER,
    -- codex: reasoning_output_tokens; agy: thinking_tokens; claude: NULL.
    thinking_tokens INTEGER,
    -- Tool names called in this request, comma-separated; an office tool with
    -- an op as '<tool>:<op>'. NULL where the vendor does not say (codex, agy).
    tools TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_requests_turn ON usage_requests(turn_id, seq);

-- A compaction, manual or the vendor's own, as the vendor reported it.
CREATE TABLE IF NOT EXISTS usage_compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL REFERENCES usage_turns(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    trigger TEXT,
    pre_tokens INTEGER,
    post_tokens INTEGER
);

-- Every quota poll's reading that differs from the one before it for its
-- bucket, exactly as the vendor gave it.
CREATE TABLE IF NOT EXISTS quota_polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    runtime TEXT NOT NULL,
    label TEXT NOT NULL,
    remaining_fraction REAL NOT NULL,
    reset_time TEXT
);

CREATE INDEX IF NOT EXISTS idx_quota_polls_bucket ON quota_polls(runtime, label, id);

-- A runtime and model that refused a turn for quota: no turn starts on them
-- before `until` (epoch seconds).
CREATE TABLE IF NOT EXISTS quota_holds (
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    until INTEGER NOT NULL,
    PRIMARY KEY (runtime, model)
);
