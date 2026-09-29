# Office.AI — Design

An environment in which several AI agents from different vendors work together on
one machine: a shared repository, branches, pull requests, chat, a task board and
a knowledge base.

This document states how the system is built.

---

## 1. Scope

The office provides workspaces, git, stages, communication, a board, knowledge,
visible quota and context figures, and the mechanics that wake an agent.

The office does not impose a method. It does not require review, does not block a
merge and does not decide for the director or a lead. Behaviour is set by text —
each agent's standing instructions and the project's rules — not by code.

Code enforces two things only: integrity (merge preconditions, an agent that
cannot be deleted mid-turn) and authority (tickets, tools split by role, a
manager's reach bounded by its subtree).

**Authority is about tools, not about the disk.** Agents run with permissions
bypassed, so the office's database, its files and other agents' workspaces are
physically reachable from a workspace — they are a few levels up from the working
directory. There is no technical wall and there will not be one: it cannot be
built against a process that is allowed everything. The prohibition on writing
past the tools lives as a line in every system prompt.

---

## 2. Participants

| Role | What they do |
|---|---|
| **Owner** (a human) | Sets the director's model and standing instructions; writes rules and wiki; files tasks and edits their titles and bodies; takes part in the conversation |
| **Director** | Heads the team: hires, fires, moves, instructs and assigns anywhere in it; runs the board; merges; writes rules; manages the stages; stops turns. The main chat is wired to it |
| **Lead** | Hires, fires, moves, instructs and assigns within its own subtree; runs the board with the director; merges its subtree's PRs; stops its subtree's turns; works in its own workspace |
| **Executor** | Works in its own workspace |

**The team is a tree.** Every agent but the director has a manager,
`agents.manager_agent_id` — the agent it reports to, the director or a lead; the
director has none. The director and the leads are the managers. An agent's
subtree is everyone whose chain of managers reaches it, itself excluded; the
director's subtree is every agent but itself. What a manager may do to whom is
section 14's.

Every agent an agent hires has a title, `agents.title`: one line, such as "Economy
architect" or "Advisor to the economy lead", required by the hire. The director has
none.

**Standing instructions.** Every agent has optional standing instructions,
`agents.instructions`. The owner writes the director's on the settings page;
anyone else's are written by any manager above it, with
`agent(op=instruct, agent, text)`, and empty text clears them. A moved agent keeps
its instructions, for the managers above it in its new place to rewrite or clear.
They go into the agent's system prompt (section 4). A manager above the agent reads
them with `agent(op=instructions, agent)`; `roster()` prints the caller's own as they
stand now, and nobody else's.

Every participant's name lives in one namespace; there are no role constants. The
owner is a named participant like any other — the setting `owner_name`, default
`Owner`. `say(to=…)` takes a participant's name or `all`. The name `office`
belongs to the office itself.

Two names are refused when hiring: the owner's and `office`. A name is an
identity, and taking either would hand an agent the other's authority. An agent's
name is Latin letters, digits, `.`, `_` and `-`, starting and ending with a letter or
a digit; the owner's name may not contain a colon.

A local service is not a participant. It answers an agent's expectation (section
4), and its message arrives from `hook:<service>`, a sender no participant can be.
`say` and `remind` refuse a `hook:` recipient.

**The owner hires the director**, on the main page, once — it is the only hire
control in the interface. Afterwards the owner changes the director's runtime,
model and effort from the settings page; a runtime change is refused while a turn
or a compaction runs on the director's session. `agent(op=fire)` refuses a director
target; no tool lets an agent change anyone's model, its own included.

The owner's interface carries authority (rules, wiki, the director's settings,
resolving tickets addressed to him), conversation (common chat, direct messages,
comments) and observation. Of planning he has only filing a task into the "idea"
column and editing titles and bodies: the board is the managers' instrument — the
director's and the leads'.
Merging, assigning work, hiring leads and executors and moving cards do not exist
in the interface. The exceptions are in section 16.

The message bus restricts nobody: any participant may write to any other.

---

## 3. Turns and processes

**One process per turn.** The process lives as long as the turn and ends with it.
Continuing a session means a new process:

| Runtime | First turn | Continuation |
|---|---|---|
| claude | `--session-id <our uuid>` | `--resume <the same uuid>` |
| codex | `codex exec` | `codex exec resume <thread_id>` |
| agy | no id | `--conversation=<id>` |

Only claude's session id is ours. codex and agy mint their own, read back out of
the stream's first event.

Agent statuses: `provisioning` → `ready` → `running` ⇄ `idle`. `assign` against a
`provisioning` agent records the work without a workspace and returns at once;
`hire` attaches the workspace when the clone is ready.

### A turn has no time limit

**There is no wall-clock limit, no threshold on context and nothing that ends a
turn on its own.** A turn that has started stops only when somebody asks for it
to stop.

The office sets the vendors' own timeouts out of the way:

| Runtime | Mechanism | Value |
|---|---|---|
| agy | `--print-timeout` | `720h` |
| claude | `BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS`, `MCP_TOOL_TIMEOUT`, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` | one day |
| claude | `MCP_TIMEOUT`, `MCP_CONNECT_TIMEOUT_MS` | 5 minutes |
| codex | `mcp_servers.office.tool_timeout_sec` / `.startup_timeout_sec` | one day / 5 minutes |
| claude, codex | turn timeout on `-p` / `exec` | none |

The office's own timeouts stand in three places: the free quota and model polls
that run on the bus thread, every call against the owner's repository that does
not move objects (`git.OWNER_TIMEOUT`, 60 seconds), and the donor refresh
(`git.DONOR_TIMEOUT`, 300 seconds). Everything that moves objects runs uncapped.

A process that announces the end of its turn and then does not exit is closed
after `process.LINGER_GRACE_SECONDS` (15 seconds). Nothing waits for that
deadline: a healthy process exits in milliseconds and the reaper unblocks with
it.

### Visibility and stopping

- `quiet_for` (age of the last output) and `running_for` are shown in the works
  monitor beside every live work, and on the main page beside the director's stop
  button;
- stopping: the button on a work card in the monitor, the button beside the
  director's chat, and a manager's `agent(op=stop)` on an agent in its subtree.
  Any active work is failed as `killed` **before** the process is killed; with no
  work, only the turn ends. A work that has already reported is left alone;
- stderr lines carrying a vendor marker (`[agy]`, `codex:`, `claude:`,
  `warning:`) become events;
- the output tail is written for every turn, and the agent's next turn writes over
  it. Exit code 0 is not the same statement as "the turn happened" — agy's print
  timeout returns 0 with an empty response.

`agent(op=stop)` against yourself is refused: that call is the turn that would be
killed.

### Adapter interface

```python
runtime: str
takes_system_prompt: bool
build_command(ws, model, effort, office_url, session_id, resume=False) -> list[str]
encode_message(text) -> str
parse_line(line) -> Event | None
prepare_session(ws, agent) -> None
poll_quota(session_ids=()) -> list[QuotaSnapshot] | None
quota_reset(error) -> int | None
list_models() -> list[ModelInfo] | None
```

`quota_reset` reads the time of return out of the vendor's own quota refusal:
claude's "resets 12:50am (<zone>)", with or without a date; codex's "try again at
3:20 PM", with or without a date, in the machine's zone; agy's "Resets in 3h58m49s".
Without a date, the moment is the next time the clock shows it.

Optional methods: `child_env()` (claude), `compact_command(ws, model, effort,
office_url, session_id)` (claude), `poll_context(session_id)` (codex), `turn_requests(session_id, since)` (codex).

`Event` carries `kind`, `text`, `phase`, `session_id`, `context_used`,
`context_limit`, `context_before`, `error`, `request` and `totals`.
`phase` is `started` or `finished` on a tool event: a started call with no finished
twin is the diagnosis "the agent is sitting inside a command". `request` is one
model request's token counts, model and tools, keyed so that the events of one
request fold into one; `totals` is the process's own sum on `turn_end`.

`resume` is required and separate from `session_id`: an id alone cannot tell a
first turn from a continuation. `office_url` is the address of this agent's
office MCP server, the only one it has.

An adapter has no method for delivering into a live session.

One adapter instance per turn. Two of them hold per-turn caches — codex remembers
its rollout file and how far it has read, claude remembers which `tool_use` id
belongs to which tool — so an instance must never be shared between turns.

### The child process environment

Vendor session variables are stripped (`CLAUDE_CODE_*`, `CLAUDE_SESSION*`,
`CODEX_SESSION*`, `AGY_SESSION*`, `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`,
`CLAUDE_AGENT_SDK_VERSION`). Credentials and config paths are left alone. Added:
`OFFICE_AGENT`, `OFFICE_SESSION`, `OFFICE_MCP_URL`, `OFFICE_MARK`, and the
variables that close outbound push (section 8). A compaction gets the same
environment as a turn of its session (`process.turn_env`).

**Every git the office starts — an agent's process at any depth and the hub's own
calls — runs with `GIT_CEILING_DIRECTORIES` naming the owner's repository**, written
last. From a directory under `<root>` that is not inside a clone, git finds no
repository; in a clone it finds the clone, and in the owner's repository itself it
finds his.

`OFFICE_MARK` is an origin stamp of the form `<office id>:<agent>:<turn>`. An
environment is copied into every child at creation at any depth and survives the
death of every intermediary, so any process this turn started can be recognised
by the mark, orphans included. A run on a stage extends its agent's mark with
`:<handle>:`; a stage's preparation is marked `<office id>:office:<stage>:`
(section 12). The office id is a short hash of the data
directory: two offices on one machine do not touch each other's processes.

Processes are cleared in two passes and neither replaces the other. The first is
by tree (`taskkill /F /T` on Windows, `killpg` on POSIX): it needs no mark but
cannot reach a process whose parent has already died. The second walks by mark
(`office/marks.py`): over `<office id>:<agent>:` at the end of each turn and
after a compaction, over `<office id>:` at hub startup. The trailing colon is
required, or `architect` would cover `architect-aide`. The kill goes through a
handle opened before the mark was read, and death is confirmed by waiting rather
than assumed.

The second pass has its own blind spot: a process whose environment was built
fresh rather than inherited — a service, a scheduled task, a `Win32_Process.Create`
launch — carries no mark. That is what the first pass is for.

On Windows, an MSYS program started by another MSYS program carries no mark in
its Windows environment either. Once its parent has died, neither pass reaches it.

The sweep is silent when there is nothing to clear, writes a line to the office
log when something was cleared, and reaches the owner only for what survived the
kill or could not be touched. Every live turn is taken down at interpreter exit.

### Hub restart and crash

Only processes in mid-turn die. An agent outside a turn loses nothing: its state
is the vendor's session file plus its working tree.

At startup the hub:

1. sweeps by mark everything left from this office's previous life — before
   recovery, and before anything is started;
2. recovers the stages (section 12);
3. moves to `failed`, with reason `hub_restart`, every work whose assignee had a
   turn in progress when the hub died, and attaches the output tails spilled to
   disk;
4. closes the turn of every agent carrying a turn-start marker: drops the marker
   and tells the senders whose messages that turn consumed that they were not
   processed (section 4);
5. moves agents from `running` to `idle`;
6. when a work was interrupted or a lead's turn was killed, sends the director a
   message from `office` listing the interrupted works, and every other agent that
   section 4 would tell of an interrupted work or of a lead's killed turn one message
   listing those; when nothing was interrupted, sends nothing;
7. writes a line to the owner's journal; if the restart killed the director's
   turn the line is critical and raises the plate;
8. pauses the office (section 4, "The switch"): the messages of step 6 wait there
   until the office runs.

The first round of quota polling (section 10) runs to its end before the bus's first
tick; the web app does not wait for it.

Only a claude session is recoverable across a restart — only its id is ours.

---

## 4. The message bus

**One queue per participant. Three drains, never two for one message.**

| Drain | Cost | When |
|---|---|---|
| Hook | 0 | Inside a running turn, on the agent's own activity |
| Piggyback | 0 | Inside a running turn, on the result of an office MCP tool call |
| Turn | one turn | Agent outside a turn: a process started with the message in its prompt |

The first two work only for an agent whose turn is running. For an agent outside
a turn the message rides the prompt of the next turn — the third drain.

The hook and the piggyback take the queue with the same atomic rename
(`shared.claim_inbox`): one gets the bytes, the other gets nothing. Starting a
turn reads the database only.

Starting a turn and delivering a message are one action. An agent outside a turn
is asleep; waking it and handing it the message are indistinguishable.

### The queue and its carrier

The durable queue is the database: one watermark per agent,
`agents.last_seen_message_id`; everything past it is undelivered. There is no
second watermark over events — an agent receives messages and nothing else.

`<ws>/.office/inbox` is the cache the hook reads: one JSON line per message,
`{"to": name, "text": text, "after": watermark}`, appended. Whoever claims it takes the lines
addressed to itself and **appends the rest back**. The workspace's owner is
recorded in `<ws>/.office/owner`, rewritten at the start of every turn; the hook
delivers nothing unless the process's `OFFICE_AGENT` matches that file.

An agent with no workspace has no inbox and waits for a turn.

**Nothing is written to an inbox while no turn is running.** The inbox is a
forward cache for a turn already in progress; a hook fires only on the agent's
own activity, which an idle agent has none of. Until a turn runs, unread messages
stay in the database and ride the next turn's prompt.

Each line carries the watermark as it stood before that line was written. When a
turn ends, however it ends, the office takes back whatever the inbox still holds
for its agent and moves the watermark back to the lowest of those marks: what the
turn never received is unread again and rides the next turn.

### The owner's read mark

The owner has a mark too — `owner_reading`, one row per conversation, keyed by
channel plus peer. It is a different mark, not a second copy of the same one. An
agent's watermark decides what to DELIVER. Nothing is delivered to the owner — he
reads pages — so his mark decides only what to HIGHLIGHT. Moving it consumes
nothing; losing the whole table costs a counter and not one message.

Unread means: messages newer than the mark, addressed to the owner and not
written by him (a note to self counts nowhere); in the common chat, everything
except his own posts. Counters sit beside "Direct messages" and "Common chat".
The main page has no counter of its own: the director's chat is the same
direct-message thread behind a second door, and the panel there reports reading
exactly as the DM page does, so the director's thread is counted under "Direct
messages" like any other.

**The mark is moved by the browser, and no GET in this system writes anything.**
The office cannot tell one request from another: a request from the tab being
looked at, from a window behind three others, a HEAD probe and an address the
browser prefetched are the same request on this side. The browser can tell, and
reports it itself — `POST /messages/seen`, sent from `app.js`.

The report goes out only when all four conditions hold, because each is a
separate way of not seeing:

* the tab is visible;
* the window has focus;
* the panel is at its bottom edge — a reader who scrolled up to read something
  old did not see what arrived below;
* the machine was touched recently (ten minutes) — the only condition about the
  person rather than the window.

The third is the same calculation that decides whether to scroll a panel to a new
message or leave the reader where they stand (`app.js`, `atFoot`): one definition
for both questions. Scrolling does not count as input: the panel scrolls itself
to new messages, and activity the office generates for itself would keep the desk
occupied forever.

A report is sent on every occasion when one of the four could just have become
true, and on the one occasion when the bottom line changes under someone already
looking: panel swap, tab return, focus return, scroll to bottom, first input after
idle, page load. A repeat with the same id is not sent; a report rejected by the
server or lost to a broken connection is retried at most once every five seconds
per conversation. A 4xx is never retried.

The panel carries the id of the newest **displayed** line: on a page scrolled back
that is the end of that page, not the end of the conversation. The write is
monotonic and idempotent in a single statement.

The mark emits no events. The only thing that depends on it is the menu counters,
and they re-read themselves immediately after a successful report.

### Hooks by vendor

| Runtime | Events | Config | Injection point |
|---|---|---|---|
| claude | `PostToolUse` | `<ws>/.office/claude-settings.json`, passed with `--settings` | tool call |
| codex | `PostToolUse` | inline `-c hooks.PostToolUse=…` on the invocation | tool call |
| agy | `PreInvocation`, `PostInvocation` | `<ws>/.agents/hooks.json` | model call |

The hook's reply for claude and codex is `hookSpecificOutput.additionalContext`;
for agy it is `injectSteps[].ephemeralMessage`, plus
`terminationBehavior: "force_continue"` on `PostInvocation`. An empty queue
returns `{}` for codex and agy, and nothing at all for claude; for agy that `{}`
is what stops `force_continue` continuing the turn forever.

The injected text opens with one line saying that these are office messages that
arrived during the turn.

agy needs no tool call: its boundary is a model call, so an agy agent that only
ever thinks still gets its messages inside the turn. On a weak model a turn can
fit in a single model call, and then the message rides the next turn.

For claude and codex the hook is a process per tool call, so the command starts
with a shell test for the inbox file's existence. The hook config is rewritten at
the start of every turn: the command carries absolute paths to the interpreter,
to this copy of the office and to the workspace. The hook script imports the
standard library and `shared.py` beside it, and `shared.py` imports the standard
library only. agy's `hooks.json` is merged with
whatever is there; only the office's entry is replaced.

The hook's own stdin and stdout are set to UTF-8 explicitly. They are handed to
the process in the machine's code page, and everything crossing them is prose.

### Which drain is allowed

- **A direct message** buys a turn. If a turn is already running it arrives inside
  it for free; if not, a turn is started for it.
- **A common-chat post** never buys a turn of its own. If a turn is running it
  arrives inside it; if not, it rides the prompt of whatever turn happens next.

There is no third kind of delivery. Everything an agent receives is a message from
a participant, `office` included, or from a local service answering an expectation.

A third shape rides the common channel: **a quiet line**, a common-channel row that
names one recipient. It buys no turn, rides the next one, and is shown to nobody
else. The office uses it for what somebody must not go on believing but need not be
woken for.

The delivered text says which is which: `[common chat, from X]`, `[direct message
from X]`, `[the wake you set]` for a `remind` an agent set itself, and a quiet line
as its own `[office]`-prefixed body.

An agent never receives its own messages. The watermark moves past them anyway.

A single delivery always carries everything accumulated, except that of the
common chat it carries the newest 50 lines, after a line saying how many earlier
ones were left out and which `chat(before_id=…)` call can read them. Wakes coalesce over a
3-second window; the bus ticks once a second; the fuse allows at most 6 wakes per
agent in 300 seconds, with extras collapsing while the queue keeps growing. The
environment never interrupts a turn that has started. A quota hold (section 10)
also defers a turn; the messages wait. So does a paused office (below).

### The switch

The office is **running** or **paused**. The owner flips it with a button, or sets a
**one-shot timer** that flips it at a local time of day ("pause at 10:00", "run at
23:00"): the timer fires at the next occurrence of that time and makes the flip its
time field was labelled with when it was set. There is one timer at most; setting one
replaces the pending one and the owner can cancel it. There is no recurring schedule.
Flipping by hand does not clear a pending timer; a timer that fires flips the state and
is cleared.

The hub always starts paused. The state is in memory. A pending timer is a settings row
(`switch_timer`, section 15) and survives a restart; one whose moment passed while the
hub was down is dropped at startup. Every flip is a line in the owner's journal with its
cause: by hand, by the timer, or the hub's start.

While the office is paused:

- no turn starts, for any cause — a direct message, a reminder that came due, a
  keep-alive, a restart notice — except that a direct message **from the owner** starts
  its addressee's turn as it would when running, and that turn delivers whatever the
  agent has unread. A common-chat post by the owner, and a message from `office` or a
  service, start nothing;
- turns already running go on to their end; nothing is stopped or killed, and the hook
  and the piggyback deliver into them as ever;
- messages are still accepted and stored, and reminders and expectations still come due
  and become messages; they wait, and buy their turns once the office runs;
- the progress check and the director's "nothing in the office is moving" notice do not
  run. On switching to running the check's chains are dropped and its first check is a
  full interval away;
- quota polling, quota holds, the owner's interface, the compact button and the
  director's stop button work as ever.

### Expectations

`expect(about, within_seconds)` records an open expectation — a random token, the
agent, `about` and the due time — and returns `http://127.0.0.1:<port>/hooks/<token>`,
which the agent hands to a local service as the address to notify when its job
ends. The ceiling on `within_seconds` is `remind`'s.

