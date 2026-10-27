"""Owner-facing work snapshots and captured output."""

from office import core, db, git
from office.adapters import RUNTIMES, adapter_for
from office.web import transcript


def works(conn, bus) -> list[dict]:
    live = bus.live_turns()
    rows = db.query(
        conn,
        """
        SELECT works.id, works.status, works.brief, works.branch, works.started_at,
               works.pause_reason, works.resume_after, works.fail_reason,
               agents.name AS agent_name, agents.status AS agent_status,
               agents.runtime AS agent_runtime, agents.model AS agent_model,
               agents.effort AS agent_effort,
               agents.context_used, agents.context_limit,
               assigner.name AS assigner_name, tasks.title AS task_title,
               work_log.role AS log_role, work_log.complexity AS log_complexity
        FROM works
        JOIN agents ON agents.id = works.agent_id
        JOIN agents assigner ON assigner.id = works.assigned_by_agent_id
        LEFT JOIN tasks ON tasks.id = works.task_id
        LEFT JOIN work_log ON work_log.work_id = works.id
        ORDER BY works.id DESC
        """,
    )
    result = []
    for row in rows:
        work = dict(row)
        work["turn"] = live.get(work["agent_name"])
        if work["status"] in {"running", "done"} and work["turn"] is not None:
            work["derived_status"] = "running"
        elif work["status"] == "done":
            work["derived_status"] = "reported"
        elif work["status"] == "running":
            work["derived_status"] = "waiting"
        else:
            work["derived_status"] = work["status"]
        result.append(work)
    return result


def quota(conn) -> list[tuple[str, list[dict]]]:
    grouped = {runtime: [] for runtime in RUNTIMES}
    for row in core.quota_buckets(conn):
        grouped[row["runtime"]].append(row)
    return list(grouped.items())


def snapshot(conn, config, bus) -> dict:
    cards = works(conn, bus)
    for work in cards:
        turn = work["turn"]
        if turn is not None:
            work["turn"] = {key: turn[key] for key in (
                "started_wall_at", "last_activity_wall_at", "context_used", "context_limit", "call_instants",
            )}
    return {
        "project_state": git.project_state(config),
        "works": cards,
        "wakes": core.scheduled_messages(conn),
    }


def changed_fields(previous: dict, current: dict) -> dict:
    """Changed fields, recursively within dictionaries; lists replace a field."""
    return {
        key: changed_fields(previous[key], value)
        if isinstance(value, dict) and isinstance(previous.get(key), dict) else value
        for key, value in current.items() if key not in previous or previous[key] != value
    }


def work_state(work: dict) -> dict:
    """Card fields without the independently delivered last activity instant."""
    return {**work, "turn": None if work["turn"] is None else {
        key: value for key, value in work["turn"].items() if key != "last_activity_wall_at"
    }}


def new_clock_activity(previous, current) -> dict[str, float]:
    """Initial activity instants for newly displayed live turns."""
    known = {(work["agent_name"], work["turn"]["started_wall_at"]) for work in previous if work["turn"] is not None}
    return {
        work["agent_name"]: work["turn"]["last_activity_wall_at"] for work in current
        if work["turn"] is not None and (work["agent_name"], work["turn"]["started_wall_at"]) not in known
    }


def works_delta(previous: dict, current: dict) -> dict:
    changes = {key: current[key] for key in ("wakes", "project_state") if previous[key] != current[key]}
    before = {work["id"]: work_state(work) for work in previous["works"]}
    after = {work["id"]: work_state(work) for work in current["works"]}
    cards = {}
    added = [work for work_id, work in after.items() if work_id not in before]
    removed = [work_id for work_id in before if work_id not in after]
    changed = [
        {"id": work_id, **changed_fields(before[work_id], work)}
        for work_id, work in after.items() if work_id in before and before[work_id] != work
    ]
    if added:
        cards["added"] = added
    if removed:
        cards["removed"] = removed
    if changed:
        cards["changed"] = changed
    if list(before) != list(after):
        cards["order"] = list(after)
    if cards:
        changes["works"] = cards
    return changes


def output_delta(previous: dict | None, current: dict) -> dict:
    if previous is None or (previous["source"] != current["source"] and previous["rows"] != current["rows"]):
        return {"rows": current["rows"], "note": current["note"]}
    changes = {}
    if previous["note"] != current["note"]:
        changes["note"] = current["note"]
    before, after = previous["rows"], current["rows"]
    if before != after:
        for drop in range(len(before) + 1):
            overlap = len(before) - drop
            if overlap <= len(after) and before[drop:] == after[:overlap]:
                changes.update(drop=drop, append=after[overlap:])
                break
    return changes


def output(conn, config, bus, work_id: int) -> dict:
    work = db.query_one(
        conn,
        "SELECT works.status, works.output_tail, agents.name AS agent_name, "
        "agents.runtime AS agent_runtime FROM works JOIN agents ON agents.id = works.agent_id "
        "WHERE works.id = ?",
        (work_id,),
    )
    if work is None:
        return {"source": ("missing", work_id), "rows": [], "note": "Work not found. Refresh Works."}
    live = False
    if work["status"] == "failed" and work["output_tail"]:
        lines = work["output_tail"].splitlines()
        source = ("failed", work_id, work["output_tail"])
    else:
        source = ("agent", work["agent_name"], bus.turn_tail_source(work["agent_name"]))
        lines = bus.turn_tail(work["agent_name"])
        live = lines is not None
        if not live:
            try:
                lines = (config.tails_dir / f"{work['agent_name']}.log").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except FileNotFoundError:
                lines = []
    if not lines:
        return {
            "source": source,
            "rows": [],
            "note": "Waiting for turn output." if live else "No captured output.",
        }
    adapter = adapter_for(work["agent_runtime"], config)
    return {"source": source, "rows": transcript.line_rows(lines, adapter), "note": None}
