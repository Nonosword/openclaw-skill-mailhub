from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

from ..core.config import Settings
from ..core.logging import get_logger, log_event
from ..core.store import DB
from ..shared.time import utc_now_iso, today_yyyy_mm_dd_utc

from ..connectors.providers.imap_smtp import fetch_and_store_recent_full
from ..connectors.providers.google_gmail import gmail_list_messages, gmail_get_message
from ..connectors.providers.ms_graph import graph_list_recent_messages, graph_get_message

from .triage import normalize_and_store_message

logger = get_logger(__name__)


def inbox_poll(since: str = "15m", mode: str = "alerts", provider_id: str = "") -> Dict[str, Any]:
    """
    Poll providers with account-level incremental cursors.
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    providers = _mail_providers(db, provider_id=provider_id)
    log_event(
        logger,
        "inbox_poll_start",
        since=since,
        mode=mode,
        provider_id=provider_id,
        provider_count=len(providers),
    )
    collected: List[Dict[str, Any]] = []
    for p in providers:
        try:
            kind = str(p.get("kind") or "").strip().lower()
            pid = str(p.get("id") or "")
            log_event(logger, "provider_poll_start", provider_id=pid, kind=kind)
            if kind == "google":
                out = _poll_google_provider(s, db, p)
                collected.append(out)
            elif kind == "microsoft":
                out = _poll_microsoft_provider(s, db, p)
                collected.append(out)
            elif kind == "imap":
                out = _poll_imap_provider(s, db, p)
                collected.append(out)
            else:
                out = {"provider": kind, "provider_id": pid, "count": 0, "items": []}
                collected.append(out)
            log_event(
                logger,
                "provider_poll_done",
                provider_id=pid,
                kind=kind,
                count=int((out or {}).get("count") or 0),
            )
        except Exception as exc:
            collected.append(
                {
                    "provider": str(p.get("kind") or ""),
                    "provider_id": str(p.get("id") or ""),
                    "error": str(exc),
                }
            )
            log_event(
                logger,
                "provider_poll_error",
                level="error",
                provider_id=str(p.get("id") or ""),
                kind=str(p.get("kind") or ""),
                error=str(exc),
            )

    log_event(
        logger,
        "inbox_poll_done",
        since=since,
        mode=mode,
        provider_count=len(collected),
        total_count=sum(int((x.get("count") or 0)) for x in collected if isinstance(x, dict)),
    )
    return {"ok": True, "since": since, "mode": mode, "items": collected}


def inbox_ingest_day(date: str = "today") -> Dict[str, Any]:
    """
    Reuse cursor-based poll; ingest day is now logical wrapper for compatibility.
    """
    if date == "today":
        day = today_yyyy_mm_dd_utc()
    else:
        day = date

    out = inbox_poll(since="36h", mode="ingest")
    out["day"] = day
    return out


def inbox_bootstrap_provider(provider_id: str, cold_start_days: int | None = None) -> Dict[str, Any]:
    """
    After bind, run one immediate incremental pull from a cold-start window.
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    p = db.get_provider(provider_id)
    if not p:
        return {"ok": False, "reason": "provider_not_found", "provider_id": provider_id}

    _reset_cursor(db, provider_id)
    if cold_start_days is not None:
        meta = _provider_meta(p)
        meta["mail_cold_start_days"] = int(max(1, cold_start_days))
        db.upsert_provider(
            pid=provider_id,
            kind=str(p.get("kind") or ""),
            email=(p.get("email") or ""),
            meta_json=json.dumps(meta, ensure_ascii=False),
            created_at=str(p.get("created_at") or utc_now_iso()),
        )

    out = inbox_poll(mode="bootstrap", provider_id=provider_id)
    return {"ok": True, "provider_id": provider_id, "bootstrap": out}


def inbox_read(message_id: str, include_raw: bool = False) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    resolved_message_id = db.resolve_message_id(message_id)
    if not resolved_message_id:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}
    msg = db.get_message(resolved_message_id)
    if not msg:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}

    payload: Dict[str, Any] = {
        "mail_id": int(msg.get("mail_id") or 0),
        "id": msg.get("id"),
        "provider_id": msg.get("provider_id"),
        "thread_id": msg.get("thread_id"),
        "from_addr": msg.get("from_addr") or "",
        "to_addrs": msg.get("to_addrs") or "",
        "subject": msg.get("subject") or "",
        "date_utc": msg.get("date_utc") or "",
        "snippet": msg.get("snippet") or "",
        "body_text": msg.get("body_text") or "",
        "body_html": msg.get("body_html") or "",
        "has_attachments": bool(msg.get("has_attachments")),
    }
    if include_raw:
        payload["raw_json"] = msg.get("raw_json") or ""
    return {"ok": True, "message": payload}


def _gmail_subject(raw: Dict[str, Any]) -> str:
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", []) if "name" in h and "value" in h}
    return headers.get("subject", "")