`POST /hooks/<token>` with the JSON `{"from": "<service>", "text": "<message>"}`
closes the expectation and puts `[about: <about>]` and the text
into the agent's queue as a direct message from `hook:<from>`, in one transaction:
stored, delivered and buying a turn like any other. `from` matches
`[a-z0-9][a-z0-9-]{0,39}`. The answer is `202`; `404` for any token with no open
expectation — unknown, answered, past its due time, or its agent fired; `400` for a
body that is not a JSON object, a `from` that is not a service name, or a missing or
blank `text`. Each answer carries one sentence.
There is no authentication.

The bus tick closes an expectation whose due time has passed and tells its agent;
one that came due while the hub was down closes on the first tick after startup.
Firing an agent deletes its expectations. A closed expectation is deleted when its
agent's turn ends. Nothing goes from the office to a service.

### What is guaranteed

**A message either arrives, or its sender is told that it did not.** There are no
automatic retries.

A turn is ordered because a message that buys one appeared, not because a hook
failed. If the hook got there first the queue is empty, the prompt is empty and
no turn starts.

Writing to the inbox of a running turn advances the read mark in advance. What the
turn did not take is given back when it ends (above), so a turn's range never
holds a message the turn did not receive, and never misses one it did. At turn
start, the mark as it stood is saved in `agents.turn_start_message_id` — "what
this turn took". **On an abnormal end, every sender of a direct message in that
range gets a message from `office` saying it was not processed, with the reason
and, for quota, the time of return.** The brief of the agent's work is not among
them. A
message in the range counts as read and is not repeated; the sender repeats it if
they think fit — for quota, the notice says it waits for them until the time of return. On a clean end the marker is simply dropped. The marker lives in the
database, because the death of the hub is also an abnormal end.

The notices go out before the marker is dropped, and the marker is dropped either
way.

Common-chat posts get no such notices: they buy no turn and carry no guarantee.
`office` itself is never told, so a death cannot cascade into messages about
messages.

What the guarantee does not cover: the model's attention. Text placed in the
context is a delivered message.

### What the office says in its own name

**Text addressed to a participant is always delivered as a message from whoever
wrote it.** Assigning work sends the brief as the assigner's message; finishing
work sends the report to the assigner as the assignee's message. Tickets, PR
comments, opening and merging PRs, moving tasks, hiring, firing and reassigning
work send nothing; the state is readable through the tools in section 14.

**In its own name the office says nine things**, all of them messages from the
participant `office`:

| Occasion | Told to | Buys a turn |
|---|---|---|
| A work failed or was paused (unless whoever would be told caused it with `agent(op=stop)`) | the work's assigner; for a work its assignee assigned itself, the assignee's manager, and for the director the owner's journal | yes; the journal buys nothing |
| A lead's turn ended abnormally — for any reason but a stop | its manager | yes |
| Hub restart that interrupted a work or killed a lead's turn, with the list of interrupted works | the director, with all of them; every other agent the two rows above name for an interrupted work or a killed turn, with those | yes |
| Your message was not processed | the sender | yes |
| A deferred message has nowhere to go (the recipient was fired) | the sender | yes |
| Your expectation ran out of time: nothing came, and its address is closed | that agent | yes |
| A running agent has said nothing for the owner's silence threshold | its manager | yes |
| Nothing moves the open works an agent answers for | the agent; then each manager above it in turn; then the owner's journal (critical) | yes; the journal buys nothing |
| Nothing in the office moves | the director | yes |

**The progress check** runs on the bus thread once a minute, and not at all without
a director or while the office is paused. It works from the database and from which turns are running. It keeps
three things in memory: whether the owner has stopped the director's turn (set by
that stop, cleared by the director's next turn), where each stuck agent's chain
stands, and the last "nothing moves" notice. A hub restart forgets all three.

An agent **answers for** a work: its holder while it runs; its assigner once it has
reported (the holder, for a work it assigned itself); for a pause or a failure,
whoever section 4 tells of it, and the director for a work of its own — except one
of the director's own that the owner stopped, which nobody answers for.

Something **moves** an agent's works when it has a turn running, or the other side
of one of them does; a direct message it has not read; a deferred message it set
or will receive, or an expectation it holds open; a running work it assigned to
somebody else; or a direct message of its own that its addressee has not taken —
unread, or inside the addressee's running turn — and, for one to the owner, that
he has not written back after. The director has one more while the owner has
stopped it. An agent that answers for works and has none of these is **stuck**.

About a stuck agent the check says one thing at a time along a chain: level 0 is the
agent itself, 1 its manager, and so on to the director; above the director is a
critical line in the owner's journal. A level whose runtime and model are held for
quota (section 10) is passed over, and so is the director's while the owner has
stopped it. The bus keeps in memory, per stuck agent, its works and their statuses
as last found, the level told last, that message, and `MAX(messages.id)` when it
went; works found different drop the entry, and a hub restart starts every chain
again at level 0. The next word waits until the last one has been
taken — read, with no turn of its addressee running — and while that addressee has
a deferred message set since then still waiting. Taken, and the agent still stuck
on the same works, it goes one level up, or back to level 1 when the agent has had
a direct message since. After the journal line nothing more is said until the
agent has a direct message. The text is facts: each work's number, holder, status
and the start of its brief.

