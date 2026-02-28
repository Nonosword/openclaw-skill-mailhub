from __future__ import annotations

from typing import Any, Dict

from ..config import Settings
from ..store import DB
from ..utils.time import today_yyyy_mm_dd_utc, utc_now_iso


def analysis_record(
    *,
    message_id: str,
    title: str,
    summary: str,
    tag: str,
    suggest_reply: bool,
    suggestion: str,
    source: str = "openclaw",
) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    resolved_message_id = db.resolve_message_id(message_id)
    if not resolved_message_id:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}
    msg = db.get_message(resolved_message_id)
    if not msg:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}

    db.upsert_message_analysis(
        message_id=resolved_message_id,
        title=title,
        summary=summary,
        tag=tag,
        suggest_reply=bool(suggest_reply),
        suggestion=suggestion,
        source=source,
        updated_at=utc_now_iso(),
    )
    return {
        "ok": True,
        "mailhub_id": int(msg.get("mail_id") or 0),
        "mail_id": int(msg.get("mail_id") or 0),
        "message_id": resolved_message_id,
        "source": source,
    }


def analysis_list(date: str = "today", limit: int = 200) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_message_analysis_by_date(day, limit=limit)
    for i, r in enumerate(rows, start=1):
        r["index"] = i
        r["mailhub_id"] = int(r.get("mail_id") or 0)
        r["mail_id"] = int(r.get("mail_id") or 0)
    return {"ok": True, "day": day, "count": len(rows), "items": rows}
