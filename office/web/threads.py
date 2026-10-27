"""Rendered rows and cursor pages for growing conversations."""

from office import core, db
from office.web.deps import get_db, get_owner_name
from office.web.templating import templates


PAGE_SIZE = 200

def page(request, kind, peer, after=0, before=None):
    conn = get_db(request)
    owner = get_owner_name(conn)
    if kind in {"ticket", "pr"}:
        table = "ticket_comments" if kind == "ticket" else "pr_comments"
        foreign = "ticket_id" if kind == "ticket" else "pr_id"
        where, args = f"{foreign} = ?", (int(peer),)
        if before is None:
            cursor, order, args = "id > ?", "ASC", args + (after,)
        else:
            cursor, order, args = "id < ?", "DESC", args + (before,)
        rows = db.query(conn, f"SELECT * FROM {table} WHERE {where} AND {cursor} ORDER BY id {order} LIMIT {PAGE_SIZE + 1}", args)
        more = len(rows) > PAGE_SIZE
        rows = list(rows[:PAGE_SIZE])
        if before is not None:
            rows.reverse()
    else:
        rows, more = core.conversation_history(
            conn, "all" if kind == "chat" else peer, owner,
            before_id=before, after_id=after if before is None else None, limit=PAGE_SIZE,
        )
    template = templates.get_template("partials/message.html")
    return {"rows": [{"id": row["id"], "html": template.render(m=row, owner_name=owner)} for row in rows], "more": more}
