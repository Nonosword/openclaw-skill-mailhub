from __future__ import annotations

import email.utils
import sys
from email.message import EmailMessage
from typing import Any, Dict, Optional, Tuple

from ..agent_bridge import draft_reply_with_agent
from ..config import Settings
from ..store import DB
from ..utils.time import utc_now_iso, today_yyyy_mm_dd_utc
from ..providers.imap_smtp import send_email as imap_send
from ..providers.google_gmail import gmail_send
from ..providers.ms_graph import graph_send_mail


def _choose_sender_provider(db: DB) -> Optional[Dict[str, Any]]:
    providers = db.list_providers()
    # Prefer google/microsoft if available
    for kind in ("google", "microsoft", "imap"):
        for p in providers:
            if p["kind"] == kind:
                return p
    return providers[0] if providers else None


def _choose_sender_for_message(db: DB, msg_provider_id: str) -> Optional[Dict[str, Any]]:
    providers = db.list_providers()
    # try exact provider_id match
    for p in providers:
        if p["id"] == msg_provider_id:
            return p
    # fallback by kind prefix
    if msg_provider_id.startswith("google:"):
        for p in providers:
            if p["kind"] == "google":
                return p
    if msg_provider_id.startswith("microsoft:"):
        for p in providers:
            if p["kind"] == "microsoft":
                return p
    if msg_provider_id.startswith("imap:"):
        for p in providers:
            if p["kind"] == "imap":
                return p
    # final fallback: original priority
    return _choose_sender_provider(db)


def _draft_reply(subject: str, body_hint: str, disclosure: str, incoming: Dict[str, Any] | None = None) -> Tuple[str, str]:
    incoming = incoming or {}
    agent_out = draft_reply_with_agent(
        {
            "incoming_email": {
                "subject": incoming.get("subject") or subject,
                "from": incoming.get("from_addr") or "",
                "body_text": incoming.get("body_text") or "",
                "snippet": incoming.get("snippet") or "",
            },
            "hint": body_hint,
            "must_append_disclosure": disclosure,
        }
    )
    if agent_out:
        a_subj = str(agent_out.get("subject") or "").strip()
        a_body = str(agent_out.get("body") or "").strip()
        if a_subj and a_body:
            if not a_body.endswith(disclosure):
                a_body = (a_body.rstrip() + "\n\n" + disclosure).strip()
            return a_subj, a_body + "\n"

    # Fallback: rule-based draft.
    subj = subject.strip()
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    body = (body_hint.strip() + "\n\n" + disclosure + "\n").strip() + "\n"
    return subj, body


def reply_prepare(index: int) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    pending = db.list_reply_queue(status="pending", limit=200)
    if index < 1 or index > len(pending):
        raise ValueError("Index out of range")

    item = pending[index - 1]
    msg = db.get_message(item["message_id"])
    if not msg:
        raise RuntimeError("Message not found")

    # Create draft
    hint = "Thanks for your email. "  # MVP default
    subj, body = _draft_reply(msg.get("subject") or "", hint, s.disclosure_text(), incoming=msg)
    db.update_reply_draft(item["id"], subj, body, utc_now_iso())

    to_addr = _extract_reply_to(msg.get("from_addr") or "")
    return {
        "queue_id": item["id"],
        "message_id": item["message_id"],
        "preview": {
            "from": (_choose_sender_for_message(db, msg.get("provider_id") or "") or {}).get("email") or "",
            "to": to_addr,
            "subject": subj,
            "body": body,
        },
        "note": "Confirm before sending: reply send --confirm-text must include 'send'.",
    }