**Nothing in the office moves** when no turn runs, no direct message is unread, no
deferred message and no expectation is waiting, and no work is running. Then, if a
direct message other than one between the director and the owner has been sent
since the director last wrote to the owner or was last told this (kept in memory),
and the owner
has not stopped the director, the director is told, with the list of open works.
It is not sent in a pass that already wrote to the director about its own works.

The silence report is about a turn that has not ended and may never. It has no
power over the agent — no stop, no flag, nothing written but a message. The
threshold is the owner's, on the settings page: absent, empty or unparseable
means twenty minutes; zero or less means never. The director's own silence is not
reported — the director has no manager, and the owner already has `quiet_for` live
on the main page.

When the dead turn of a lead also failed or paused a work whose notice goes to that
same manager, the manager gets one message carrying both.

A message from a service that dies with a turn is given back, whole, to the agent
whose turn it was, as a quiet line. A message from `office` that dies with a turn
is lost silently; for a lead, the notice of its dead turn goes to its manager. The
director's own dead turn is on the owner's plate (section 15). The compensation
is observability, which is the rule the whole tool surface obeys: **no state may
be knowable only from a message that was received.**

Seven more things the office says buy nobody a turn:

| Occasion | Told to | Shape |
|---|---|---|
| The main branch has moved — work done outside the office has come in; take it in with `git merge origin/<branch>` | everyone | common chat |
| The project's rules have changed | every agent but the one who changed them | quiet line |
| Your standing instructions have been rewritten | the agent they belong to | quiet line |
| Your workspace has changed, with its workspace and sandbox paths | every agent whose workspace a `work_reassign` changed | quiet line |
| The work you did has been closed | the agent who did it, unless it closed the work itself | quiet line |
| You have been moved, with the name of your new manager | the moved agent, and nobody else | quiet line |
| A service's message died with your turn | the agent whose turn it was | quiet line |

The second, third and fourth exist because a system prompt is fixed when a session
is created, on every runtime: a rule, an instruction or a workspace path written
today is invisible to every session already open until that session is replaced.
The fifth and the sixth exist because a brief and a manager arrive in the snapshot a
session opened with, the brief also as a message, and neither changes when the work
ends or the agent is moved.

### What a wake carries

- **An existing session** → the messages that accumulated.
- **A new session** → the system prompt (for agy and codex as the first message)
  plus one full state snapshot.

**A system prompt is fixed when its session is created, on every runtime.** claude is
handed `--append-system-prompt-file` on every turn and applies it only at session
creation; agy and codex take theirs in the first message. There are three system
prompts, one per role: director, lead and executor. Each carries the agent's own
name, the owner's name,
the project's rules, the agent's own standing instructions — the director's under a
heading naming the owner, anyone else's under one saying they come from its
managers — and the workspace and sandbox paths; a
change to the rules, to the instructions or to the workspace reaches sessions
already open as a quiet line. In the director's prompt the block of rules ends with
a line saying that each rule's number is the `rule_id` its changes take. The team,
the works, quota and the model catalogue are not in it at all — they are
`roster()`'s, which is always current.

The snapshot is assembled from what the office already holds: who this agent is —
name, kind, title and manager; the team; this agent's own work (its id, status,
branch, assigner and brief, `failed` included) and, when the work has a task, that
task with each task above it; open PRs; the top-level tasks neither done nor cancelled, one
line each as `task(op=list)` prints them, in progress first and ideas last, at
most fifty, with the count of the rest;
a line saying that `task(op=list)` and `task(op=read)` read the board; open tickets
addressed to this participant — number, title and author, no bodies, with a note of
how to read one. A ticket wakes nobody, so
a fresh session would otherwise have no way to learn that one is waiting on it.

---

## 5. Sessions and context

**Context is managed by the vendor's own auto-compaction. The office has no
continuation mechanism of its own.** claude gets `--autocompact auto` on every
turn; codex has its own; agy has a budget in its agent definition.

**The environment has no thresholds.** It does not ask an agent to write anything,
does not end its turn and interrupts nothing as the window fills. There is no
handoff note.

### Measuring occupancy

**Occupancy is what the next request will carry:** the last round's prompt plus
the answer produced in it. Output counts from the moment it is produced — the next
request carries it as input. The rule is the same for all three runtimes and lives
in one place (`adapters/base.py`, `window_occupancy`); an adapter owns only the
mapping from its vendor's field names onto the two terms.

None of it is taken from a turn's final event.

| Runtime | Source | Prompt | Output |
|---|---|---|---|
| claude | `assistant.message.usage` | `input` + `cache_read` + `cache_creation` (disjoint fields) | `output_tokens` |
| agy | `step_update.usage` | `input_tokens` + `cache_read_tokens` | `output_tokens` |
| codex | `info.last_token_usage` from the session file, polled every 5 seconds | `total_tokens` — already summed by the vendor | |

agy's `thinking_tokens` and codex's reasoning tokens are not counted.

The denominator: claude reports it in `result.modelUsage[].contextWindow`, codex
in `info.model_context_window`, agy not at all. Until the CLI reports one, a table
by model is used (claude: haiku 200 000, sonnet, opus and fable 1 000 000; agy: gemini
1 048 576; codex has no entry). An unknown model gets the smallest window its
runtime offers.

Occupancy need not grow monotonically — the vendor may compact by itself and the
figure simply updates. A codex session file that cannot be read means "no number",
which `health()` says out loud; an estimate is never substituted.

### Post-mortem classification

A turn that dies at or above 85 % of the window is read as `context_overflow` and
the agent's `session_id` is cleared. This is a diagnosis: nothing consults the
figure while a turn is alive.

### When a session ends

| Case | Cause |
|---|---|
| Quota exhausted | the runtime stopped answering |
| Runtime change | session ids are not portable between vendors |
| Context overflow | the vendor hit its own wall |
| Stop, crash, hub death | the process is gone |
| `agent(op=new_session)` | asked for |

In every case a new session starts from nothing and gets the full snapshot.
Reasoning that lived only in the conversation is lost.

`agent(op=new_session)` is refused while a turn or a compaction is running on the
agent's session, the same condition compaction refuses on; the decision and the
dropping of the session id happen under the lock that starts turns.

### Manual compaction

`agent(op=compact)` is a manager's, on any agent in its subtree; the owner has a
button for the director.

**Compaction happens at once or not at all.** There is no deferral and no queue.
Only a free agent can be compacted: claude will not compact a session a turn is
running on, so the command is refused on a busy agent with a reason, and the
button is disabled and says why.

Only claude can do it. agy cannot at all; codex has it only through an app-server
the office does not use. For those two the answer is `agent(op=new_session)`. A refusal
is always explicit and always named: the runtime cannot, there is no session,
there is no workspace, a turn is running, a compaction is already running, or
claude declined (it does not produce a boundary on a short conversation). Both
callers — a manager's command and the owner's button — ask the same predicate.

The compaction runs with every flag and every variable a resumed turn of the same
session runs with, except the turn's stdin input format and `--autocompact`: the
prompt `/compact` is the argument.

Success returns `before`, `after` and `freed`, and writes a line to the owner's
journal. Every compaction, done or declined, leaves a usage row (section 13), and
its runtime's quota is polled once it ends.

A "this agent is compacting" flag prevents a turn from starting while it runs; it
is set and read under the same lock that starts turns.

### The cost of a model change

Changing model within a runtime keeps the session and drops the cache; changing
runtime ends the session. `core.update_agent_model` classifies this in one place,
and the settings page shows the classification before confirmation (`dry_run`). No
agent tool changes a model; the settings page offers it for the director only.

---

## 6. Tasks, works, workspaces

**A task** is a record of something to be done. It may have no assignee and no
works. Statuses: `idea` · `planned` · `needs_clarification` · `in_progress` ·
`paused` · `done` · `cancelled`. `task(op=move)` to `done` or `cancelled` takes a
result line. A cancelled task still blocks the tasks that depend on it.

A task may sit under a parent task, `tasks.parent_task_id`; a task without one is
top-level. `task(op=create)` takes an optional `parent`; `task(op=update)` takes
`parent` to move a task under another and `parent=0` to make it top-level. A parent
that is the task itself or lies under it is refused, and the refusal names the
chain; the check and the write share one transaction. Nothing else about parents is
enforced: a parent may be `done` while its children are open, and a parent's status
does not follow its children's.

`task(op=list)` and `task(op=read)` are open to everyone. `list` takes `parent`,
`status` — one status, or `all`; without it every status but `done` and `cancelled` — and `query`, a
regular expression matched case-insensitively against titles; an invalid expression
is refused. Without `parent` and `query` it lists the top-level tasks; with `query`
alone, the whole board; with `parent` alone, that task's children; with both,
everything under that task at any depth. One line per task: id, status, title, the
children's counts (open / done; a cancelled child is in neither) and `blocked by #…` naming each dependency that is
not done. The order is the board's: status in the order above, then position, then
id. The answer stops at two hundred lines or sixteen thousand characters, and its
last line counts the tasks left out and names the filters that narrow the list.
`read` gives one task: title, status, body, result, the chain of parents up to the
top, the children (one line each as in `list`, capped the same way), the
dependencies both ways, the works on it (id, assignee, assigner, status) and the
linked tickets (id, status, title).

Dependencies live in `task_dependencies`: `blocking_task_id` finishes before
`blocked_task_id`. There is no cycle detection; a task may not block itself. A
`planned` or `in_progress` task whose blocker is not `done` is reported by
`roster()` as its own list.

**A work** is activity on a task: brief, assignee, assigner, workspace, branch,
session. Statuses: `running` · `paused` · `failed` · `done`. A paused work goes back
to `running` when its holder's next turn starts; a failed one only by
`work_reassign`.

`assign(agent, brief, task_id, branch, role, complexity)` — the branch is required, the
task is not. `role` and `complexity` are required when the assignee is an executor and
refused when it is a lead or the director: only an executor's work is classified. The
role is one of the roles of the works price list (section 10), the distinct values of
its Role column, and the complexity one of the values of the setting `work_complexities`,
one value per line; a setting with no lines gives the defaults, `fully specified`,
`medium`, `high`; a price list with no rows gives `coding`, `review`, `consultation`,
`design`. A value outside the list is refused; the refusal names the value and lists the
values in effect. Both go to the work's row in `work_log` (section 13), NULL for a lead's
or the director's work. The caller is recorded as the assigner, `works.assigned_by_agent_id`; a manager
that assigns a work to itself is its own assigner. `work_reassign` records its
caller as the assigner in the same way, and the work's report and notices go to
that caller from then on. `work_reassign` sends nobody anything; the caller writes
to whoever has to act on it. The branch name travels to the assignee in the brief; the
assignee creates the branch itself.

Invariants checked explicitly:

- one active work per agent — in `assign` and in `work_reassign`, where the work
  being reassigned does not count against its own assignee;
- one workspace per agent — the partial unique index
  `workspaces(owner_agent_id) WHERE NOT NULL`.

### Branches are git's business, not the office's

There is no branch reservation. The office keeps no list of taken branches, does
not switch anyone's working copy and does not forbid writing to the default
branch. The assigner names a branch when assigning, having looked at what exists.

**The only truth about which branch a working copy is on is the copy itself.**
Publishing reads the branch from it and refuses only on a detached HEAD. A PR
takes the branch name from the publish that just happened, not from the database.

Two workspaces on one branch are not prevented by the office; git refuses at push
time, and the refusal is shown in words: the branch in the repository has moved
ahead, somebody else is writing to it.

Because a branch is required, a work with no code in it is given the default
branch. A wiki page, a review or a QA run produces nothing to commit, and a branch
of its own invites a commit. This is a convention stated in every prompt, not a
check in code: the office does not read the meaning of a brief.

Synchronising with a colleague is ordinary git: a working copy is a full clone of
the shared repository, and any agent can fetch another's published branch, look at
it or merge it.

### Workspace

```
Workspace { id, path, owner_agent }
```

The path is `<root>/ws/<workspace-id>/`. The id is assigned at creation and never
changes; the owner is a column. There is no branch in the record — that is git's.

A workspace outlives its owner: on firing it stays on disk with no owner, and
`hire(adopt_workspace_id=…)` adopts it; `hire` refuses a workspace that has an owner,
naming it. A sandbox belongs to its workspace (`<root>/scratch/<workspace-id>/`) and
goes wherever the workspace goes.

