from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from ..config import Settings
from ..store import DB
from ..utils.time import utc_now_iso
from ..providers.google_gmail import (
    google_calendar_create_event,
    google_calendar_delete_event,
    google_calendar_list_events,
)
from ..providers.ms_graph import (
    graph_calendar_agenda,
    graph_calendar_create_event,
    graph_calendar_delete_event,
)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(raw: str, *, default_now: datetime) -> datetime:
    v = (raw or "").strip()
    if not v:
        raise ValueError("Empty datetime value")
    parsed = v
    if parsed.endswith("Z"):
        parsed = parsed[:-1] + "+00:00"
    # Graph may return 7-digit fractional seconds; fromisoformat supports up to 6.
    m_frac = re.match(r"^(.*T\d{2}:\d{2}:\d{2}\.)(\d+)(.*)$", parsed)
    if m_frac and len(m_frac.group(2)) > 6:
        parsed = m_frac.group(1) + m_frac.group(2)[:6] + m_frac.group(3)
    # Normalize timezone like +0800 to +08:00 for strict parsing.
    m_tz = re.match(r"^(.*[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?)([+-]\d{2})(\d{2})$", parsed)
    if m_tz:
        parsed = m_tz.group(1) + m_tz.group(2) + ":" + m_tz.group(3)
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise ValueError(f"Invalid datetime value: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_now.tzinfo or timezone.utc)
    return dt.astimezone(timezone.utc)


def _keyword_range(keyword: str, now: datetime) -> Tuple[datetime, datetime]:
    k = (keyword or "").strip().lower()
    day0 = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if k == "today":
        return day0, day0 + timedelta(days=1)
    if k == "tomorrow":
        return day0 + timedelta(days=1), day0 + timedelta(days=2)
    if k == "yesterday":
        return day0 - timedelta(days=1), day0
    if k == "past_week":
        return now - timedelta(days=7), now
    if k == "this_week":
        start = day0 - timedelta(days=day0.weekday())
        return start, start + timedelta(days=7)
    if k == "this_week_remaining":
        end = day0 + timedelta(days=(7 - day0.weekday()))
        return now, end
    if k == "next_week":
        start = day0 - timedelta(days=day0.weekday()) + timedelta(days=7)
        return start, start + timedelta(days=7)
    raise ValueError(f"Unsupported datetime-range keyword: {keyword}")


def _resolve_range(
    *,
    action: str,
    datetime_raw: str,
    datetime_range_raw: str,
    duration_minutes: int,
) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    dr = (datetime_range_raw or "").strip()
    dt = (datetime_raw or "").strip()

    if dr:
        if "/" in dr:
            a, b = dr.split("/", 1)
            start = _parse_dt(a, default_now=now)
            end = _parse_dt(b, default_now=now)
            if end <= start:
                raise ValueError("datetime-range end must be after start")
            return start, end
        if dr.startswith("{"):
            try:
                payload = json.loads(dr)
            except Exception as exc:
                raise ValueError(f"Invalid datetime-range JSON: {exc}") from exc
            start = _parse_dt(str(payload.get("start") or ""), default_now=now)
            end = _parse_dt(str(payload.get("end") or ""), default_now=now)
            if end <= start:
                raise ValueError("datetime-range end must be after start")
            return start, end
        return _keyword_range(dr, now)

    if dt:
        if action == "add":
            start = _parse_dt(dt, default_now=now)
            end = start + timedelta(minutes=max(1, int(duration_minutes)))
            return start, end
        # For non-add actions, datetime means "that day" when only date is provided,
        # otherwise a 24h window from the given timestamp.
        parsed = _parse_dt(dt, default_now=now)
        if len(dt) <= 10:  # likely YYYY-MM-DD
            start = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
            return start, start + timedelta(days=1)
        return parsed, parsed + timedelta(days=1)

    if action == "add":
        raise ValueError("add requires --datetime or --datetime-range")
    if action == "remind":
        return _keyword_range("tomorrow", now)
    if action == "summary":
        return _keyword_range("this_week_remaining", now)
    return now, now + timedelta(days=3)


def _provider_events_to_unified(provider: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in raw_items:
        if provider["kind"] == "google":
            start_raw = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
            end_raw = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")
            if not start_raw or not end_raw or not e.get("id"):
                continue
            start = _iso_utc(_parse_dt(str(start_raw), default_now=datetime.now(timezone.utc)))
            end = _iso_utc(_parse_dt(str(end_raw), default_now=datetime.now(timezone.utc)))
            out.append(
                {
                    "provider_id": provider["id"],
                    "provider_kind": "google",
                    "event_id": str(e.get("id")),
                    "title": str(e.get("summary") or ""),
                    "start_utc": start,
                    "end_utc": end,
                    "location": str(e.get("location") or ""),
                    "status": str(e.get("status") or ""),
                    "description": str(e.get("description") or ""),
                    "raw_json": json.dumps(e, ensure_ascii=False),
                }
            )
            continue

        if provider["kind"] == "microsoft":
            start_raw = (e.get("start") or {}).get("dateTime")
            end_raw = (e.get("end") or {}).get("dateTime")
            if not start_raw or not end_raw or not e.get("id"):
                continue
            start = _iso_utc(_parse_dt(str(start_raw), default_now=datetime.now(timezone.utc)))
            end = _iso_utc(_parse_dt(str(end_raw), default_now=datetime.now(timezone.utc)))
            out.append(
                {
                    "provider_id": provider["id"],
                    "provider_kind": "microsoft",
                    "event_id": str(e.get("id")),
                    "title": str(e.get("subject") or ""),
                    "start_utc": start,
                    "end_utc": end,
                    "location": str((e.get("location") or {}).get("displayName") or ""),
                    "status": str(e.get("showAs") or ""),
                    "description": str(((e.get("body") or {}).get("content")) or ""),
                    "raw_json": json.dumps(e, ensure_ascii=False),
                }
            )
    return out


def _calendar_providers(db: DB, provider_id: str = "") -> List[Dict[str, Any]]:
    providers = [p for p in db.list_providers() if p.get("kind") in ("google", "microsoft")]
    if provider_id.strip():
        providers = [p for p in providers if p.get("id") == provider_id.strip()]
    return providers


def _sync_window(
    *,
    db: DB,
    providers: List[Dict[str, Any]],
    start_utc: str,
    end_utc: str,
    max_results: int = 200,
) -> Dict[str, Any]:
    synced = 0
    errors: List[Dict[str, Any]] = []

    for p in providers:
        try:
            if p["kind"] == "google":
                raw = google_calendar_list_events(p["id"], start_utc, end_utc, max_results=max_results)
            elif p["kind"] == "microsoft":
                raw = graph_calendar_agenda(p["id"], start_utc, end_utc, top=max_results)
            else:
                raw = []
            normalized = _provider_events_to_unified(p, raw)
            for item in normalized:
                db.upsert_calendar_event(
                    provider_id=item["provider_id"],
                    provider_kind=item["provider_kind"],
                    event_id=item["event_id"],
                    title=item["title"],
                    start_utc=item["start_utc"],
                    end_utc=item["end_utc"],
                    location=item["location"],
                    status=item["status"],
                    description=item["description"],
                    raw_json=item["raw_json"],
                    updated_at=utc_now_iso(),
                )
            synced += len(normalized)
        except Exception as exc:
            errors.append({"provider_id": p["id"], "provider_kind": p["kind"], "error": str(exc)})
    return {"synced": synced, "errors": errors}


def _event_line(e: Dict[str, Any]) -> str:
    title = (e.get("title") or "").strip() or "(no title)"
    start = str(e.get("start_utc") or "")
    end = str(e.get("end_utc") or "")
    loc = str(e.get("location") or "").strip()
    pid = str(e.get("provider_id") or "")
    if loc:
        return f"[{start} ~ {end}] {title} @ {loc} ({pid}#{e.get('event_id')})"
    return f"[{start} ~ {end}] {title} ({pid}#{e.get('event_id')})"


def agenda(days: int = 3) -> Dict[str, Any]:
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days)
    return calendar_event(
        event="view",
        datetime_raw=_iso_utc(start),
        datetime_range_raw=f"{_iso_utc(start)}/{_iso_utc(end)}",
        provider_id="",
    )


def calendar_event(
    *,
    event: str,
    datetime_raw: str = "",
    datetime_range_raw: str = "",
    title: str = "",
    location: str = "",
    context: str = "",
    provider_id: str = "",
    event_id: str = "",
    duration_minutes: int = 30,
) -> Dict[str, Any]:
    action = (event or "").strip().lower()
    if action not in ("view", "add", "delete", "sync", "summary", "remind"):
        return {
            "ok": False,
            "reason": "invalid_event_action",
            "supported": ["view", "add", "delete", "sync", "summary", "remind"],
        }

    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    if action == "delete":
        eid = (event_id or "").strip()
        if not eid:
            return {"ok": False, "reason": "event_id_required", "hint": "Use --event-id <provider_event_id>."}

        providers = _calendar_providers(db, provider_id=provider_id)
        targets: List[Dict[str, Any]] = []
        if providers:
            targets = providers
        else:
            # Best-effort fallback: resolve provider from cached events by event_id.
            found = db.find_calendar_events_by_event_id(eid)
            target_ids = {x.get("provider_id") for x in found if x.get("provider_id")}
            targets = [p for p in _calendar_providers(db) if p.get("id") in target_ids]

        if not targets:
            return {"ok": False, "reason": "provider_not_found", "hint": "Pass --provider-id for delete."}

        deleted: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for p in targets:
            try:
                if p["kind"] == "google":
                    google_calendar_delete_event(p["id"], eid)
                elif p["kind"] == "microsoft":
                    graph_calendar_delete_event(p["id"], eid)
                else:
                    continue
                db.delete_calendar_event(p["id"], eid)
                deleted.append({"provider_id": p["id"], "provider_kind": p["kind"], "event_id": eid})
            except Exception as exc:
                errors.append({"provider_id": p["id"], "provider_kind": p["kind"], "event_id": eid, "error": str(exc)})
        return {"ok": len(errors) == 0, "event": "delete", "deleted": deleted, "errors": errors}

    try:
        start_dt, end_dt = _resolve_range(
            action=action,
            datetime_raw=datetime_raw,
            datetime_range_raw=datetime_range_raw,
            duration_minutes=duration_minutes,
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "invalid_datetime_input",
            "error": str(exc),
            "hint": "Use --datetime <ISO8601> or --datetime-range <start/end|json|keyword>.",
        }
    start_utc = _iso_utc(start_dt)
    end_utc = _iso_utc(end_dt)
    providers = _calendar_providers(db, provider_id=provider_id)
    if not providers:
        return {"ok": False, "reason": "no_calendar_provider", "hint": "Bind Google or Microsoft first."}

    if action == "add":
        target = providers[0]
        subject = (title or "").strip() or "Untitled event"
        try:
            if target["kind"] == "google":
                created = google_calendar_create_event(
                    target["id"],
                    summary=subject,
                    start_utc_iso=start_utc,
                    end_utc_iso=end_utc,
                    location=location,
                    description=context,
                )
            else:
                created = graph_calendar_create_event(
                    target["id"],
                    subject=subject,
                    start_utc_iso=start_utc,
                    end_utc_iso=end_utc,
                    location=location,
                    body_text=context,
                )
            normalized = _provider_events_to_unified(target, [created])
            for item in normalized:
                db.upsert_calendar_event(
                    provider_id=item["provider_id"],
                    provider_kind=item["provider_kind"],
                    event_id=item["event_id"],
                    title=item["title"],
                    start_utc=item["start_utc"],
                    end_utc=item["end_utc"],
                    location=item["location"],
                    status=item["status"],
                    description=item["description"],
                    raw_json=item["raw_json"],
                    updated_at=utc_now_iso(),
                )
            return {
                "ok": True,
                "event": "add",
                "provider_id": target["id"],
                "provider_kind": target["kind"],
                "window": {"start_utc": start_utc, "end_utc": end_utc},
                "created": normalized[:1],
            }
        except Exception as exc:
            return {"ok": False, "event": "add", "reason": "create_failed", "error": str(exc)}

    sync_info = _sync_window(db=db, providers=providers, start_utc=start_utc, end_utc=end_utc)
    rows = db.list_calendar_events(start_utc=start_utc, end_utc=end_utc, provider_id=provider_id, limit=500)
    if action == "sync":
        return {
            "ok": len(sync_info["errors"]) == 0,
            "event": "sync",
            "window": {"start_utc": start_utc, "end_utc": end_utc},
            "synced_count": sync_info["synced"],
            "errors": sync_info["errors"],
        }

    if action == "view":
        return {
            "ok": len(sync_info["errors"]) == 0,
            "event": "view",
            "window": {"start_utc": start_utc, "end_utc": end_utc},
            "count": len(rows),
            "events": rows,
            "errors": sync_info["errors"],
        }

    lines = [_event_line(e) for e in rows]
    if action == "summary":
        summary_text = (
            f"Calendar summary {start_utc} ~ {end_utc}: total {len(rows)} event(s).\n"
            + ("\n".join(lines[:50]) if lines else "No events in this window.")
        )
        return {
            "ok": len(sync_info["errors"]) == 0,
            "event": "summary",
            "window": {"start_utc": start_utc, "end_utc": end_utc},
            "count": len(rows),
            "summary_text": summary_text,
            "events": rows[:50],
            "errors": sync_info["errors"],
        }

    # remind
    now_iso = _iso_utc(datetime.now(timezone.utc))
    upcoming = [x for x in rows if str(x.get("end_utc") or "") >= now_iso]
    reminder_text = (
        f"Upcoming reminders {start_utc} ~ {end_utc}: {len(upcoming)} event(s).\n"
        + ("\n".join([_event_line(x) for x in upcoming[:20]]) if upcoming else "No upcoming events.")
    )
    return {
        "ok": len(sync_info["errors"]) == 0,
        "event": "remind",
        "window": {"start_utc": start_utc, "end_utc": end_utc},
        "count": len(upcoming),
        "reminder_text": reminder_text,
        "events": upcoming[:20],
        "errors": sync_info["errors"],
    }
