"""The wiki as files: a copy of every page in each workspace's sandbox, and
publishing a page from its file.

A page `a/b` is `<sandbox>/wiki/a/b.md`, holding the page's body. `wiki_copies`
keeps, per workspace, the version and the body the office last wrote into each
file; a file whose text differs from that body holds edits not published, and
the office leaves it as it is.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from office import core, db, git
from office.config import Config

#: The line that opens a place a publish could not merge.
_MARKER = "<<<<<<< your copy"


def wiki_dir(config: Config, workspace_id: str) -> Path:
    return config.scratch_dir / workspace_id / "wiki"


def page_file(config: Config, workspace_id: str, path: str) -> Path:
    return wiki_dir(config, workspace_id) / f"{path}.md"


def _read(file: Path) -> str | None:
    """The file's text with its line ends made `\\n`, or None when there is none."""
    try:
        return file.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except FileNotFoundError:
        return None


def _write(file: Path, text: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + ".office-tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, file)


def _copy(conn, workspace_id: str, path: str) -> dict | None:
    row = db.query_one(
        conn, "SELECT version, body FROM wiki_copies WHERE workspace_id = ? AND path = ?",
        (workspace_id, path),
    )
    return dict(row) if row is not None else None


def _record(conn, workspace_id: str, path: str, version: int, body: str) -> None:
    with db.transaction(conn):
        db.execute(
            conn,
            "INSERT OR REPLACE INTO wiki_copies (workspace_id, path, version, body) "
            "VALUES (?, ?, ?, ?)",
            (workspace_id, path, version, body),
        )


def _edited(text: str | None, page_body: str | None, copy: dict | None) -> bool:
    """Whether a file holds edits of its agent's: there is one, and it is neither
    the page nor what the office last wrote there."""
    return (
        text is not None and text != page_body and (copy is None or text != copy["body"])
    )


def sync_page(conn, config: Config, workspace_id: str, path: str) -> None:
    """Bring one file up to its page, unless it holds edits: write the page where
    the file is missing or is what the office last wrote. Where the page is gone,
    forget the copy, and remove the file unless it holds edits."""
    page = db.query_one(conn, "SELECT version, body FROM wiki WHERE path = ?", (path,))
    copy = _copy(conn, workspace_id, path)
    file = page_file(config, workspace_id, path)
    if page is None:
        if copy is None:
            return
        if _read(file) == copy["body"]:
            file.unlink()
        with db.transaction(conn):
            db.execute(
                conn, "DELETE FROM wiki_copies WHERE workspace_id = ? AND path = ?",
                (workspace_id, path),
            )
        return
    if copy is not None and copy["version"] == page["version"] and file.exists():
        return
    text = _read(file)
    if _edited(text, page["body"], copy):
        return
    if text != page["body"]:
        _write(file, page["body"])
    _record(conn, workspace_id, path, page["version"], page["body"])


def sync(conn, config: Config, workspace_id: str) -> None:
    """sync_page for every page and every copy of this workspace's sandbox."""
    paths = {r["path"] for r in db.query(conn, "SELECT path FROM wiki")}
    paths |= {
        r["path"]
        for r in db.query(conn, "SELECT path FROM wiki_copies WHERE workspace_id = ?", (workspace_id,))
    }
    for path in sorted(paths):
        sync_page(conn, config, workspace_id, path)


def state(conn, config: Config, workspace_id: str, path: str, version: int) -> str:
    """How the caller's file of a page at `version` stands, in a sentence."""
    file = page_file(config, workspace_id, path)
    copy = _copy(conn, workspace_id, path)
    text = _read(file)
    if text is None:
        return f"You have no file {file}."
    if copy is not None and text == copy["body"]:
        if copy["version"] == version:
            return f"Your file {file} holds v{version}."
        return f"Your file {file} holds v{copy['version']}; the page is at v{version}."
    made_on = f"v{copy['version']}" if copy is not None else "no copy of the page"
    return f"Your file {file} has edits you have not published, made on {made_on}."


def publish(
    conn, config: Config, workspace_id: str, path: str, *, category: str | None,
    title: str | None, actor: str,
) -> str:
    """note(op=write, kind=wiki): publish the caller's file of a page, merged with
    what changed on the page since the caller's copy. Answers in a sentence."""
    file = page_file(config, workspace_id, path)
    mine = _read(file)
    if mine is None:
        raise ValueError(
            f"no file for '{path}' — a page is published from {file}; write it there first"
        )
    if _MARKER in mine:
        raise ValueError(
            f"{file} still holds a place marked '{_MARKER}': settle it, remove the markers "
            "and publish again"
        )
    page = db.query_one(conn, "SELECT * FROM wiki WHERE path = ?", (path,))
    if page is None:
        if not category or not title:
            raise ValueError(f"'{path}' is a new page: publishing it needs category and title")
        core.note_wiki(
            conn, path=path, category=category, title=title, body=mine, updated_by=actor,
            expected_version=None,
        )
        _record(conn, workspace_id, path, 1, mine)
        return f"wiki '{path}' created as v1"

    version = page["version"]
    category = category or page["category"]
    title = title or page["title"]
    if mine == page["body"] and (category, title) == (page["category"], page["title"]):
        _record(conn, workspace_id, path, version, mine)
        return f"your file is the page as it stands at v{version}: nothing to publish"
    copy = _copy(conn, workspace_id, path) or {"version": 0, "body": ""}
    merged, conflicts = mine, 0
    if copy["version"] != version:
        merged, conflicts = _merge(mine, copy["body"], page["body"], copy["version"], version)
    if conflicts:
        _write(file, merged)
        _record(conn, workspace_id, path, version, page["body"])
        return (
            f"Nothing published: v{version} of '{path}' changes the same lines as your edits, "
            f"in {conflicts} place(s). They are marked in your file between '{_MARKER}' and "
            f"'>>>>>>> v{version}'; the rest of v{version} is merged into it. Settle the marked "
            "places, remove the markers and publish again."
        )
    core.note_wiki(
        conn, path=path, category=category, title=title, body=merged, updated_by=actor,
        expected_version=version,
    )
    if merged != mine:
        _write(file, merged)
    _record(conn, workspace_id, path, version + 1, merged)
    if merged == mine:
        return f"wiki '{path}' published as v{version + 1}"
    return (
        f"wiki '{path}' published as v{version + 1}: your edits were merged with the changes "
        f"made since your copy of v{copy['version']} (last written by {page['updated_by']}). "
        f"Your file now holds v{version + 1}."
    )


def _merge(mine: str, base: str, theirs: str, base_version: int, version: int) -> tuple[str, int]:
    """`git merge-file` of the three texts: the result, and how many places could
    not be merged."""
    with tempfile.TemporaryDirectory() as scratch:
        names = []
        for name, text in (("mine", mine), ("base", base), ("theirs", theirs)):
            Path(scratch, name).write_bytes(text.encode("utf-8"))
            names.append(name)
        done = git.git(
            ["merge-file", "-p", "-L", "your copy", "-L", f"v{base_version}", "-L", f"v{version}",
             *names],
            cwd=scratch, check=False,
        )
    return done.stdout, done.returncode
