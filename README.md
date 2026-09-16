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

If you do not use `uv`, `pip install -e .` works too.

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
somewhere else, name it:

```
OFFICE_CLAUDE_BIN=/path/to/claude
OFFICE_CODEX_BIN=/path/to/codex
OFFICE_AGY_BIN=/path/to/agy
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

The office lives **inside the project it works on**. Point it at that project:

```
OFFICE_ROOT=/path/to/your-project/.office-data
python -m office.hub
```

Then open **http://localhost:7777**.

The data directory is created for you. Everything the office owns lives in it:
the database, the agents' copies of the repository, and its own copy of your
git history. Your project folder is the parent of it, and that is where finished
work is delivered.

To use another port, set `OFFICE_PORT`.

### First run

The main page walks you through two steps.

**Set up the project.** The office looks at your project folder and tells you what
it found — a repository with history, an empty one, or no repository at all. You
name the main branch, press the button, and it builds its own copy.

**Hire a director.** You give the director a name, pick a vendor, a model and an
effort level, and write your standing instructions for it. You also type your own
name here, which is how agents will address you.

After that the director is in charge. It hires the rest of the team itself.

---

## What the office does

**A team you hire.** You hire one director; the director hires and fires
executors. Each agent runs on the vendor, model and effort level chosen for it.

**A private copy of the repository for every agent.** Each agent gets its own git
clone with its own working tree. They never edit the same files at the same time.

**Work handed out on a named branch.** The director writes a brief, names a
branch and assigns it to an agent. The agent does the work there and reports back
when it is done.

**Pull requests.** An agent publishes its branch and opens a pull request; the
team reviews it in comments. Only the director merges.

**Delivery to your own repository.** Every merge is pushed straight into your
project folder, on your main branch, and the files on disk are updated with it.
The office also picks up commits you make yourself, so the team never works from
a stale copy.

**Conversation.** You have a chat with the director on the main page, private
threads with every agent, and a common room they can all read.

**A task board.** Six columns, from idea to done. You can add a task and edit its
text; moving cards is the director's job.

**Tickets.** A question or a bug addressed to one participant, who is the only one
who can close it. A ticket addressed to you waits on your main page until you
answer it.

**A wiki and a rule book.** The wiki is shared notes with versions and comments.
The rules are short standing conventions, and every agent gets them in full at the
start of every session.

**A works monitor.** It shows who is running, for how long, how long they have
been silent, and what command they are inside right now. Every turn has a stop
button, because nothing else ever stops one.

**Quota and context in plain sight.** The main page shows how much of each
vendor's quota is left and when it resets, and how full the director's context
window is. You can compact that window with one button.

**A notice log.** Turns that died, quota that ran out and came back, restarts. It
is the record of what happened while you were not looking.

---

MIT licensed. See [LICENSE](LICENSE).