`work_reassign(workspace=inherit)` hands the work's workspace to the recipient
together with everything uncommitted, and the workspace the recipient held until
then goes to the agent that held the work's workspace: the two swap, and neither is
left without one. It is refused while the work's workspace belongs to an agent that
is neither nobody, the work's assignee nor the recipient and that has an active work,
and the refusal names that agent and points to `workspace=fresh`.
`work_reassign(workspace=fresh)` clones a new workspace on the default branch:
"start again" rather than "carry on". The previous agent's tree, uncommitted work
and all, stays with it, and the recipient's previous workspace becomes ownerless
rather than deleted. `work_reassign` refuses a recipient that is still provisioning.
A session resumes in a workspace other than the one it began in with its context
intact, on every runtime, so a change of workspace takes effect from each agent's
next turn and clears no session. **An agent's workspace never changes during its
turn**: `work_reassign` is refused while an agent whose workspace it would change
carries a turn-start marker (section 4), and whenever it would change the caller's
own. It is also refused while the work's assignee, when that is neither the
recipient nor the caller, carries the marker. The marker is set once a turn's
process exists, so a turn being started at the moment of the reassignment is not
seen by it. A manager reaches a work's code through git: its holder publishes the
branch and the manager fetches it into its own workspace; a work that lies in the
manager's own workspace is handed on with `workspace=fresh` once its branch is
published.

`work_reassign` to the work's current assignee with `inherit` moves nothing: only the
assigner and the work's status change, and the work is open again. That is how a
reported work is sent back to its assignee, and how a work is taken up again after a
dead turn.

Every agent whose workspace changes by `work_reassign` gets a quiet line naming its
workspace and sandbox paths (section 4).

### How an agent's work ends

| Cause | Consequence |
|---|---|
| `context_overflow` | `session_id` cleared, new session with a full snapshot |
| `quota_exhausted` | the work is moved to `paused` with `resume_after` |
| `tool_error` / `crash` | the output tail is stored on the work record |
| `killed` | stopped by a manager above the agent or by the owner |
| `hub_restart` | the turn died with the hub |

The reason is decided in this order: `killed` from the kill flag;
`quota_exhausted` by a regular expression over the vendor's error text;
`context_overflow` by window occupancy; `tool_error` when there is error text;
otherwise `crash`.

The output tail is a 200-line ring buffer in memory, mirrored to
`<root>/tails/<agent>.log` every 5 seconds and again at process exit. A snapshot of
it is stored on the work row when a work fails, and lives until that row is deleted.
`work(op=show, work=N)` prints it, with the rest of the work, to any caller.

`finish_work` is the assignee **reporting**: the row is marked `done`, the summary
goes to the assigner as a message from the assignee — to nobody when the assignee
is its own assigner — and nothing is written to the task. `close_work` is a manager
**accepting**: the row is deleted and the accepted one-line result — the closer's
own words — is appended to the task. It refuses a `failed` work by name.

Moving a task to `done` deletes every work still on it, so it is an act on those
works: it is refused unless each of them is within the caller's reach for
`work_close` — its assignee in the caller's subtree, or the caller its assigner. It
is also refused while a work on it has reported and waits to be closed, naming that
work. An executor can move to `done` only a task with no works.

