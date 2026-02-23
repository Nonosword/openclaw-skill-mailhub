from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ..config import Settings
from ..store import DB
from ..providers.google_gmail import google_calendar_list_events
from ..providers.ms_graph import graph_calendar_agenda


def agenda(days: int = 3) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    time_min = now.isoformat().replace("+00:00", "Z")
    time_max = end.isoformat().replace("+00:00", "Z")

    providers = db.list_providers()
    items: List[Dict[str, Any]] = []

    # Google primary calendar
    for p in providers:
        if p["kind"] == "google":
            try:
                ev = google_calendar_list_events(p["id"], time_min, time_max)
                for e in ev:
                    items.append(
                        {
                            "provider": "google",
                            "title": e.get("summary"),
                            "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
                            "location": e.get("location"),
                        }
                    )
            except Exception as e:
                items.append({"provider": "google", "error": str(e)})

    # Microsoft calendarView
    for p in providers:
        if p["kind"] == "microsoft":
            try:
                ev = graph_calendar_agenda(p["id"], time_min, time_max)
                for e in ev:
                    items.append(
                        {
                            "provider": "microsoft",
                            "title": e.get("subject"),
                            "start": (e.get("start") or {}).get("dateTime"),
                            "end": (e.get("end") or {}).get("dateTime"),
                            "location": (e.get("location") or {}).get("displayName"),
                        }
                    )
            except Exception as e:
                items.append({"provider": "microsoft", "error": str(e)})

    # Sort by start as string (ISO) for MVP
    items_sorted = sorted([x for x in items if "start" in x], key=lambda x: x["start"] or "")
    errors = [x for x in items if "error" in x]

    return {"ok": True, "window_days": days, "events": items_sorted, "errors": errors}