def _mail_providers(db: DB, *, provider_id: str = "") -> List[Dict[str, Any]]:
    providers = db.list_providers()
    if provider_id.strip():
        providers = [p for p in providers if p.get("id") == provider_id.strip()]
    out: List[Dict[str, Any]] = []
    for p in providers:
        kind = str(p.get("kind") or "").strip().lower()
        if kind not in ("google", "microsoft", "imap"):
            continue
        meta = _provider_meta(p)
        if not bool(meta.get("is_mail", True)):
            continue
        out.append(p)
    return out


def _provider_meta(provider: Dict[str, Any]) -> Dict[str, Any]:
    raw = provider.get("meta_json")
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cursor_key(provider_id: str) -> str:
    return f"mail.cursor.{provider_id}.state"


def _load_cursor(db: DB, provider_id: str) -> Dict[str, Any]:
    raw = db.kv_get(_cursor_key(provider_id))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cursor(db: DB, provider_id: str, state: Dict[str, Any]) -> None:
    db.kv_set(_cursor_key(provider_id), json.dumps(state, ensure_ascii=False), utc_now_iso())


def _reset_cursor(db: DB, provider_id: str) -> None:
    db.kv_set(_cursor_key(provider_id), "{}", utc_now_iso())


def _cold_start_days(s: Settings, provider: Dict[str, Any]) -> int:
    meta = _provider_meta(provider)
    try:
        days = int(meta.get("mail_cold_start_days", 0))
    except Exception:
        days = 0
    if days <= 0:
        days = int(s.mail.fetch.default_cold_start_days)
    return max(1, days)


def _cursor_start_utc(s: Settings, provider: Dict[str, Any], state: Dict[str, Any]) -> str:
    cur = str(state.get("last_received_utc") or "").strip()
    if cur:
        return cur
    dt = datetime.now(timezone.utc) - timedelta(days=_cold_start_days(s, provider))
    return dt.isoformat().replace("+00:00", "Z")


def _http_status(exc: Exception) -> int:
    if isinstance(exc, requests.HTTPError):
        try:
            return int(exc.response.status_code) if exc.response is not None else 0
        except Exception:
            return 0
    return 0


def _is_rate_limit(exc: Exception) -> bool:
    return _http_status(exc) in (403, 429)


