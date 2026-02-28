from __future__ import annotations

from typing import Any, Dict, List

from ..config import Settings
from ..store import DB
from ..utils.time import today_yyyy_mm_dd_utc
from .triage import triage_day


def daily_summary(date: str = "today", include_lists: bool = True) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()

    # Ensure tags/reply-needed queue are up-to-date for the day from existing DB messages.
    tri = triage_day(day)
    tag_counts = tri.get("tag_counts", [])

    by_type: Dict[str, int] = {k: int(v) for k, v in tag_counts}
    reply_counts = db.reply_status_counts_by_message_date(day)

    sent_items = db.list_reply_queue_by_message_date(day, status="sent", limit=50)
    pending_items = db.list_reply_queue_by_message_date(day, status="pending", limit=50)

    out: Dict[str, Any] = {
        "day": day,
        "stats": {
            "total": int(tri.get("total", 0)),
            "by_type": by_type,
            "replied": int(reply_counts.get("sent", 0)),
            "suggested_not_replied": int(reply_counts.get("pending", 0)),
            "auto_replied": int(reply_counts.get("auto_sent", 0)),
        },
        "summary_text": _compose_summary_text(day, by_type, reply_counts),
    }
    if include_lists:
        out["replied_list"] = _to_simple_list(sent_items)
        out["suggested_not_replied_list"] = _to_simple_list(pending_items)
    return out


def _to_simple_list(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, x in enumerate(items, start=1):
        out.append(
            {
                "index": i,
                "queue_id": x.get("id"),
                "message_id": x.get("message_id"),
                "from": x.get("from_addr") or "",
                "subject": x.get("subject") or "",
                "status": x.get("status") or "",
                "send_mode": x.get("send_mode") or "",
            }
        )
    return out


def _compose_summary_text(day: str, by_type: Dict[str, int], reply_counts: Dict[str, int]) -> str:
    top = sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_txt = ", ".join(f"{k}:{v}" for k, v in top) if top else "no tagged emails"
    return (
        f"{day} summary: total={sum(by_type.values())}, "
        f"types=[{top_txt}], "
        f"replied={reply_counts.get('sent', 0)}, "
        f"suggested_not_replied={reply_counts.get('pending', 0)}, "
        f"auto_replied={reply_counts.get('auto_sent', 0)}."
    )