`fail_work` keeps the row: it holds the tail, and its assigner and every manager
above its assignee see it in `roster()` until the work is reassigned, its task is
closed, or it is written off. `work_dismiss` (a manager's) deletes a failed work
together with its tail and refuses anything that is not failed. `pause_work` is not
a failure: branch, tree and brief are intact.

The supervisor's copy of the tail in `<root>/tails/<agent>.log` survives a
dismissal, until that agent's next turn.

---

## 7. Tickets

A channel with an addressee and a right to close.

```
Ticket        { id, title, body, author, addressee, kind, status,
                resolution, resolved_by, resolved_at, task_id?, created_at }
TicketComment { id, ticket_id, author, body, created_at }
```

`kind` is an open list (`question`, `bug`), informational.

**The authority rule is asymmetric:**

- a ticket addressed to the owner is resolved only by the owner. Agents have
  `ticket(op=resolve)`, and it refuses on a ticket addressed to the owner;
- a ticket addressed to an agent is resolved by that agent or by the owner.

**A ticket wakes nobody and sends nothing** — not on creation, not on a comment,
not on resolution. What is open and what is resolved is read with
`ticket(op=list|read)`; `list` gives the open tickets unless `status` names
`resolved` or `all`. Tickets addressed to the owner appear on his main page.
`task_id` is an optional link.

Resolved tickets are kept forever.

---

## 8. Git

The layer does five things and that is the whole list: **import a project; give an
agent a working copy; let an agent publish work; take published work into the main
line; deliver the main line to the owner.** What is not on that list is not in the
layer.

### Layout

**`<root>` is always `<owner's repository>/.office-data`.** The office lives inside
the project it works on; the delivery address is the parent of its data directory,
and so is computed rather than stored or entered. The hub takes it from `OFFICE_ROOT`
and does not start without it.

```
<owner's repository>/          the owner's working tree, the delivery target
<root> = .../.office-data/
<root>/repo/project.git        bare — the office's repository, the push target from workspaces
<root>/repo/seed/              an ordinary clone --recurse-submodules — the object donor
<root>/ws/<workspace-id>/      workspaces
<root>/scratch/<workspace-id>/ the agent's sandbox — outside every git tree; wiki/ holds the wiki's files
<root>/stages/<name>/          stages (section 12)
<root>/stages/<name>.prepare.log
<root>/stages/.<handle>.index  a snapshot's temporary index
<root>/stages/.trash/          stage trees waiting to be deleted
<root>/lfs/                    the shared LFS store
<root>/tails/<agent>.log       output tails
<root>/office.db               all state
<root>/office.log              the office's own log
```

`<root>/lfs` is the shared store: workspaces, the donor and stages read and write
their large files there, and new objects are written there first. `project.git`
keeps its own store: what the import and the owner's intakes brought, and what
every publish sent. `<root>/lfs` fills from it on demand.

The donor is an ordinary clone, not bare: a bare repository has no working tree,
`.git/modules/<name>` never appears, and a submodule's alternates have nothing to
point at. There is no build tree — merging happens in the object database.

The whole office sits in the owner's working tree, so `.office-data/` is appended
to `.git/info/exclude` in his repository, never to `.gitignore`.

### Common rules for calling git

One wrapper per module. On every call: `LC_ALL=C` (refusals are matched against
git's English text), `protocol.file.allow=always` (the donor, `project.git` and the
owner's repository are local paths), `GIT_CLONE_PROTECTION_ACTIVE=false` (git-lfs
installs a `post-checkout` hook and git refuses to run a hook that arrived with the
clone), `GIT_TERMINAL_PROMPT=0` and `GIT_CEILING_DIRECTORIES` (section 3).

Every path that becomes an address for git is written with forward slashes. A path
with mixed separators is read by git-lfs as an scp address.

### Project setup

`project_state(config)` → `absent` | `empty` | `ready`, from files, no subprocess.

Setup is one button and no path field: the project is the parent of `<root>`. The
form therefore states the situation rather than asking about it — "a repository
with N commits, it will be imported" or "there is no repository yet, one will be
created" — and shows the fields that case reads.

| What is at the path | What the office does | Fields |
| --- | --- | --- |
| a repository with commits | clones into `project.git`, drops the copy's remote, pushes the LFS objects the owner's repository holds | branch |
| a repository with no commits | an empty bare and a first commit | branch, README |
| no repository | `git init` in the owner's folder, then the same | branch, README |

The import sends every large file of the history that the owner's repository
holds, and leaves out those it holds only as pointers. Every large file of the main
branch's tip must arrive. If one does not, the import is undone and the refusal
names the files: the owner pulls their content into his clone and presses the
button again.

**The branch name is always read**, and it is the one thing the office cannot work
out for itself. A clone brings a foreign `HEAD`, so afterwards the repository's
`HEAD` is pointed at the named branch. If that branch does not exist it is created
at the commit the source's HEAD is on.

**The README is read only where the office makes the first commit**, and its text
is the owner's: the office writes no prose of its own into someone else's project.
The field is required, and the button is disabled while it or the branch field is
empty.

Setup refuses if `project.git` already exists, and refuses immediately if another
caller is inside it. The check and the work are held under one lock. A failure at
any step removes `project.git` and the donor. Setup then installs the
`pre-receive` hook and builds the donor.

The main branch's name becomes `project.git`'s `HEAD` and is stored nowhere else.
That same name is the delivery target.

### Workspace

```bash
git clone --recurse-submodules \
  --reference-if-able <root>/repo/seed/.git \
  -c submodule.alternateLocation=superproject \
  <root>/repo/project.git <root>/ws/<workspace-id>
```

`--reference-if-able` rather than `--reference`: a submodule whose object store the
donor does not have is then cloned in full instead of aborting the clone it belongs
to. It is also the only way to get `submodule.alternateErrorStrategy=info` — git
appends its own value for that key after everything passed with `-c`, and the last
one wins for a single-valued key.

`--dissociate` is never passed. The clone runs with `GIT_LFS_SKIP_SMUDGE=1`: assets
in a workspace are pointers, and an agent pulls the file it needs with
`git lfs pull -I <path>`.

Config: `submodule.recurse=true`, `fetch.recurseSubmodules=on-demand`,
`diff.submodule=log`, `status.submoduleSummary=true`, `lfs.storage=<root>/lfs`,
`protocol.file.allow=always`, the LFS filters in skip mode, and `user.name` /
`user.email` from the agent's name. `lfs.url` and `lfs.pushurl` are explicit
`file:///` URLs on `project.git` — both keys, because download reads the first and
upload the second; explicit, because `submodule sync` rewrites submodule addresses
from the superproject's.

`.office/` and `.agents/` are appended to `.git/info/exclude`.

`<root>/scratch/<workspace-id>/` is created alongside — the agent's sandbox, a
sibling of the clone rather than a directory inside it. Tools, scripts and anything
that is not a deliverable live there, out of reach of everything git does to the
workspace. The path reaches the prompt as `{sandbox}`.

Handing a workspace to another agent changes `user.name` and `user.email` and
nothing else.

### Submodules are read-only

The office does not host submodules, does not publish into them and does not
deliver work in them.

Outbound push is closed by **the agent process's environment**, not by repository
configuration: `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_n`/`VALUE_n` pairs sets
`url.<unreachable path>.pushInsteadOf` for `https://`, `http://`, `ssh://`,
`git://` and `git@`. A workspace holds a real clone of somebody else's repository,
the key to its server is already on the machine, and one ordinary `git push` inside
a submodule would go there in the owner's name.

The rule hangs on the process, so it does not care whether a submodule existed at
clone time or appeared later, whether it is nested or top-level, or how many
remotes it has. Fetching is untouched: `pushInsteadOf` affects sending only.

The office's own calls run from the hub process, which has none of these variables
and addresses only local paths. A submodule written as `name@server:path` with a
name other than `git` falls outside the rule; the prohibition on editing submodules
stands in the prompt, and this is the net under it.

`publish()` reports any submodule holding commits not reachable from its own
remote-tracking refs: work there was done and is going nowhere. The sentence goes to
whoever published.

### Donor

`gc.auto=0` and `gc.pruneExpire=never` in the donor and in all its `modules/*`:
workspaces borrow its objects.

It is refreshed with `fetch --recurse-submodules --prune`, time-limited, **when a
workspace is created** — that is, just before anything is asked of it. It is not
refreshed during a merge: it walks the network into every external submodule, and a
hang there would stall the recording of work already delivered.

### Publishing

`publish()` is how the office puts a branch into `project.git`: opening a PR, or
reporting a work with one, calls it. An agent's own `git push` from its workspace
puts a branch there too, for a colleague to fetch; the workspace's LFS store is the
shared one, so a pushed branch's large files are already where any other workspace
reads them. The branch name comes from the working copy; a detached HEAD is
refused with an explanation.

The branch's large files go first: `git lfs push origin <branch>` into
`project.git`, which sends the objects of the commits no remote-tracking ref of
`origin` reaches. The branch goes only if they all arrived; an object missing from
the shared store refuses the publish by name. Then one push with
`--recurse-submodules=no`, run with none of the workspace's hooks —
`core.hooksPath` points at a path that cannot be created. `project.git`'s own
`pre-receive` still runs on that push.

**Publishing to the main branch is rejected**, by `publish()` and separately by
`project.git`'s `pre-receive` hook: an agent has a real shell, and its workspace's
`origin` is `project.git` at a local path, so refusing inside `publish()` alone
would hold only until the first hand-typed command. The hook reads the branch name
from `HEAD` at push time, so it follows it. A push that moves nothing runs no hook,
so `publish()` refuses the main branch itself, and no pull request ever has it as
its source. Merge and intake move branches with `update-ref`,
which runs no hooks.

The only writer of the main branch is the merge, under a lock.

A non-fast-forward refusal is turned into a suggestion to fetch the branch or
publish under a name of one's own.

### Merge and delivery

A PR is an office entity in SQLite, always against the superproject.

`pr(op=create)` and `work(op=finish, pr=…)` publish the caller's branch first. When a
PR from that source branch into the same target is already open, they update that
PR — its title to the one given and, when one is given, its description — answer
"updated … published again", and open no second one: that is how a branch is
published again, after it has taken its target in.

**Only a branch that already contains the target is merged.** If it does not, the
answer is `behind` with one known action: bring the target into the branch in the
workspace it comes from, settle the conflict there, publish it again. The merging
manager does not do it in its own workspace.

The consequence is that the result's tree is exactly the source's tree, there is
nothing to reconcile, and **a conflict inside the office is impossible by
construction**. Conflicts exist only in a workspace — where the files, the history
and the person who wrote the code are.

An agent whose merge of the target conflicts asks the authors of the conflicting
changes and resolves once they answer. It writes to an author on the roster
directly; for an author no longer on it, to its own manager, and the director,
having none, decides itself; for an address outside the office, to the owner. This
is a line in the prompts, not a check in code.

The hub performs the merge in `project.git`, without a working copy, under a lock:

```
project.git HEAD names no live branch   → missing
take the owner's branch in (below)      → diverged → diverged
S = refs/heads/<source>, T = refs/heads/<target>
either branch absent                    → missing
S == T                                  → up_to_date
T not reachable from S:
    S reachable from T                  → up_to_date
    otherwise                           → behind
git commit-tree S^{tree} -p T -p S      → C
    deliver C to the owner              → refusal → blocked
git update-ref refs/heads/<target> C T  (checking the old value) → merged
```

A missing branch is the outcome `missing` rather than an exception: the intake
stands above it in the sequence, and a main branch that moved must be announced in
the common chat whatever the merge itself concluded.

The order follows from the commit existing as objects no branch knows about:
**build → give to the owner → record here → record in the database.** A refusal at
any step rolls nothing back, because nothing has been done before it, and "merged"
means "the owner has it".

A refusal at the last step is also `merged`, by the same rule: the owner has the
commit. It is a sentence, not an exception — it goes to whoever merged and says the
office's branch did not move. The next intake fast-forwards it onto the owner's
branch, which carries that commit.

`up_to_date` and `behind` are different positions, and the second reachability
question separates them: a branch lying wholly inside the target has nothing to
take in. Such a request is closed as answered, in its own words rather than in the
word "merged".

Merging takes a `delete_branch` decision and will not run without one. It removes
`refs/heads/<source>` on exactly the two outcomes that answer the request — after
the target has actually moved onto the merge commit, and on the `up_to_date` the
ancestor check reaches. On both, the work stays reachable from the surviving
branch and only the name goes. The old oid is passed to `update-ref`, so a delete
is refused if anything moved the branch. A delete that did not happen is a
sentence; what the merge did stands either way.

Only the main branch is delivered. A merge into any other branch is the office's
internal business and needs no intake.

### Taking the owner's work in

The link runs both ways. Without the reverse direction the office would work from
a snapshot taken on the day it was set up, and the owner's first commit to the
target branch would stop delivery for good: the push is not forced, and an office
commit built without his would be rejected as non-fast-forward.

One helper, three calls:

```
git fetch <owner's path> <branch>         → refs/remotes/owner/<branch>
git lfs fetch file:///<owner's git-dir>   refs/remotes/owner/<branch>
the office's branch is an ancestor of what arrived → fast-forward onto it
what arrived is an ancestor of the office's        → nothing new
neither                                            → diverged
```

`refs/remotes/owner/<branch>` is outside `refs/heads/`, so it is not a branch
anybody can clone, publish to or merge, and neither a workspace's clone nor the
donor's refresh fetches it. It is also what tells git-lfs which commits the owner's
repository already has. The large-file fetch takes the objects of the branch's tip.
The LFS fetch runs only where the branch is about to move, and its failure stops the move: pointers would otherwise be in every
workspace with the objects nowhere.

Intake runs in three places:

* **the "Take my changes" button on the main page** — when the owner decides it is
  time;
* **before a merge** — always; it is what stops his work being written over;
* **when a workspace is created** — so a new agent does not start from yesterday.

If the main branch moved, the office says so **in the common chat**. A silent
intake would leave working agents on the old state until the merge.

`diverged` means the owner rewrote his branch's history. The merge refuses and says
so; he sorts it out.

A new submodule arrives by this same route, with the superproject's commit, and
materialises in a workspace when the agent switches to it.

### The owner's repository

Delivery is one push from `project.git` into a path on this machine, always a path
and never a remote name:

```bash
git push --recurse-submodules=no --no-follow-tags <path> \
  <merge commit oid>:refs/heads/<main branch>
```

**Every call against the owner's repository runs with the user's and the machine's
git configuration switched off**: `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` point
at a file that does not exist. `url.<x>.insteadOf` and `pushInsteadOf` rewrite any
address string, a path on disk included, and a rule like `D:/` is enough to send a
local push out over the network. Switching the configuration off also removes
`push.followTags`, a foreign `core.hooksPath` and credential helpers. The owner
repository's own local configuration is untouched — the receiving side reads it.

No call on this path can ask for a password: `GIT_TERMINAL_PROMPT=0`, and askpass
points nowhere.

The cap is a minute for everything except object transfer.

**LFS objects go before the branch, or the branch does not go at all.** From
`project.git`, `git lfs push owner refs/heads/<source>`, with the remote `owner`
given for that one call as the owner's path and its `file:///` git directory — the
merge commit and the source branch share one tree. It sends the objects of the
commits `refs/remotes/owner/<branch>` does not reach; the intake before every merge
has just moved that ref. A non-zero code is a refusal: a pointer to an object the owner does not have would send his own filter
out to the network for it.

`receive.denyCurrentBranch=updateInstead` in the owner's repository is what makes a
push into the checked-out branch update the files as well. Project setup writes it,
together with `git lfs install --local` — a local config cannot be hidden by the
nulled global one, and without the filters a delivery would write pointers into the
files. Where git-lfs is not installed that command fails and nothing is written,
which is a repository that takes delivery of everything except the contents of its
large files. The **"Set up delivery"** button on the settings page writes both again.

The delivery address is shown on that page and is not editable.

Outside its own directory the office does exactly four things: the delivery, one
`receive.denyCurrentBranch` line, `git lfs install --local`, and one line in
`.git/info/exclude`. To those is added `git init` when there is no repository at
that path yet.

**`project.git`'s `HEAD` is the only place recording which branch is the main one,
and a push does not move it** — git has no refspec for that.

A delivery refusal is one sentence for the team to relay to the owner:

| What git said | What the team sees |
| --- | --- |
| `refusing to update checked out branch` | delivery is not set up — the button on the settings page |
| `Working directory has unstaged changes` | the owner has uncommitted edits |
| `has staged changes` | the owner has edits in the index |
| `Could not update working tree to new HEAD` | an incoming file is already there untracked |
| non-fast-forward | the owner's branch has moved ahead |
| anything else | git's text as it stands |

A refusal stops the merge: the PR stays open, the office's branch has not moved,
and the team repeats the merge when the owner says he has cleared the way.
Untracked files do not obstruct an intake.

`behind`, `blocked`, `diverged` and `missing` all leave the PR open and hand the
sentence back to whoever merged. In front of a `blocked` or `diverged` sentence the
tool puts "This is <owner>'s to clear in his own repository — send it to him as it
stands and change nothing there." `merged` and `up_to_date` both delete the row: the
request has been answered, and the merge commit carries the PR's description and a
digest of its comments into the history.

### What the layer does not do

It does not host submodules, publish into them or deliver work in them. It keeps no
build tree; stages (section 12) are outside this layer. It does not check gitlinks and does not extract commits from dead
workspaces. It does not look for the owner's hooks and filters and keeps no
snapshot of his environment. It knows no revert levels, no delivery switch and no
"send now" button. It does not resolve conflicts and never meets one. It does not
reserve branch names — it rejects writes to the one branch it delivers. It does not
search submodules for a live push address.

---

## 9. Knowledge base

**Wiki** — categories, markdown, a version per page. **Rules** — a flat list of
statements, each with a short title and a number. The owner edits both from the
Knowledge page; agents reach them through `note`.

The source is SQLite. **Every agent reads and edits the wiki as files**: page
`a/b` is `<sandbox>/wiki/a/b.md` in each workspace's sandbox, holding the page's
body. `wiki_copies` keeps, per workspace and page, the version and the body the
office last wrote into that file. A file whose text differs from that body and from
the page holds edits its agent has not published, and the office leaves it as it
is.

The files are brought up to the pages at the start of every turn of the sandbox's
agent, before its process starts, and one page's file when the agent reads, undoes
or deletes that page: a missing file, or one that is what the office last wrote,
gets the current page; for a page that is gone the copy is forgotten and the file
removed unless it holds edits, which leaves it a draft of a new page. An agent drops its own edits by deleting the file and reading the page. The
owner's edits on the Knowledge page reach the files the same way.

`note(op=write, kind=wiki, path)` publishes the caller's file of the page. A new
page needs `category` and `title`. For an existing page they rename or re-file it.
Every new page, the owner's included, has a path of segments of lowercase letters,
digits, `_` and `-`, each starting with a letter or a digit, none a Windows device
name, joined by `/` (`core.note_wiki`, and the `pattern` of the owner's form). A
page body is stored with LF line ends.
When the page has changed since the caller's copy, the file is merged with it by
`git merge-file`, the copy's body as the base; a clean merge is published and
written into the file, and a merge with overlapping changes publishes nothing,
writes the file with those places marked and says how many there are. A file that
still holds a marker is refused. Two publishes of one page at once are settled by
the version check of `core.note_wiki`: the second is refused and is written again.

`note(op=list, kind=wiki)` returns the index — path, category, title, version and
comment count per page, no bodies. `note(op=read)` returns one page's version, who wrote it
last, the state of the caller's file and every comment on it; no body.
`note(op=delete)` is refused unless the caller's copy is at the page's version.
There is no search: the files are searched with the agents' own tools.

`note(op=undo)` returns a page body to its previous state — one step back, which is
the whole of the wiki's history (`wiki.prev_body`). The undo is itself a write, so
it can be undone.

`note(op=comment)` comments on a page. A page is rewritten underneath its comments,
so each comment records the version it was written against, and both readers print
it. A comment wakes nobody.

A page's `path` is also the path of its file under `wiki/`.

**Rules are the director's to write and everyone's to read.** `note(kind=rule)` has
`list` for everyone, and `create`, `update` and `delete`, which it refuses for any
role but the director; `title` is required on create and optional on update.
`list` prints every rule as it stands now. All of them also go verbatim into every
system prompt as they stood when the session was created, which means a change
takes effect in the prompt from an agent's next session. The office always tells
the sessions already open with a quiet line (section 4); an urgent change the
director also sends as a direct message itself.

Both prints carry each rule's number, which is the address for changing or
deleting it: `- [id] Title: text`. A rule with no title prints as its own text.

---

## 10. Quota and the model catalogue

Quota is a property of a vendor account, not of a session, a work or an agent.

```
QuotaBucket { runtime, label, remaining_fraction, reset_time }
```

The key is the pair (runtime, `label`). The office names the **window** and nothing
else: `5h` or `week`, taken from the vendor's own `window_minutes` where there is
one and from unambiguous words in its label where there is not. Anything
unrecognised keeps the vendor's text untouched. Whatever else the vendor said stays
as a parenthesised qualifier where it distinguishes two buckets of one runtime —
`week (Fable)` against `week (all models)`.

Two numbers are normalised:

- `remaining_fraction` is always what is left. agy reports the remainder, claude and
  codex report what is spent, and those two are subtracted from one;
- `reset_time` is always epoch seconds, UTC. Unparseable is `NULL`; no local zone is
  substituted. claude's `/usage` names no year: the one that puts the moment
  nearest to now is taken;
- a bucket whose reset time has passed is stored as fully remaining, with no
  reset time — when a reading is saved, and in every polling round for buckets
  no reading has refreshed. The history keeps the vendor's figure.

Reading it is free for all three, with no turn, at any moment:

| Runtime | Source |
|---|---|
| claude | `claude -p /usage --output-format json`, lines of the form `<label>: N% used` |
| codex | `rate_limits` from the newest session file of a session the office itself started |
| agy | `agy --output-format json --print /usage`, `command.data.groups[].buckets[]` |

Polling is one round at hub startup, run by the bus loop before its first tick, then
every 60 seconds on a thread of its own, and one round of a runtime as soon as a turn or
a compaction on it ends. The web app does not wait for the first round. A successful
poll of a runtime deletes the buckets its answer did not mention. A failed poll
deletes and changes nothing. A runtime that has never answered has no row, and
`health()` says so.

**The history** (`quota_polls`) keeps every reading that differs from the one
before it for its bucket, as the poll gave it.

Only Gemini models are used from agy; the "Claude and GPT" group is dropped
entirely — the one place where part of a vendor's answer is not shown.

The buckets carry no derived figure; the wallets below are derived from them.

A quota change publishes the SSE event `quota` and the page redraws itself. Quota
is not substituted into any system prompt; the director and the leads read it
through `roster()`.

### Wallets

Each runtime's subscription is a **wallet** in its own currency — CL for claude, CD
for codex, GM for agy — where one unit is one dollar at the vendor's API list prices.
The weekly reset is **payday**; the 5-hour window limits what may be spent now.

The owner sets, on the settings page, per runtime a total weekly limit (the worth of the
weekly bucket, in units) and a 5h window (the worth of the `5h` bucket; empty when the
subscription has none), and a reserve: a percentage of the week the team leaves
untouched, 0 by default. The settings are `wallet_<runtime>_week`,
`wallet_<runtime>_window` and `wallet_<runtime>_reserve`. A runtime with no weekly limit, or with no weekly bucket
stored, shows no wallet. A runtime with no 5h window set, or with no `5h` bucket stored,
shows its weekly part only. The weekly bucket is `week (all models)` for claude and `week`
for codex and agy.

`roster()` gives a manager, in its quota part, per wallet:

- what is **left** until payday, out of the weekly share. The share is the weekly limit
  times one less the reserve; what is left is the weekly bucket's remaining fraction times
  the weekly limit, less the reserve times the weekly limit, not below zero;
- where there is a 5h window, what is **left in the current window** out of the 5h
  window: the `5h` bucket's remaining fraction times the 5h window, or what is left of the
  week if that is less; and the time the window resets;
- the payday.

The settings page shows, for each runtime whose CLI this machine has, its total weekly limit, 5h window and reserve; the reserve is a control: a thin bar that is the whole
weekly bucket, its filled part at the right being the remaining fraction and draining to
the left, with a slider (0 to 100) over it. The reserve is 100 less the slider's value, so
a slider at the right edge is a reserve of 0. A label beside it gives the reserve in percent
and in units of the runtime's currency, and follows the slider and the weekly limit field. A
runtime with no weekly reading stored has a bar with no fill and a working slider. The
reserve is saved with the rest of the form.

Right after the quota part, `roster()` gives a manager the **price list**: a header naming
its figures typical / with margin; the lines of the setting `price_list_note`, indented,
when it has any; "Works you assign:" and the rows of the table `price_works`, indented
(`(empty)` when it has none), each row `role | complexity | size | model | price`; "What a
lead spends on its own:" and the rows of `price_own` the same way, each row `what | size |
price`; and the line "Work that is not in the price list is not assigned, unless the list
is empty or <owner> has instructed otherwise." An executor does not see it.

The office reads one thing in these rows: the **roles in effect** are the distinct
`role` values of `price_works`, in order of first position. With no row, the defaults
apply. Nothing is decided from `price_own`.

The two price lists are stored as the tables `price_works` (position, role, complexity,
size, model, price) and `price_own` (position, what, size, price), every column but
`position` text. `position` is the order the owner set; `price` is the owner's own text
("0.1 / 0.3 CD", "unknown yet"). The setting `price_list_note` holds the free text shown
above the price list, and `work_complexities` stays a setting.

The settings page edits the two price lists as tables and the work complexities as a
list. Above the works table sits a text box, "Note shown above the price list", for
`price_list_note`. A table has a header row, one text input per cell, and per row buttons
that move it up, move it down and delete it, with an "Add row" button under it; the list
has the same without the header. A row's first cell is required. Every row posts its cells as repeated fields in document
order: `works_role`, `works_complexity`, `works_size`, `works_model` and `works_price` for
the works table; `own_what`, `own_size` and `own_price` for the own table; `complexity`
for the list. Saving zips each table's fields into rows, trims every cell, and replaces
both tables in one transaction; the note is written as posted, and the complexities as the
trimmed values, one per line. The wallet fields are written only for the runtimes the page
shows. A
table scrolls inside its own box.

Right after the price list, one line names the roles in effect and one the complexities in
effect, as `assign` takes them for an executor.

There is no notion of paid overflow: the vendors' overdraft fields are not read. An
exhausted window arrives as a turn error and is classified as `quota_exhausted`.
Its time of return is the one the vendor's refusal names (`quota_reset`), worked
out once per death. The work is paused, a message from `office`
goes to whoever section 4 names for it, and the owner gets a plate (if it was the
director's turn) or a journal line, naming the hold when there is one. The work
resumes when its holder's next turn starts: after the time of return, a message to
the agent is all it takes.

**The hold.** Until the time of return no turn starts on that runtime and model:
messages to its agents stay unread and ride the first turn after it. The hold is
written before the dead turn is let go, is kept in the database across a hub
restart, and ends at the time of return; a refusal that names no time holds
nothing. A compaction is refused while a hold is in force.

**The model catalogue** is read the same way: free, with no turn, once an hour on
its own thread, and stored as a snapshot like quota — `claude -p /model` (plus the
effort levels parsed out of `claude --help`), codex's `$CODEX_HOME/models_cache.json`,
which the CLI maintains itself and which costs no process at all, and `agy models`.
A catalogue is never guessed: every adapter returns `None` on any failure rather than
a hardcoded list, and `None` ("could not ask") and `[]` ("asked, nothing") stay
distinct all the way up. `efforts` is `None` where the vendor publishes none, which is
not the same as "this model takes no effort".

A model and an effort are validated against the catalogue at hire time. No
catalogue means no validation: a vendor being unreachable must not stop hiring. A
profile is not validated when saved — a catalogue is a fact about today and a
profile lives longer. The catalogue is shown under the hire and settings model
fields, so what is offered and what is accepted are one text.

The ceiling on parallelism is quota, not hardware. This is a line in the director's
and the lead's prompts, not a limit in code.

---

## 11. Supervised commands

`run` executes a command in the agent's workspace under a deadline and **always
comes back**: either the command finished and the agent gets its exit code and
output, or the deadline passed and the agent gets the output so far, how long ago
the last byte arrived, and a handle.

Four rules hold it together:

* **The deadline never kills.** The process goes on running and the handle keeps
  naming it. What happens next is the agent's decision: wait on the handle again,
  or stop it.
* **The end of an agent's turn kills everything that agent started.**
* **Kill by process tree, never by pid.**
* **Nothing survives the hub that started it.** The tree kill takes down what hangs
  off a live parent chain; what does not carries the office's mark and is swept by
  it, at the end of the turn and again at the next hub startup.

`DEADLINE_SECONDS` is 150 — both the default and the ceiling, because a vendor
runtime destroys an office tool call that takes more than three minutes. An agent may
have three commands alive at once (`MAX_RUNNING_PER_AGENT`); a fourth is refused by
name, with the handles of the three. The result carries the tail of
the output (`RESULT_TAIL_CHARS`, 32 000) and names the file holding the whole run,
`<ws>/.office/runs/<handle>.log`, which the agent can grep; a file that was itself
cut short says so in its last line.

This is deliberately not a general shell. It is for tests and trial runs — things
that can hang. Reading and editing files and ordinary quick commands stay on the
agent's own tools.

A command runs with the environment section 3 gives an agent's processes, without
the turn's identity: it carries the agent's mark, the push-closing variables and
`GIT_CEILING_DIRECTORIES`, and no `OFFICE_AGENT`, `OFFICE_SESSION` or
`OFFICE_MCP_URL`.

`run(op=start, stage=<name>)` runs the command on a stage instead of in the
workspace (section 12).

Nothing here runs on the event loop or on the bus thread. Spawning, reading and
killing happen on this module's own threads, and the MCP handler waits for the
deadline with an awaitable that holds no thread.

---

## 12. Stages

A **stage** is a working tree the office owns and agents use one at a time. It holds
what is expensive to build and cannot be used by two processes at once. An agent
runs a command on a stage with its own work applied to it, and gets back what the
command changed.

```
Stage { name, prepare, state, reason }
```

`state` is `preparing` · `ready` · `broken`. `reason` is set only for `broken`: the
output of the step that failed and what to do next, or the sentence that the hub
restarted while the stage was being prepared.

The office knows nothing about what runs on a stage. What a project's stages are
for, and which command does what on them, is written in its rules and wiki.

### Lifecycle

The director creates, resets and deletes stages with `stage(op=create|reset|delete)`.
No other participant has these operations; the owner sees stages and does not
manage them.

`create(name, prepare)`: the name is unique and matches `[a-z0-9][a-z0-9-]{0,39}`;
`prepare` is one shell command line and may be empty. The call writes the row as
`preparing` and returns at once, and the preparation runs on a thread of its own.

**A preparation** is, in order: the clone (on `create` only), the configuration,
the switch to the main branch's tip, the fill, and `prepare`. Every line git and
`prepare` print, progress included, counts as output for the preparation's
`quiet_for`. Everything it starts carries the mark `<office id>:office:<stage>:`,
and what is left running under that mark is swept when the preparation ends,
however it ended.

`prepare` runs in the stage's tree through the shell `run` uses, with the variables
that close outbound push (section 8). Exit code 0 makes the stage `ready`; anything
else makes it `broken`, with the exit code and the last 200 lines of output. The
full output is `<root>/stages/<name>.prepare.log`.

A clone that fails, or a tree that is not a usable clone, makes the stage `broken`
with git's output and the sentence that the stage has to be deleted and created
again. A configuration, switch or fill that fails makes it `broken` with git's
output and the sentence that it has to be reset again.

`reset` runs a preparation in the tree the stage already has.

`delete` moves the tree aside and removes the row, the stage's refs in
`project.git` and its preparation log. Moving aside is a rename to
`<root>/stages/.trash/<name>-<n>`, followed by deletion on a background thread. A
rename that fails is refused with the operating system's sentence; a delete that was
pending, or that had killed a preparation, leaves the stage `broken` with that
sentence. A stage that has to start cold is deleted and created again.

On a stage where no run is running, `reset` and `delete` take effect at once, and a
preparation in progress is killed first; a preparation that has not ended ten
seconds after its kill refuses the operation, with a sentence that sends the
director to the owner. On a stage where a run is running they wait for that run to
end: until then the stage refuses new runs, naming the pending operation, and the
runs waiting in its queue are answered at once.

### The tree

The clone is a workspace's (section 8), pointers and all. **The configuration** is
a workspace's too, with the LFS filters in their ordinary mode, in the superproject
and in every submodule. **The fill** runs `git lfs pull` in the superproject and in
every submodule: large files in a stage are content, not pointers. Commits the
stage writes are signed `office`, passed per command.

The stage's large files are written to `<root>/lfs` like a workspace's: what a run
changes under LFS is in the shared store once C is written.

**The switch** puts the tree at a commit: it removes every lock file git left in the
superproject and its submodules, then `fetch` from `project.git`,
`checkout --force`, `submodule update --init --recursive --force`, and `clean -fd`
in the superproject and in every submodule. Ignored files are never removed.

**Every git call the office makes for a stage — in the stage, in an agent's
repository and in `project.git` — runs with hooks switched off**: `core.hooksPath`
points at a path that cannot be created.

The queue serialises the office's own runs and nothing else.

### Running on a stage

`run(op=start, command, stage=<name>)`. Everything section 11 says about `run`
holds: the deadline never kills, the end of the agent's turn stops the run and takes
it out of the queue, the whole process tree is killed, and the log is
`<ws>/.office/runs/<handle>.log`. A stage run counts towards `MAX_RUNNING_PER_AGENT`
from the moment it is enrolled. The deadline counts from the call, the snapshot
included.

Every process of a stage run — the office's git calls for it and the command alike —
carries the agent's mark extended by `:<handle>:`, which the sweep over the agent
still covers.

A workspace has at most one run per stage, running or waiting. A second is refused
with the handle of the first.

At the call:

1. **State.** A `preparing` stage refuses the call with how long the preparation has
   run and how long ago it last printed; a `broken` one with its reason; a stage
   with a pending reset or delete with that operation.
2. **Snapshot.** The agent's working tree as it stands — tracked changes and
   untracked files that are not ignored — becomes commit S whose parent is the
   agent's HEAD. It is built on a temporary index that starts as a copy of the
   agent's own. It writes objects into the agent's repository and new large files
   into `<root>/lfs`, and touches no index, HEAD or file of the agent's. S is
   force-pushed to `project.git` as `refs/office/stage/<stage>/<workspace-id>`,
   outside `refs/heads/`: nothing clones, publishes or merges it.
3. **Queue.** The state is checked again, and the request joins the stage's queue,
   first come first served.

When the request reaches the head of the queue, the run is these steps, and stopping
it or ending its turn kills whichever step is in progress. A run that has not ended
ten seconds after its kill is given up on: it stays reported as running and keeps
the stage.

4. **Switch** to S. git's output counts for the run's `quiet_for`. A switch that
   fails fails the run; the stage stays `ready`.
5. **Artifacts.** `<root>/scratch/<workspace-id>/stage/<stage>/` is emptied and
   passed to the command as `OFFICE_ARTIFACTS`.
6. **Command.** It runs in the stage's tree, through the shell `run` uses, with a
   `run` command's environment (section 11), the run's mark and `OFFICE_ARTIFACTS`.
7. **Clearing.** However the run ended — the command exited, it was stopped, the
   turn ended — the office sweeps by the run's mark and waits until those processes
   are dead, then removes every lock file git left in the superproject and in its
   submodules.
8. **Changes.** Only after a command that exited on its own, whatever its exit
   code: `add -A` and `write-tree` on the stage's own index. A tree that differs from
   S's becomes commit C with parent S, force-pushed to the same ref.
9. A pending reset or delete takes effect; otherwise the next request is taken.

The result is `run`'s, plus:

- while waiting: how many requests are ahead, and who has had the stage for how long;
- S's oid;
- a step before the command that failed, with its output;
- after the command: C's oid; the number of changed files, the first 20 paths
  outside LFS and the first 20 under LFS; and the commands that bring C into the
  working copy — fetch the ref; write the diff from S to C, LFS paths excluded, to
  `<root>/scratch/<workspace-id>/stage/<stage>.patch`, and the LFS paths the same
  diff touches to `<root>/scratch/<workspace-id>/stage/<stage>.lfs-paths`, both
  before anything is applied; `git apply` the patch without whitespace warnings, or
  `git add` the files it touches and `git apply --3way` it where plain `apply`
  refuses; `restore --source=C` the paths in the list, read as literal paths, with
  the ordinary LFS filter. Or that the command changed nothing;
- the artifacts folder and how many files it holds, once step 5 has emptied it.

The ref is overwritten by that workspace's next run on the same stage and removed
with the stage.

### Visibility

`roster()` gives everyone each stage's name and state, who is running on it and for
how long, and who is waiting. The director additionally gets `prepare`, `reason`, a
pending operation, and the free space on the drive holding `<root>`. The Team page
lists the stages (section 15).

### Hub restart

Runs die with their turns, and a pending reset or delete is forgotten. A stage in
`preparing` becomes `broken`; `reset` resumes it in the tree it has. After the
startup sweep, every lock file git left in a stage is removed, and so are the
snapshots' temporary indexes. Everything under `.trash` is deleted on a background
thread.

### What a stage does not do

It does not gate a merge, does not run by itself, and is reached by nothing but
`run`. It keeps no history of runs. It has no approval step.

---

## 13. Storage

| Stored | Why |
|---|---|
| Agents: role, manager, title, standing instructions, composition, runtime/model/effort, workspace path | Not recoverable |
| Tasks, each with its parent, and their dependencies | This is the plan |
| Rules and wiki | What the database exists for |
| Wiki copies: per workspace and page, the version and body last written into its file | The base a publish is merged from |
| Open PRs and their comments | git holds no metadata |
| Works that are still on the books: brief, assignee, assigner, session id | Live state |
| Tickets and comments, resolved ones included | The wording of a product decision |
| **Messages — all of them, forever** | `DELETE FROM messages` exists nowhere |
| The current quota snapshot | One row per bucket |
| Quota holds: runtime, model, the time of return | A hold outlives a hub restart |
| Quota history: every reading that differs from the one before it for its bucket | Nothing in the office reads it; the owner does, with SQL |
| The work log: one row per work, kept after the work is closed. Numbers and names, no text | The owner reads it with SQL |
| Usage: one row per vendor process — a turn, a compaction or a keep-alive — with the agent, its work and task, who woke it, its outcome and its token totals; one row per model request and per compaction inside it. Numbers and names, no text. A turn's totals are the vendor's own, except agy's, which are the sums of its requests; a claude request's output count is NULL, the turn's total holds it | The keep-alive reads it; the owner reads the rest with SQL |
| Each runtime's model catalogue | A snapshot, overwritten hourly |
| Owner notices | The record of what happened while he was not looking |
| How far the owner has read each conversation | Not recoverable from anywhere else |
| The two price lists: rows in the order the owner set, cells as the owner's text | The owner's own words |
| Settings | |
| Stages: name, preparation command, state, reason | Not recoverable |
| Open expectations: token, agent, what is awaited, when opened, due time | A service answers across a hub restart |

| Not stored | What replaces it |
|---|---|
| Closed works | The result line on the task |
| Merged and closed PRs | The merge commit in git |
| Session ids after a work is closed | There is nothing to continue |
| Full stdout logs | A ring buffer in memory; only the tail on disk |
| Wiki edit history beyond one step | The version and the version check on write |
| A stage's queue, a pending reset or delete, the history of stage runs | Nothing; runs die with the hub |
| A closed expectation, once its agent's turn has ended | The message it produced |

The `events` table is a record of a mutation and the trigger to redraw a page
region. Nothing reads it back and no participant has a reading position in it. The
prune keeps the newest thousand rows and deletes the rest in the same transaction
as the insert, so the table does not grow with hub uptime.

Notices are kept forever. They are the owner's own journal, separate from delivery
to agents.

History is paginated by cursor over `id`, not over `created_at`: the timestamp is
in milliseconds and repeats inside a burst. For the same reason, lists that are not
paginated are ordered by `id` too. The one exception is resolved tickets, sorted by
resolution time with `id` as the second key. Indexes: `(channel, id)`,
`(recipient, id)`, `(sender, id)`; the last also carries the DM thread list as one
grouped query.

### The work log

`work_log` holds one row per work, keyed by `work_id`, the id of the row in `works`.
Nothing deletes a row and no foreign key reaches it. It holds names and numbers only: no
brief, no summary. The owner reads it with SQL, together with `usage_turns` and
`usage_requests`, which carry `work_id` and `task_id`.

Columns: `work_id`, `task_id`, `agent`, `runtime` and `model` (the work's holder),
`assigned_by`, `branch`, `role`, `complexity`, `assigned_at`, `reports`, `pr_id`,
`merge_commit`, `files_changed`, `lines_changed`, `ended_at`, `ended_as`.

Each column is written by the function that changes the fact:

- `assign_work` inserts the row with everything known at that moment; `assigned_by` is
  the first assigner;
- `finish_work` adds one to `reports`;
- `reassign_work` sets `agent`, `runtime` and `model` to the new holder's and clears
  `ended_at` and `ended_as`;
- `merge_pr`, on a merge, finds the newest logged work, closed or not, whose `branch` is
  the PR's source branch and whose holder is the PR's author, and sets its `pr_id` and `merge_commit`. It adds
  the merge's size to `files_changed` and `lines_changed`: the files changed and the
  insertions plus deletions of the merge commit against its first parent, from
  `git diff --numstat`. A binary file counts as a file and no lines. Where git refuses
  the size is left as it was, NULL when nothing was measured. `pr_id` and
  `merge_commit` hold the latest merge, the size all of them. A work on the default
  branch has no merge and no PR fields; a PR that ends as `up_to_date` writes nothing;
- `close_work` sets `ended_at` and `ended_as` = `closed`; `dismiss_work`, `dismissed`;
  `fail_work`, `failed`; moving the task to `done` with the work still on it,
  `task_closed`.

### Schema

`schema.sql` is `CREATE TABLE IF NOT EXISTS` only; there are no migrations. A new
**table** appears in an existing database by itself at the next startup. A new
column, a changed column or a `CHECK` never reaches an existing database: the owner
brings that database up to the schema by hand before the hub starts.

The `CHECK` on `prs.status` lists four values of which two are reachable: `merged`
and `closed` are states in which the row stops existing rather than states it lives
in.

---

## 14. The agent tool surface

Identity is the path `/mcp/<agent>/` and nothing else. The role is read from
`agents.kind` on every call; an unknown name gets the executor role.

Four seams to the supervisor, all installed by the hub at startup because `mcp.py`
holds no `Bus`: the piggyback drain, stopping a turn, compaction, and firing. The
first is a question with no consequences and defaults to silence; the other three
are actions and default to refusing.

Shared by everyone:

```
say(to, text)                    # to = "all" | a participant's name
chat(before_id?)                 # the common chat, newest page first, cursor backwards
remind(op, ...)                  # set(to, text, in_seconds) — the same message, sent later; wakes the addressee
                                 # cancel(reminder_id) — one of your own that has not gone
expect(about, within_seconds)    # an address a service notifies; its answer or the due time wakes you
task(op, ...)                    # list | read | create | update | move | link (link with remove=true drops it)
                                 # list(parent?, status?, query?) — one level, or a search under parent or over the board
work(op, ...)                    # show — your brief, branch and assigner
                                 # show(work) — one work in full, its stored output tail included
                                 # finish — your report to whoever assigned the work, with a PR if you like
pr(op, ...)                      # create | comment | list | read
                                 # merge | close  (managers only)
note(op, kind, ...)              # wiki: list | read | comment | write | undo | delete; pages are files in the sandbox
                                 # rule: list; create | update | delete  (director only)
ticket(op, ...)                  # create | comment | list | read | resolve | link
run(op, ...)                     # start | wait | stop | list; start takes stage=
roster()                         # the team as a tree; for a manager, everything staffing its subtree turns on
```

Managers only — the director and the leads:

```
agent(op, ...)   # hire | fire | stop | compact | new_session | instruct | instructions | move
                 # save_profile | hire_from_profile | list_profiles
assign(agent, brief, task_id, branch, role, complexity)
work_close(work, summary)        # accept a reported work: the row goes, the result lands on the task
work_dismiss(work)               # write off a failed work together with its tail
work_reassign(work, to_agent, workspace)   # workspace = inherit | fresh
```

Director only:

```
stage(op, ...)   # create | reset | delete (section 12)
```

**A manager's reach is its subtree**, checked in code on every call:

- `hire` and `hire_from_profile` make the caller the new agent's manager. They
  take `title`, required, and `lead`: true makes a lead, otherwise an executor;
- `fire`, `stop`, `compact`, `new_session`, `instruct`, `instructions` and `move`
  take a target in the caller's subtree; `compact` and `new_session` are refused
  while a turn or a compaction is running on the target's session;
- `instruct(agent, text)` replaces the target's standing instructions; empty text
  clears them. `instructions(agent)` returns them;
- `move(agent, manager)` takes a new manager that is the caller or a lead in the
  caller's subtree, and is neither the target nor inside the target's subtree. It
  refuses while the moved subtree — the target and everyone under it — holds a work
  still on the books (running, paused, reported and not closed, or failed) whose
  assigner would afterwards be neither its assignee nor above it. It sends the
  moved agent one quiet line naming its new manager, and nothing to anybody else;
- `assign(agent)` takes the caller or an agent in its subtree, and records the
  caller as the assigner;
- `work_close`, `work_dismiss` and `work_reassign` take a work whose assignee is in
  the caller's subtree or which the caller assigned; `work_reassign` also takes a
  `to_agent` that is the caller or in its subtree, and records the caller as the
  assigner;
- `task(op=move, status=done)` is refused unless every work still on the task
  passes the same test as `work_close`; for an executor, unless the task has no
  works;
- `pr(op=merge)` and `pr(op=close)` take a PR whose author is the caller or in its
  subtree.

Together these keep a work's assigner on the roster and either its assignee itself
or above it for as long as the work's row exists: `assign` and `work_reassign`
reach only the caller's own subtree and record the caller, `move` refuses as above,
and `fire` refuses while its target has subordinates. `assign`, `work_reassign` and
`move` each take their decision in the same transaction as their write.

`task(op=move, status=done)` requires `result` and closes the task entirely: the
records of its works are deleted. `task(op=move, status=cancelled)` is the
director's and the leads': it requires `result`, the reason, and is refused while
any work is on the task.

`pr(op=merge)` requires `delete_branch` and will not run without it.

`roster()` answers by role. Everyone gets the whole team as a tree — each agent's
name, title, kind, runtime and model, indented under its manager — its own standing
instructions as they stand now, its own workspace and sandbox paths, the stages, as
section 12 lists, and the deferred
messages it set itself, each with its number, and the open expectations it set. A
manager additionally gets, for its subtree, each agent's status and context fill
and every deferred message and open expectation; every work that is not finished
(running, paused, reported or failed, with the reason it stopped and, for a pause,
the earliest it could resume) whose assignee is in its subtree or which it
assigned; remaining quota per runtime with its reset time, each wallet (section 10), and each runtime and
model on which no turn starts before its time of return; tasks that look ready
but whose dependency is not done; the model catalogue; and workspaces that belong
to nobody. The director alone additionally gets the directories under `ws/` the
office did not create. Apart from the caller's own instructions and paths, which its
system prompt carries as they stood when the session was created, none of that is in
any system prompt.

`remind` is the only alarm in the system. A turn is bought by a message and by
nothing else, so an agent that needs to continue later leaves itself a message and
ends its turn. The row sits in `scheduled_messages`; on its tick the bus takes what
has come due and sends it by the ordinary `say` path, then deletes the row — from
there it is an ordinary message, with its own turn, its own thread and its own
report to the sender if the turn died. A due time that passed while the hub was
down fires on the first tick after startup.

`all` is not accepted as a recipient: a post to the common chat wakes nobody, so a
deferred one would arrive at the appointed moment and do nothing. The ceiling is
seven days. It takes an offset, not an absolute instant: an agent's notion of "now"
comes from a system prompt fixed when the session was created. `cancel` takes back
a deferred message its caller set that has not gone yet; the owner cancels any on
the works page. Either deletes only a row that has not come due.

`agent(op=fire)` refuses while the agent still has active work — running, paused,
or reported and not closed — while it has subordinates (they are moved or fired
first), while an open PR names it as author, and while any vendor process is
speaking on its session — a turn or a compaction. The decision and the deletion
happen under the same lock that starts turns. The fired agent's failed works are
deleted with it, and so are the deferred messages it set and its expectations.

The managers' tools are hidden from an executor's `tools/list` and rejected by name
if it calls them; `stage` is hidden from a lead and rejected the same way.
`note(kind=rule)` refuses `create`, `update` and `delete` to anyone but the director,
and `pr(op=merge|close)` refuses an executor.

**No tool exists whose only purpose is to check whether anything has happened.**
There is no `inbox_check`, no `poll`, and no file standing in for them. Everything
arrives by itself.

The reading operations — `task(op=list|read)`, `note(op=list|read)`,
`pr(op=list|read)`, `ticket(op=list|read)`, `work(op=show)`, `chat()`, `roster()` —
are not polling:
they answer what was asked rather than "is there news". Their composition follows
from the rule in section 4: no state may be knowable only from a message that was
received. So the reason a work failed, the text of PR comments, one's own brief and
the common chat's history are all readable.

An agent's composition at hire is `{runtime, model, effort}`. Runtime and model
are required, effort is not; the environment proposes no default. A composition
that works is saved by a manager as a named profile; the profiles are shared by
all managers.

### The toolset is not configurable by anything

Which tools an agent has is decided by its runtime. An agent gets its vendor's
built-in set in full, plus the office's tools. There is no choice of composition at
hire, no catalogue and no trimming flag anywhere.

**An agent has exactly one MCP server — the office's — and it is not chosen.** It is
always attached, at `http://127.0.0.1:<port>/mcp/<agent name>/`. The trailing slash
is required: without it the hub answers with a redirect, and Python's standard
library, on which the agy shim is written, refuses to replay a POST on one.

claude gets it through `--mcp-config` plus `--strict-mcp-config`, which cuts the
agent off from servers the owner configured for himself; codex through
`-c mcp_servers.office.url` on the invocation; agy through a stdio shim registered
once machine-globally (`agy mcp add`), which reads the address out of the turn's
environment.

Nothing else can be attached: neither an agent nor the owner has such an
operation.

---

## 15. The owner's interface

The menu has eleven items with icons; the icons are drawn in the markup, as there is
no icon font and no external source in the system. Five items carry counters and
only five: open tickets, works on the books, unread direct messages, unread common
chat, waiting PRs. A zero is not drawn.

Each counter is its own region listening for the event kinds that can change it. The
list of kinds is declared in one place (`office/web/routes/nav.py`) and the markup
takes its trigger from there.

The head of every page carries the office's switch (section 4): its state, "Running" or
"Paused"; a button that flips it; while it is paused and turns are still running, how many
are still finishing; a pending timer as "pause at 10:00" or "run at 23:00" with a button
that cancels it; and a time field that sets the timer, labelled with the flip it would
make. The region refreshes on the events of the journal, of settings and of agents, and
its time field counts as a draft.

Two pages are **screens** rather than documents: the main page and the board. They
take the window's height, and what scrolls inside them is what grows — the
conversation and the board's columns. The rest are documents and scroll whole.

All times are shown local and stored UTC.

1. **Main** — the director's chat (pages of 200 messages, cursor backwards), a stop
   button for its turn with `running_for` and `quiet_for` counters (only while a turn
   runs), the plate of critical notices, quota by bucket, the director's context fill
   with a compact button, and open tickets addressed to the owner. Until the project
   is `ready` the setup panel stands here; until the director exists, the form that
   hires it — which also asks for the owner's name, since the first run bypasses the
   settings page and renaming without losing one's own correspondence is only
   possible before the first message. Under the model field is the vendor model
   catalogue, the same one the office would refuse by.
2. **Direct messages** — a thread list and one thread, paginated.
3. **Kanban** — seven columns. By default the board holds the top-level tasks. A card
   with children carries their counts, open and done, and opens the board of its
   children (`?parent=N`); that board has a breadcrumb of the chain back to the top
   level. A link shows every task on one flat board (`?flat=1`). The done and
   cancelled columns each hold the twenty tasks changed most recently, newest first,
   with a link that shows all of them (`?all_done=1`). The owner files a task into "idea", with an optional
   parent by id, filled in with the current parent on a parent's board, and edits
   titles and bodies; there is no move control for him. A card with unclosed
   blocking tasks carries the list of them, in any column.
4. **Works** — who, what, branch, context, a stop button, with the quota bars of the
   main page beside them. Finished works do not appear. State is stated both ways:
   either "a turn is running" with `running_for` and `quiet_for`, or "no turn" naming
   the assignee's status; a reported work names who it waits on to close it. The brief is
   collapsed and rendered as the markdown it is written in. The turn's output is a
   collapsed block, parsed by the same parser the office listens to the turn with:
   speech, tool calls with their start and end, errors. A call with a start and no end
   is marked. The transcript is served from its own endpoint, so it is parsed only when
   somebody opens it, and it lives exactly as long as the tail file does. Under the
   works are the deferred messages the office is holding, and the owner can cancel
   any of them.
5. **Common chat** — paginated.
6. **Team** — the team as a tree, each agent under its manager with its title; on
   what, how much context, and under each agent what it awaits from a service,
   since when and until when; under them the stages: each one's state with its timers while preparing, the run in progress with its timers,
   who is waiting, a pending reset or delete, the preparation command, and the reason
   for a broken one. No hiring or firing here, and no control over stages.
7. **Knowledge** — wiki and rules.
8. **Pull requests** — the list and comments. There is no merge button.
9. **System notices** — the journal: turns that died, quota exhausted with its hold,
   compaction results, hub restarts, the office's own tick failing over an agent, a
   marked process that survived being killed, and open works that nothing moves and
   nobody above their agent has moved.
10. **Settings** — the owner's name, the director's standing instructions, the
    silence threshold, the wallets' weekly limits, 5h windows and reserve, the two price lists (a table each) and their note, the work complexities (a list, showing the values in effect), and the director's runtime/model/effort with the cost shown
    before confirmation. Also the delivery address, the outcome of the last delivery,
    and — while the owner's repository is missing `receive.denyCurrentBranch` — the
    "Set up delivery" button that writes it back.

Every element of the main page collects its own data and nobody else's; the full
page render is the merge of those collectors.

`Bus.health()` warnings — conditions the office can see and cannot fix — are printed
as a line on the main page when the list is not empty. `/health` returns the same
plus `project_state` and whether the database answers. Today they are: codex being
reachable only through a batch shim, which truncates the inline hook config and
leaves its agents without mid-turn delivery; the office's own tick failing over a
named agent; a marked process that survived being killed; the project repository not
`ready`; the owner's folder having stopped being a git repository; an agy hook
command that cannot be executed; codex agents whose context fill cannot be read;
directories under `ws/` the office did not create; and a runtime that has never
reported quota. All but the tick failure are re-derived on every call and stop
showing themselves the moment the condition clears; that one clears on the agent's
next started turn.

### The plate and the journal

Critical things go on a plate under the director's chat, at once: its turn died (with
the reason), its quota ran out (with the time of return), or open works that nothing
moves have come up the chain past the director. The plate carries the three newest undismissed ones and counts the
rest. There is no "retry" button. Dismissing it stamps every unseen critical notice and
is the only thing that makes it go away — a turn that failed is an instant, not a
condition that clears itself. The journal keeps the rows either way.

Everything else is a line in the journal. Anything that needs no attention is not
written at all: statuses, hiring and firing, PRs opening and merging, tasks moving,
wiki and rule edits, quota figures — each of those has a page.

### A refusal has to be visible

Every refusal is a rendered panel carrying the reason, with status 400. htmx does not
swap 4xx responses by default; `app.js` opts 400 in explicitly and only 400. A wiki
save against a version somebody has already moved answers 409: nothing is swapped and
the sentence goes to an alert, so the owner's text stays in the box.

Upstream of that, a form names its required fields with `data-office-required`: the
submit button renders disabled and JavaScript enables it only once every named field
is non-empty after trimming and passes its own constraints — the owner's name,
for one, may not contain a colon. `required` alone admits a single space, and htmx runs
`checkValidity` and stops before opening the request, which is a click that does
nothing and says nothing.

### Live updates

`Broadcaster.publish(kind, payload)`, where `kind` is one of a closed list:

```
agents · works · tasks · messages · prs · quota · wiki · rules · settings ·
tickets · notices · stages
```

`app.js` subscribes to all of them and re-dispatches each as the DOM event
`office:<kind>` on `<body>`; a region re-requests its own fragment through
`hx-trigger`. A region containing an unsaved draft (a text, time, number or range input
or a `textarea` whose value differs from the one the server rendered, outside a closed
`<details>`, or a table editor whose rows were added, deleted or moved) skips the update and catches itself up the moment the draft is gone. Each
region uses its own `hx-swap`.

After the connection drops — and only after a drop, not on the first connect — every
region in which nobody is editing is re-requested by that same catch-up mechanism,
each request carrying its own region as the source.

Scroll position is remembered per scrollable box across swaps in one place. A box that
follows the newest line returns to the bottom if it was at the bottom, and otherwise
stays where the reader left it.

### settings keys

```
owner_name              the owner's name (default "Owner")
silence_notice_minutes  the silence threshold; 0 or less means never report
switch_timer            the pending timer of the office's switch: "run" or "pause", a space, an epoch second; empty means none
wallet_<runtime>_week   the total weekly limit of a runtime's wallet, in units; empty means no wallet
wallet_<runtime>_window the 5h window of a runtime's wallet, in units; empty means none
wallet_<runtime>_reserve the percentage of the week the team leaves untouched; empty means 0
price_list_note         the free text shown above the price list in roster; plain text
work_complexities       the complexities assign accepts, one per line; empty means the defaults
delivery_last           the outcome of the last delivery (JSON), for the settings page
```

---

## 16. Operations in the owner's interface

There are five; everything else is observation and conversation.

**Hiring the director and setting its composition.** The first run is impossible
without it, and no agent may change anyone's model.

**The stop button** — in the monitor and beside the director's chat. There is no limit
on a turn's length, and the managers, which decide about stopping through
`agent(op=stop)`, run on the same mechanics and can be wedged themselves.

**The compact button** for the director's context. The director cannot compact itself:
`agent(op=compact)` on itself is its own turn, which the compaction would refuse.

**The "Take my changes" button** — taking the owner's work in from his repository. The
office learns of his commits by itself before the next merge; the button is for
letting the team know sooner.

**Cancelling a deferred message**, on the works page.

---

## 17. Stack

Python 3.12 · FastAPI · SQLite (WAL) · Jinja2 + HTMX + SSE · python-markdown. No npm
and no build step.

One process: the web UI, MCP over HTTP at `/mcp/<agent>`, the hook at
`/hooks/<token>`, and the process supervisor.
It listens on `127.0.0.1` only, and before any route sees a request it refuses
one addressed to any name but `127.0.0.1` or `localhost`, and one that changes
something — any method but `GET`, `HEAD` and `OPTIONS` — and carries an `Origin`
other than the address it was sent to. A request with no `Origin`, as a service
sends it, passes. Nothing authenticates a request.
All state in one SQLite file.

Hard rules of the single process:

- **never hold a writing SQLite transaction across a subprocess call**;
- **every git call goes to a thread**;
- `assign` against a `provisioning` agent returns immediately;
- nothing long lives on the bus thread: a tick reads the queue and starts turns, while
  a compaction runs on the thread of whoever asked for it — a tool call or a page
  route, both in the thread pool.

One SQLite connection per process, every access through `db.py` under an RLock, reads
included: `sqlite3` does not serialise one connection across threads by itself.
`db.transaction()` does not nest and raises if asked to.

The dependency runs one way: the bus calls `core`, and `core` knows nothing of the bus.
A live notification of a mutation arrives through a hook the hub installs and carries
no information anybody acts on; all content is read from the tables.

The office logs under the `office` namespace to stderr and to `<root>/office.log`.