def _date_max(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def _poll_google_provider(s: Settings, db: DB, provider: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(provider.get("id") or "")
    f = s.mail.fetch
    state = _load_cursor(db, pid)
    page_size = int(state.get("page_size") or f.max_results_per_page)
    page_size = max(int(f.min_results_per_page), min(int(f.max_results_per_page), page_size))

    start_utc = _cursor_start_utc(s, provider, state)
    after_epoch = int(_parse_iso_utc(start_utc).timestamp()) - 120
    if after_epoch < 0:
        after_epoch = 0

    next_token = ""
    pages = 0
    retries = 0
    latest = str(state.get("last_received_utc") or "")
    out_items: List[Dict[str, Any]] = []
    log_event(
        logger,
        "provider_cursor_loaded",
        provider="google",
        provider_id=pid,
        start_utc=start_utc,
        page_size=page_size,
    )

    while pages < int(f.max_pages_per_run):
        try:
            listed = gmail_list_messages(
                max_results=page_size,
                provider_id=pid,
                after_epoch=after_epoch,
                page_token=next_token,
                include_next=True,
            )
            if not isinstance(listed, dict):
                listed = {"items": listed, "next_page_token": ""}
        except Exception as exc:
            if _is_rate_limit(exc) and retries < int(f.backoff_retries):
                wait_sec = min(int(f.backoff_max_seconds), int(f.backoff_initial_seconds) * (2 ** retries))
                time.sleep(max(1, wait_sec))
                page_size = max(int(f.min_results_per_page), page_size // 2)
                retries += 1
                continue
            raise

        retries = 0
        page = listed.get("items") or []
        for ref in page:
            raw = _call_with_backoff(
                lambda: gmail_get_message(ref["provider_id"], ref["gmail_id"]),
                retries=int(f.backoff_retries),
                initial_seconds=int(f.backoff_initial_seconds),
                max_seconds=int(f.backoff_max_seconds),
            )
            normalize_and_store_message(raw, provider_kind="google", raw_source=raw, provider_id=ref["provider_id"])
            msg_date = _gmail_date_utc(raw)
            latest = _date_max(latest, msg_date)
            out_items.append({"id": raw.get("id"), "subject": _gmail_subject(raw), "date_utc": msg_date})

        pages += 1
        next_token = str(listed.get("next_page_token") or "")
        if not next_token or not page:
            break

    _save_cursor(
        db,
        pid,
        {
            "last_received_utc": latest or start_utc,
            "page_size": page_size,
            "updated_at": utc_now_iso(),
        },
    )
    return {
        "provider": "google",
        "provider_id": pid,
        "count": len(out_items),
        "pages": pages,
        "items": out_items[:50],
    }


def _poll_microsoft_provider(s: Settings, db: DB, provider: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(provider.get("id") or "")
    f = s.mail.fetch
    state = _load_cursor(db, pid)
    page_size = int(state.get("page_size") or f.max_results_per_page)
    page_size = max(int(f.min_results_per_page), min(int(f.max_results_per_page), page_size))

    start_utc = _cursor_start_utc(s, provider, state)
    next_url = ""
    pages = 0
    retries = 0
    latest = str(state.get("last_received_utc") or "")
    out_items: List[Dict[str, Any]] = []
    log_event(
        logger,
        "provider_cursor_loaded",
        provider="microsoft",
        provider_id=pid,
        start_utc=start_utc,
        page_size=page_size,
    )

    while pages < int(f.max_pages_per_run):
        try:
            listed = graph_list_recent_messages(
                top=page_size,
                provider_id=pid,
                after_iso=start_utc,
                page_url=next_url,
                include_next=True,
            )
            if not isinstance(listed, dict):
                listed = {"items": listed, "next_page_url": ""}
        except Exception as exc:
            if _is_rate_limit(exc) and retries < int(f.backoff_retries):
                wait_sec = min(int(f.backoff_max_seconds), int(f.backoff_initial_seconds) * (2 ** retries))
                time.sleep(max(1, wait_sec))
                page_size = max(int(f.min_results_per_page), page_size // 2)
                retries += 1
                continue
            raise

        retries = 0
        page = listed.get("items") or []
        for ref in page:
            raw = _call_with_backoff(
                lambda: graph_get_message(ref["provider_id"], ref["graph_id"]),
                retries=int(f.backoff_retries),
                initial_seconds=int(f.backoff_initial_seconds),
                max_seconds=int(f.backoff_max_seconds),
            )
            normalize_and_store_message(raw, provider_kind="microsoft", raw_source=raw, provider_id=ref["provider_id"])
            msg_date = str(raw.get("receivedDateTime") or "")
            latest = _date_max(latest, msg_date)
            out_items.append({"id": raw.get("id"), "subject": raw.get("subject"), "date_utc": msg_date})

        pages += 1
        next_url = str(listed.get("next_page_url") or "")
        if not next_url or not page:
            break

    _save_cursor(
        db,
        pid,
        {
            "last_received_utc": latest or start_utc,
            "page_size": page_size,
            "updated_at": utc_now_iso(),
        },
    )
    return {
        "provider": "microsoft",
        "provider_id": pid,
        "count": len(out_items),
        "pages": pages,
        "items": out_items[:50],
    }


def _poll_imap_provider(s: Settings, db: DB, provider: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(provider.get("id") or "")
    f = s.mail.fetch
    state = _load_cursor(db, pid)
    max_fetch = int(state.get("page_size") or f.max_results_per_page)
    max_fetch = max(int(f.min_results_per_page), min(int(f.max_results_per_page), max_fetch))
    last_uid = int(state.get("last_uid") or 0)
    start_utc = _cursor_start_utc(s, provider, state)
    since = _since_from_start_utc(start_utc)
    log_event(
        logger,
        "provider_cursor_loaded",
        provider="imap",
        provider_id=pid,
        start_utc=start_utc,
        last_uid=last_uid,
        page_size=max_fetch,
    )

    out = fetch_and_store_recent_full(
        since=since,
        provider_id=pid,
        last_uid=last_uid,
        max_fetch=max_fetch,
    )
    saved = out.get("saved") or []
    latest = str(state.get("last_received_utc") or "")
    for x in saved:
        latest = _date_max(latest, str(x.get("date_utc") or ""))

    _save_cursor(
        db,
        pid,
        {
            "last_uid": int(out.get("max_uid_seen") or last_uid),
            "last_received_utc": latest or start_utc,
            "page_size": max_fetch,
            "updated_at": utc_now_iso(),
        },
    )
    return {
        "provider": "imap",
        "provider_id": pid,
        "count": len(saved),
        "pages": 1,
        "items": saved[:50],
    }


def _gmail_date_utc(raw: Dict[str, Any]) -> str:
    internal = str(raw.get("internalDate") or "").strip()
    if internal.isdigit():
        try:
            dt = datetime.fromtimestamp(int(internal) / 1000.0, tz=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _since_from_start_utc(start_utc: str) -> str:
    dt = _parse_iso_utc(start_utc)
    sec = int(max(60, (datetime.now(timezone.utc) - dt).total_seconds()))
    if sec < 3600:
        return f"{max(1, sec // 60)}m"
    if sec < 86400:
        return f"{max(1, sec // 3600)}h"
    return f"{max(1, sec // 86400)}d"


def _parse_iso_utc(raw: str) -> datetime:
    v = (raw or "").strip()
    if not v:
        return datetime.now(timezone.utc)
    p = v.replace("Z", "+00:00")
    dt = datetime.fromisoformat(p)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _call_with_backoff(fn, *, retries: int, initial_seconds: int, max_seconds: int):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if not _is_rate_limit(exc) or attempt >= retries:
                raise
            wait_sec = min(int(max_seconds), int(initial_seconds) * (2 ** attempt))
            time.sleep(max(1, wait_sec))
            attempt += 1
