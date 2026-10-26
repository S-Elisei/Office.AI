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

---

## Mobile chat

The mobile app provides direct messages with the team and the common chat.
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

### Use another tunnel

The office authenticates devices itself. The tunnel provides public HTTPS and
forwards requests to `http://127.0.0.1:7778`, preserving Host and Origin and
streaming SSE responses.

For a persistent Cloudflare Tunnel, configure its published hostname to use that
local URL. Set `OFFICE_MOBILE_ORIGIN` to the new HTTPS origin and restart the
hub. A change of origin requires registering the phone at the new address and
revoking its old registration. Cloudflare Quick Tunnels do not support SSE.

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

**A task board.** Six columns, from idea to done. You can add a task and edit its
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
