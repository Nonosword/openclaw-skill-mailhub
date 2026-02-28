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
    db.upsert_message_analysis(
        message_id=message_id,
        title=title,
        summary=summary,
        tag=tag,
        suggest_reply=bool(suggest_reply),
        suggestion=suggestion,
        source=source,
        updated_at=utc_now_iso(),
    )
    return {"ok": True, "message_id": message_id, "source": source}


def analysis_list(date: str = "today", limit: int = 200) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_message_analysis_by_date(day, limit=limit)
    for i, r in enumerate(rows, start=1):
        r["index"] = i
        r["mailhub_id"] = r.get("message_id")
    return {"ok": True, "day": day, "count": len(rows), "items": rows}

