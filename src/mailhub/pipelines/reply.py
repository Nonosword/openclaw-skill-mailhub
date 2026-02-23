from __future__ import annotations

import email.utils
from email.message import EmailMessage
from typing import Any, Dict, Optional, Tuple

from ..config import Settings
from ..store import DB
from ..utils.time import utc_now_iso
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


def _draft_reply(subject: str, body_hint: str, disclosure: str) -> Tuple[str, str]:
    # MVP: rule-based drafting. Replace with LLM prompt later.
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
    subj, body = _draft_reply(msg.get("subject") or "", hint, s.disclosure_text())
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


def reply_send(index: int, confirm_text: str) -> Dict[str, Any]:
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

    db.mark_reply_status(rq["id"], "sent", utc_now_iso())
    return {"ok": True, "sent_via": provider["kind"], "to": to_addr, "result": send_result}


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
            subj, body = _draft_reply(msg.get("subject") or "", "Thanks for your email.", s.disclosure_text())
            db.update_reply_draft(rq["id"], subj, body, utc_now_iso())
            drafted.append({"queue_id": rq["id"], "subject": subj})

        if not dry_run:
            # send with implicit confirmation (user enabled auto_reply)
            reply_send(index=i, confirm_text="send (auto)")

            sent.append({"queue_id": rq["id"]})

    return {"ok": True, "dry_run": dry_run, "drafted": drafted, "sent": sent}


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