def reply_send(index: int, confirm_text: str, send_mode: str = "manual") -> Dict[str, Any]:
    """
    Sends the prepared draft for the index-th pending item.
    confirm_text is a safety gate (should be user-provided in chat).
    """
    if not confirm_text or "send" not in confirm_text.lower():
        raise ValueError("Confirmation text must include 'send'")

    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    pending = db.list_reply_queue(status="pending", limit=200)
    if index < 1 or index > len(pending):
        raise ValueError("Index out of range")

    rq = pending[index - 1]
    if not rq.get("drafted_body"):
        raise RuntimeError("Draft not prepared. Run reply prepare first.")

    msg = db.get_message(rq["message_id"])
    if not msg:
        raise RuntimeError("Message not found")

    to_addr = _extract_reply_to(msg.get("from_addr") or "")
    provider = _choose_sender_for_message(db, msg.get("provider_id") or "")
    if not provider:
        raise RuntimeError("No provider configured to send")

    # Pick a from address if possible
    from_addr = provider.get("email") or ""
    subject = rq["drafted_subject"]
    body = rq["drafted_body"]

    send_result: Dict[str, Any]
    if provider["kind"] == "imap":
        send_result = imap_send(from_addr=from_addr, to_addr=to_addr, subject=subject, body=body)
    elif provider["kind"] == "google":
        raw = _build_rfc822(from_addr, to_addr, subject, body)
        send_result = gmail_send(provider["id"], raw)
    elif provider["kind"] == "microsoft":
        send_result = graph_send_mail(provider["id"], to_addr=to_addr, subject=subject, body_text=body)
    else:
        raise RuntimeError(f"Unsupported sender provider: {provider['kind']}")

    db.mark_reply_status(rq["id"], "sent", utc_now_iso(), send_mode=send_mode)
    return {"ok": True, "sent_via": provider["kind"], "to": to_addr, "send_mode": send_mode, "result": send_result}


def reply_auto(since: str = "15m", dry_run: bool = True) -> Dict[str, Any]:
    """
    MVP auto-reply:
    - Only drafts and (optionally) sends for pending items
    - Respects Settings.toggles.auto_reply
    """
    s = Settings.load()
    if s.toggles.auto_reply != "on":
        return {"ok": True, "auto_reply": "off"}

    db = DB(s.db_path)
    db.init()

    pending = db.list_reply_queue(status="pending", limit=50)
    sent = []
    drafted = []

    for i, rq in enumerate(pending, start=1):
        # draft if missing
        if not rq.get("drafted_body"):
            msg = db.get_message(rq["message_id"]) or {}
            subj, body = _draft_reply(msg.get("subject") or "", "Thanks for your email.", s.disclosure_text(), incoming=msg)
            db.update_reply_draft(rq["id"], subj, body, utc_now_iso())
            drafted.append({"queue_id": rq["id"], "subject": subj})

        if not dry_run:
            # send with implicit confirmation (user enabled auto_reply)
            reply_send(index=i, confirm_text="send (auto)", send_mode="auto")

            sent.append({"queue_id": rq["id"]})

    return {"ok": True, "dry_run": dry_run, "drafted": drafted, "sent": sent}


def reply_sent_list(date: str = "today", limit: int = 50) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_reply_queue_by_message_date(day, status="sent", limit=limit)
    return {"ok": True, "day": day, "count": len(rows), "items": _indexed(rows)}


def reply_suggested_list(date: str = "today", limit: int = 50) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_reply_queue_by_message_date(day, status="pending", limit=limit)
    return {"ok": True, "day": day, "count": len(rows), "items": _indexed(rows)}


def reply_center(date: str = "today") -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    if not sys.stdin.isatty():
        return {
            "ok": False,
            "reason": "interactive_tty_required",
            "menu": {
                "1": "show sent list",
                "2": "show suggested-not-replied list",
                "3": "prepare reply for suggested index",
            },
            "next_steps": [
                f"mailhub reply sent-list --date {day}",
                f"mailhub reply suggested-list --date {day}",
                "mailhub reply prepare --index <N>",
            ],
        }

    print("")
    print("Reply center")
    print(f"Date: {day}")
    print("1) Sent list")
    print("2) Suggested not replied list")
    print("3) Prepare reply for suggested index")
    choice = input("Select [1]: ").strip() or "1"
    if choice == "1":
        return reply_sent_list(date=day)
    if choice == "2":
        return reply_suggested_list(date=day)
    if choice == "3":
        suggested = reply_suggested_list(date=day).get("items", [])
        if not suggested:
            return {"ok": False, "message": "No suggested-not-replied items for selected day."}
        idx = input("Suggested index to prepare [1]: ").strip() or "1"
        return reply_prepare(int(idx))
    return {"ok": True, "message": "No action"}


def _build_rfc822(from_addr: str, to_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body)
    return msg.as_bytes()


def _extract_reply_to(from_header: str) -> str:
    name, addr = email.utils.parseaddr(from_header)
    return addr or from_header


def _indexed(items: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
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
