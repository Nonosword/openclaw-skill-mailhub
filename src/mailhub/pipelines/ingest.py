from __future__ import annotations

import json
from typing import Any, Dict, List

from ..config import Settings
from ..store import DB
from ..utils.time import utc_now_iso, today_yyyy_mm_dd_utc, yyyy_mm_dd_utc, parse_since

from ..providers.imap_smtp import list_recent_headers, fetch_and_store_recent_full
from ..providers.google_gmail import gmail_list_messages, gmail_get_message
from ..providers.ms_graph import graph_list_recent_messages, graph_get_message

from .triage import normalize_and_store_message


def inbox_poll(since: str = "15m", mode: str = "alerts") -> Dict[str, Any]:
    """
    Poll providers for new items, store minimal headers (or full message where supported).
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    collected: List[Dict[str, Any]] = []

    # IMAP headers only (MVP)
    try:
        imap_full = fetch_and_store_recent_full(since=since)
        for item in imap_full.get("saved", []):
            collected.append({"provider": "imap", **item})
    except Exception as e:
        collected.append({"provider": "imap", "error": str(e)})

    # Google: list ids then fetch full
    try:
        g = gmail_list_messages(since=since)
        for item in g:
            raw = gmail_get_message(item["provider_id"], item["gmail_id"])
            normalize_and_store_message(raw, provider_kind="google", raw_source=raw, provider_id=item["provider_id"])
            collected.append({"provider": "google", "id": raw.get("id"), "subject": _gmail_subject(raw)})
    except Exception as e:
        collected.append({"provider": "google", "error": str(e)})

    # Microsoft: list ids then fetch full
    try:
        ms = graph_list_recent_messages(since=since)
        for item in ms:
            raw = graph_get_message(item["provider_id"], item["graph_id"])
            normalize_and_store_message(raw, provider_kind="microsoft", raw_source=raw, provider_id=item["provider_id"])
            collected.append({"provider": "microsoft", "id": raw.get("id"), "subject": raw.get("subject")})
    except Exception as e:
        collected.append({"provider": "microsoft", "error": str(e)})

    return {"ok": True, "since": since, "items": collected}


def inbox_ingest_day(date: str = "today") -> Dict[str, Any]:
    """
    For MVP: reuse poll with wide window to ensure day coverage.
    """
    if date == "today":
        day = today_yyyy_mm_dd_utc()
    else:
        day = date

    # Fetch last 36h to cover timezones; triage filters by date prefix.
    out = inbox_poll(since="36h", mode="ingest")
    out["day"] = day
    return out


def _gmail_subject(raw: Dict[str, Any]) -> str:
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", []) if "name" in h and "value" in h}
    return headers.get("subject", "")