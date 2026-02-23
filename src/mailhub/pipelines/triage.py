from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..config import Settings
from ..store import DB
from ..utils.time import utc_now_iso, today_yyyy_mm_dd_utc
from ..utils.html import html_to_text


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _header_map(headers: List[Dict[str, str]]) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for h in headers:
        if "name" in h and "value" in h:
            m[h["name"].lower()] = h["value"]
    return m


def normalize_and_store_message(raw: Dict[str, Any], provider_kind: str, raw_source: Any, provider_id: Optional[str] = None) -> str:
    """
    Normalize different provider payloads into DB messages table.
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    now = utc_now_iso()

    if provider_kind == "google":
        pid = provider_id or "google:me"
        msg_id = f"google:{pid}:{raw.get('id')}"
        payload = raw.get("payload", {})
        headers = _header_map(payload.get("headers", []))
        subject = headers.get("subject", "")
        from_ = headers.get("from", "")
        to_ = headers.get("to", "")
        date_utc = _parse_google_date(headers.get("date")) or now[:10] + "T00:00:00Z"
        snippet = raw.get("snippet", "") or ""
        body_text, body_html = _extract_gmail_bodies(payload)
        db.upsert_message(
            {
                "id": msg_id,
                "provider_id": pid,
                "thread_id": raw.get("threadId"),
                "from_addr": from_,
                "to_addrs": to_,
                "subject": subject,
                "date_utc": date_utc,
                "snippet": snippet[:500],
                "body_text": body_text,
                "body_html": body_html,
                "has_attachments": int(_gmail_has_attachments(payload)),
                "raw_json": json.dumps(raw)[:2_000_000],
                "created_at": now,
            }
        )
        return msg_id

    if provider_kind == "microsoft":
        pid = provider_id or "microsoft:me"
        msg_id = f"microsoft:{pid}:{raw.get('id')}"
        from_ = (raw.get("from") or {}).get("emailAddress", {}).get("address", "")
        to_list = [(x.get("emailAddress") or {}).get("address", "") for x in (raw.get("toRecipients") or [])]
        to_ = ", ".join([t for t in to_list if t])
        subject = raw.get("subject") or ""
        date_utc = (raw.get("receivedDateTime") or "").replace("+00:00", "Z") or now[:10] + "T00:00:00Z"
        snippet = raw.get("bodyPreview") or ""
        body = raw.get("body") or {}
        body_html = body.get("content") if body.get("contentType") == "html" else None
        body_text = body.get("content") if body.get("contentType") == "text" else None
        if body_html and not body_text:
            body_text = html_to_text(body_html)

        db.upsert_message(
            {
                "id": msg_id,
                "provider_id": pid,
                "thread_id": raw.get("conversationId"),
                "from_addr": from_,
                "to_addrs": to_,
                "subject": subject,
                "date_utc": date_utc,
                "snippet": snippet[:500],
                "body_text": body_text,
                "body_html": body_html,
                "has_attachments": 0,
                "raw_json": json.dumps(raw)[:2_000_000],
                "created_at": now,
            }
        )
        return msg_id

    # IMAP (full message preferred when available)
    if provider_kind == "imap":
        pid = provider_id or raw.get("provider_id", "imap:unknown")
        msg_id = raw.get("id")
        db.upsert_message(
            {
                "id": msg_id,
                "provider_id": pid,
                "thread_id": raw.get("thread_id"),
                "from_addr": raw.get("from_addr"),
                "to_addrs": raw.get("to_addrs"),
                "subject": raw.get("subject"),
                "date_utc": raw.get("date_utc") or now[:10] + "T00:00:00Z",
                "snippet": (raw.get("snippet") or "")[:500],
                "body_text": raw.get("body_text"),
                "body_html": raw.get("body_html"),
                "has_attachments": int(raw.get("has_attachments", 0)),
                "raw_json": json.dumps(raw_source)[:500_000],
                "created_at": now,
            }
        )
        return msg_id

    raise ValueError(f"Unsupported provider_kind={provider_kind}")


def _parse_google_date(date_header: Optional[str]) -> Optional[str]:
    if not date_header:
        return None
    # Keep MVP simple: store day as UTC ISO prefix; exact parsing is messy across formats.
    # If you want strict parsing, plug dateutil.parser here.
    return None


def _extract_gmail_bodies(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    def walk(part: Dict[str, Any], out: Dict[str, str]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime in ("text/plain", "text/html"):
            import base64
            raw = base64.urlsafe_b64decode(data.encode("ascii") + b"==")
            out[mime] = raw.decode("utf-8", errors="replace")
        for p in part.get("parts", []) or []:
            walk(p, out)

    out: Dict[str, str] = {}
    walk(payload, out)
    text = out.get("text/plain")
    html = out.get("text/html")
    if html and not text:
        text = html_to_text(html)
    return text, html


def _gmail_has_attachments(payload: Dict[str, Any]) -> bool:
    def walk(part: Dict[str, Any]) -> bool:
        body = part.get("body", {}) or {}
        if body.get("attachmentId"):
            return True
        for p in part.get("parts", []) or []:
            if walk(p):
                return True
        return False
    return walk(payload)


# ---------- rules engine ----------
def _match_rule(rule: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    # Supported predicates:
    # header_regex, subject_regex, body_regex, from_regex, to_domain_regex, list_unsubscribe_present, from_in_contacts
    if "header_regex" in rule:
        pat = re.compile(rule["header_regex"])
        headers = ctx.get("headers_text", "")
        if not pat.search(headers):
            return False
    if "subject_regex" in rule:
        pat = re.compile(rule["subject_regex"])
        if not pat.search(ctx.get("subject", "") or ""):
            return False
    if "body_regex" in rule:
        pat = re.compile(rule["body_regex"])
        if not pat.search(ctx.get("body_text", "") or ""):
            return False
    if "from_regex" in rule:
        pat = re.compile(rule["from_regex"])
        if not pat.search(ctx.get("from_addr", "") or ""):
            return False
    if "to_domain_regex" in rule:
        pat = re.compile(rule["to_domain_regex"])
        if not pat.search(ctx.get("to_addrs", "") or ""):
            return False
    if "list_unsubscribe_present" in rule:
        want = bool(rule["list_unsubscribe_present"])
        have = "list-unsubscribe:" in (ctx.get("headers_text", "").lower())
        if want != have:
            return False
    if "from_in_contacts" in rule:
        # MVP: no contacts integration. Always False.
        if bool(rule["from_in_contacts"]) is True:
            return False
    return True


def _eval_any_all(block: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    if "any" in block:
        return any(_match_rule(r, ctx) for r in block["any"])
    if "all" in block:
        return all(_match_rule(r, ctx) for r in block["all"])
    return False


def classify_message(msg: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[str, float, str]:
    labels = rules.get("labels", {})
    subject = msg.get("subject") or ""
    body_text = msg.get("body_text") or ""
    headers_text = (msg.get("raw_json") or "")[:10_000]  # best effort
    ctx = {
        "subject": subject,
        "body_text": body_text,
        "from_addr": msg.get("from_addr") or "",
        "to_addrs": msg.get("to_addrs") or "",
        "headers_text": headers_text,
    }

    for label, block in labels.items():
        if _eval_any_all(block, ctx):
            return label, 1.0, f"Matched rule for {label}"
    return rules.get("default", "other"), 0.5, "Default label"


def is_reply_needed(msg: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[bool, str]:
    ctx = {
        "subject": msg.get("subject") or "",
        "body_text": msg.get("body_text") or "",
        "from_addr": msg.get("from_addr") or "",
        "headers_text": (msg.get("raw_json") or "")[:10_000],
    }
    if _eval_any_all(rules.get("suppress_if", {}), ctx):
        return False, "Suppressed by rule"
    if _eval_any_all(rules.get("reply_needed", {}), ctx):
        return True, "Matched reply-needed rule"
    return False, "No match"


def triage_day(date: str = "today") -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    day = today_yyyy_mm_dd_utc() if date == "today" else date

    tags_rules = _load_yaml(Path("config/rules.email_tags.yml"))
    reply_rules = _load_yaml(Path("config/rules.reply_needed.yml"))

    messages = db.get_messages_by_date(day)

    reply_items: List[Dict[str, Any]] = []
    for m in messages:
        tag, score, reason = classify_message(m, tags_rules)
        db.set_message_tag(m["id"], tag, score, reason, utc_now_iso())

        need, why = is_reply_needed(m, reply_rules)
        if need:
            rq_id = db.enqueue_reply(m["id"], "pending", why, utc_now_iso())
            reply_items.append(
                {
                    "queue_id": rq_id,
                    "message_id": m["id"],
                    "from": m.get("from_addr"),
                    "subject": m.get("subject"),
                    "why": why,
                }
            )

    counts = db.list_tag_counts_for_date(day)
    overview = _overview_by_tag(messages, db)

    return {
        "day": day,
        "total": len(messages),
        "tag_counts": counts,
        "overview": overview,
        "reply_needed": reply_items[: s.toggles.reply_needed_max_items],
    }


def _overview_by_tag(messages: List[Dict[str, Any]], db: DB) -> Dict[str, str]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for m in messages:
        tags = db.get_tags_for_message(m["id"])
        tag = tags[0]["tag"] if tags else "other"
        buckets.setdefault(tag, []).append(m)

    out: Dict[str, str] = {}
    for tag, items in buckets.items():
        # Natural short overview per label (rule-based MVP)
        subjects = [it.get("subject") or "" for it in items if it.get("subject")]
        top = "; ".join(subjects[:5])
        out[tag] = f"{len(items)} items. Examples: {top}" if top else f"{len(items)} items."
    return out


def triage_suggest(since: str = "15m") -> Dict[str, Any]:
    """
    Suggested mail: exclude ads/spam by tag.
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    # Ensure recent messages are tagged: run triage on today (cheap)
    triage_day("today")

    # Collect from last window by using stored messages + simplistic filter
    # Take today's messages and filter by tags
    day = today_yyyy_mm_dd_utc()
    messages = db.get_messages_by_date(day)

    suggested: List[Dict[str, Any]] = []
    for m in messages:
        tags = db.get_tags_for_message(m["id"])
        tag = tags[0]["tag"] if tags else "other"
        if tag in ("spam", "ads"):
            continue
        suggested.append(
            {
                "message_id": m["id"],
                "from": m.get("from_addr"),
                "subject": m.get("subject"),
                "tag": tag,
            }
        )

    suggested = suggested[: s.toggles.suggest_max_items]
    return {"day": day, "suggested": suggested}