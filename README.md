# Office.AI

Office.AI is a small office for AI agents. Several agents from different vendors
work together on one project: they share a git repository, open pull requests,
talk to each other, and keep a task board — and you watch it all in a browser.

---

## Install

You need:

* **Python 3.12 or newer**
* **git**, and **git-lfs** if your project uses large files
* **at least one vendor CLI** from the list below

### 1. Get the code

```
git clone <this repository>
cd Office.AI
uv sync
```

`uv sync` creates `.venv` inside the checkout and installs the office and its
dependencies into it. Without `uv`:

```
python3 -m venv .venv                             # macOS, Linux
python -m venv .venv                              # Windows
.venv/bin/python -m pip install -e .              # macOS, Linux
.venv\Scripts\python.exe -m pip install -e .      # Windows
```

### 2. Install the vendor CLIs

The office does not talk to any model itself. It starts a vendor's command-line
tool and reads what it prints. Install the ones you want to use:

| CLI | What it runs on | Where to get it |
|---|---|---|
| `claude` | Anthropic models | [code.claude.com/docs/en/quickstart](https://code.claude.com/docs/en/quickstart) |
| `codex` | OpenAI models | [learn.chatgpt.com/docs/codex/cli](https://learn.chatgpt.com/docs/codex/cli) |
| `agy` | Google Gemini models | [antigravity.google/docs/cli/install](https://antigravity.google/docs/cli/install/) |

One CLI is enough to start. You can add the others later.

### 3. Sign in to each CLI

Each CLI keeps its own login. Sign in once, in a terminal:

```
claude auth login
codex login
```

`agy` has no login command. Run `agy` on its own: it opens your web browser, you
sign in with your Google account there, and it keeps the token from then on. Over
SSH, where there is no browser, it gives you a URL to open elsewhere and a code to
type back.

To check that `claude` is signed in:

```
claude auth status
```

The office uses whatever plan each account already has. It never asks you for an
API key and never stores one.

### 4. Let the office find the CLIs

The office looks for `claude`, `codex` and `agy` on your PATH. If one is
somewhere else, name it.

macOS, Linux:

```
export OFFICE_CLAUDE_BIN=/path/to/claude
export OFFICE_CODEX_BIN=/path/to/codex
export OFFICE_AGY_BIN=/path/to/agy
```

Windows (PowerShell):

```
$env:OFFICE_CLAUDE_BIN = "C:\path\to\claude.exe"
$env:OFFICE_CODEX_BIN = "C:\path\to\codex.exe"
$env:OFFICE_AGY_BIN = "C:\path\to\agy.exe"
```

On Windows, point `OFFICE_CODEX_BIN` at the real `codex.exe`, not at a `.cmd`
wrapper. A wrapper stops messages from reaching an agent in the middle of its
turn.

### One thing to know before you start

Agents run with their vendor's permission checks turned off. They can read and
write anything your user account can. Run the office on a machine where that is
acceptable.

---

## Run

The office lives **inside the project it works on**. `OFFICE_ROOT` names its data
directory, `.office-data` in the project's folder; the hub does not start without
it. Start the hub from the Office.AI checkout.

macOS, Linux:

```
export OFFICE_ROOT=/path/to/your-project/.office-data
uv run python -m office.hub
```

Windows (PowerShell):

```
$env:OFFICE_ROOT = "C:\path\to\your-project\.office-data"
uv run python -m office.hub
```

Without `uv`, start it with the checkout's own interpreter:
`.venv/bin/python -m office.hub`, or `.venv\Scripts\python.exe -m office.hub` on
Windows.

Then open **http://localhost:7777**.

The data directory is created for you. Everything the office owns lives in it:
the database, the agents' copies of the repository, and its own copy of your
git history. Your project folder is the parent of it, and that is where finished
work is delivered.

To use another port, set `OFFICE_PORT`.

macOS, Linux:

```
export OFFICE_PORT=8080
```

Windows (PowerShell):

```
$env:OFFICE_PORT = "8080"
```

### First run

The main page walks you through two steps.

**Set up the project.** The office looks at your project folder and tells you what
it found — a repository with history, an empty one, or no repository at all. You
name the main branch, press the button, and it builds its own copy.

**Hire a director.** You give the director a name, pick a vendor, a model and an
effort level, and write your standing instructions for it. You also type your own
name here, which is how agents will address you.

After that the director is in charge. It hires the rest of the team itself.
You change its model and its standing instructions later on the settings page.

### Desktop interface

The menu stays at the left while document pages scroll. It scrolls separately
only when its contents exceed the window. Main and Kanban keep their screen
layout, with scrolling conversations and board columns. Main shows three ticket
rows at a time, with the remaining tickets in the same scrolling list.

The office switch at the bottom of the menu contains its live status, connection
indicator, finishing turns, Pause/Run and pending timer with Cancel. The separate
time form sits below that region. Status updates preserve a time being entered.
The time uses the office computer's local clock.

New messages and ticket or PR comments append while you type. Show older prepends
history. Stream reconnection catches up from the last loaded message id. Active
composer drafts, carets and selections stay in place. Create forms are separate
from live lists. Settings, wiki and existing-rule editors keep their draft guard. A closed PR drops
its comment draft; a resolved ticket drops its resolution draft.

Works updates changed cards and fields through its local connection. Expanded
Output panels receive appended rows and head removals from the same turn;
a replacement tail starts with full rows. Consecutive text rows merge on screen. Unchanged briefs are not resent. Live-output and call checks run every ten
seconds; unchanged checks send nothing. Turn and stage clocks advance in the
browser from recorded instants. Quiet clocks receive activity events on a one-second tick
per agent or stage; output alone does not update Works cards or
recreate Stop turn buttons. Above 1100px the Works quota panel stays 16px from
the top while the document scrolls; at 1100px and narrower the columns stack.

---

## Mobile app

The mobile app provides direct messages with the team, the common chat, Works, Quota
and the office's run/pause controls.
It installs from the phone's browser as a PWA. The computer runs the office and
an HTTPS tunnel. The phone needs no VPN, password, or screen lock.

### Start the mobile listener

Install [ngrok](https://ngrok.com/download) and connect it to your ngrok account.
Use the development domain assigned to that account.

Set `OFFICE_ROOT` as in **Run**, then start the hub with the public HTTPS origin:

```powershell
$env:OFFICE_MOBILE_ORIGIN = "https://your-assigned-domain.ngrok-free.app"
uv run python -m office.hub
```

The origin contains no path or trailing slash. The local interface listens on
`127.0.0.1:7777`; the mobile listener listens on `127.0.0.1:7778`.
`OFFICE_MOBILE_PORT` selects another mobile port.
Without `OFFICE_MOBILE_ORIGIN`, only the local listener starts.

In another terminal, start the tunnel to the mobile listener:

```powershell
ngrok http 7778 --url https://your-assigned-domain.ngrok-free.app --inspect=false
```

Keep the public Host header unchanged. Point the tunnel at the mobile port.
The computer's local interface, MCP, hooks and file routes are not mobile routes.

### Register a phone

1. Open the HTTPS address on the phone.
2. On ngrok's free plan, pass the warning page with **Visit Site**. The warning
   page returns every seven days.
3. Copy the public key shown on the connection screen.
4. On the computer, open **Settings → Mobile access**, name the phone, paste
   its public key and click **Register phone**.
5. On the phone, tap **Connect to office**.
6. Use the browser's **Install app** or **Add to Home Screen** action.

The phone keeps its private key in browser storage, non-exportable through Web
Crypto. Sign-in is automatic. The device list in local Settings contains a
**Revoke access** button for each phone. Revocation ends that phone's sessions
and pending sign-ins. Accepted agent work continues.

An unlocked phone grants access until revoked. Clearing site data removes its
key; register the replacement key and revoke the old device.
Browser storage belongs to the HTTPS origin.

An unconfirmed send stays on the phone with its send identifier. **Retry send**
checks delivery using that identifier, including after a page reload or hub
restart. Nothing sends pending messages automatically. Chat history is fetched
from the office and is not stored in the PWA cache.

### Works, quota and office controls

Open **Menu → Works** to see open assignments, statuses, briefs, branches, tasks,
context usage and turn timers. Expand **Output** to read an agent's transcript.
Use **Stop turn** to stop a running turn. Pending wakes have a **Cancel** button.
Open **Menu → Quota** to see stored quota readings and reset times. Quota
updates over the same socket while that page is visible, on opening and when
readings change. Returning to the page or reconnecting loads current readings.

The menu contains **Pause** / **Run**, a time field and **Cancel timer**. These
controls share the main office interface's state. Pause lets current turns finish;
owner direct messages and owed cut turns can still start turns. The timer uses
the office computer's local time, with its UTC offset shown in the menu. A time
that has passed selects its next occurrence tomorrow.

Opening Works or reconnecting receives a full snapshot through the existing
socket. While Works is visible, subsequent messages contain only changes:
new and removed works, changed fields and changed wakes. Unchanged
briefs are not resent. Events are coalesced, with a refresh every ten seconds;
unchanged refreshes send nothing. Opening and closing Menu over Works retains
its subscription and baseline. A reported work remains until its manager
closes it.

Turn and tool-call timers use recorded event instants and the phone's offset to
the server clock. They count between messages and resynchronize after sleep and
reconnection. Output is sent only for expanded panels, with head removal and
appended rows from the same tail; a replacement tail sends full rows. Adjacent
text rows merge on screen. Quiet clocks update through activity events, on a one-second tick
per agent, without updating work cards. Collapsing and
expanding Output starts with full rows. Updates preserve expanded panels and
output scrolling. Opening Works or the menu preserves the conversation and
composer text.

### Direct-message notifications

Open **Menu → Device → Enable notifications** on the phone and allow notifications
in the browser's permission dialog. Incoming personal messages show the sender
and an invitation to open Office.AI. Tapping a notification opens that chat.
Notifications from the same sender share one notification slot.
Messages in the conversation currently visible on that phone produce no push
notification. Other conversations still notify. The conversation list, Works, Quota,
Menu, Device dialog, hidden page and closed app suppress no notifications. Each
open window reports its own visible conversation.

On Android, enable notifications for Chrome or the installed app in system
settings. On iPhone and iPad, Web Push requires iOS/iPadOS 16.4 or newer and a
Home Screen installation. Delivery requires the office computer and the phone
to have internet access. The phone's notification and Focus settings apply.

The office sends Web Push directly to the browser's push service. Push delivery
uses no ngrok requests. Subscription endpoints must start with `https://`;
other endpoints are refused without saving them or closing the chat connection.
Failed deliveries stay queued; pending notifications
and subscriptions belong to the registered device. **Menu → Device → Disable
notifications** stops new pushes. **Sign out** closes the chat; notifications
remain enabled until disabled or the device is revoked in local Settings.

### Connections and traffic

One WebSocket carries sign-in, messages, history, reading marks, subscription
changes, Works, Quota and office controls. These operations create no extra HTTP
requests. The socket closes in the background and catches up from message IDs
when the app returns to chat.
Reconnection uses increasing delays. The app shell uses a cache-first fetch
handler. Each `/sw.js` request derives its cache name from the content of
`index.html` and every file under the mobile assets directory. Changes to those
files change the served worker automatically. Service-worker updates and
WebSocket connections still use requests.

### Use another tunnel

The office authenticates devices itself. The tunnel provides public HTTPS and
forwards requests to `http://127.0.0.1:7778`, preserving Host and Origin and
supporting WebSocket upgrades.

For a persistent Cloudflare Tunnel, configure its published hostname to use that
local URL. Set `OFFICE_MOBILE_ORIGIN` to the new HTTPS origin and restart the
hub. A change of origin requires registering the phone at the new address and
revoking its old registration.

The HTTPS tunnel provider can read traffic. Device signatures authenticate the
phone; they do not encrypt chat content from the tunnel provider.

---

## What the office does

**A team you hire.** You hire one director; the director hires the rest. The
team is a tree: every agent but the director works under a manager — the
director, or a lead. A lead runs its own part of the team the way the director
runs the whole: it hires, fires, moves and instructs the agents under it, gives
them work, merges their pull requests and stops their turns. Executors do the
work they are given. Every agent under a manager has a title and standing
instructions written by the managers above it. Each agent runs on the vendor,
model and effort level chosen for it.

**A private copy of the repository for every agent.** Each agent gets its own git
clone with its own working tree. They never edit the same files at the same time.

**Work handed out on a named branch.** The director or a lead writes a brief,
names a branch and assigns it to an agent. The agent does the work there and
reports back, when it is done, to whoever assigned it.

**Pull requests.** An agent publishes its branch and opens a pull request; the
team reviews it in comments. The director merges; a lead merges the pull
requests of its own part of the team.

**Delivery to your own repository.** Every merge is pushed straight into your
project folder, on your main branch, and the files on disk are updated with it.
The office also picks up commits you make yourself, so the team never works from
a stale copy.

**Conversation.** You have a chat with the director on the main page, private
threads with every agent, and a common room they can all read.

**A task board.** Seven columns, from idea to done. You can add a task and edit its
text; moving cards is the job of the director and the leads.

**Tickets.** A question or a bug addressed to one participant. Only that
participant, or you, can close it. A ticket addressed to you waits on your main
page until you answer it.

**A wiki and a rule book.** The wiki is shared notes with versions and comments.
The rules are short standing conventions, and every agent gets them in full at the
start of every session.

**A works monitor.** It shows who is running, for how long, how long they have
been silent, and what command they are inside right now. Every turn has a stop
button; nothing ends a turn on its own.

**Quota and context in plain sight.** The main page shows how much of each
vendor's quota is left and when it resets, and how full the director's context
window is. You can compact that window with one button.

**A notice log.** Turns that died, quota that ran out, restarts. It
is the record of what happened while you were not looking.

---

MIT licensed. See [LICENSE](LICENSE).
