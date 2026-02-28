from __future__ import annotations

import email.utils
import sys
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

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
            "privacy_constraints": [
                "Do not include user private data.",
                "Do not reveal any information outside the current email being replied to.",
                "Do not use data from other emails, accounts, contacts, calendar events, or billing records.",
                "If uncertain whether content is out of scope, omit it.",
            ],
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


def _reply_subject(source_subject: str) -> str:
    subj = (source_subject or "").strip()
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    return subj


def _ensure_disclosure(text: str, disclosure: str) -> str:
    body = (text or "").strip()
    if disclosure and not body.endswith(disclosure):
        body = (body.rstrip() + "\n\n" + disclosure).strip()
    return body + "\n"


def _build_draft_for_mode(
    *,
    mode: str,
    message: Dict[str, Any],
    disclosure: str,
    content: str = "",
) -> Tuple[str, str]:
    m = (mode or "auto").strip().lower()
    subject = message.get("subject") or ""
    if m == "auto":
        return _draft_reply(subject, "Thanks for your email.", disclosure, incoming=message)
    if m == "optimize":
        hint = content.strip() or "Please produce a concise, clear, empathetic reply."
        return _draft_reply(subject, hint, disclosure, incoming=message)
    if m == "raw":
        base = content.strip() or "Thanks for your email."
        return _reply_subject(subject), _ensure_disclosure(base, disclosure)
    raise ValueError("Unsupported mode. Use auto|optimize|raw.")


def _pending_item_by_id(db: DB, reply_id: int) -> Dict[str, Any]:
    item = db.get_reply_queue_item(reply_id)
    if not item:
        raise ValueError(f"Reply id not found: {reply_id}")
    if (item.get("status") or "") != "pending":
        raise ValueError(f"Reply id is not pending: {reply_id}")
    return item


def _resolve_pending_reply_target(
    db: DB, *, index: int | None = None, reply_id: int | None = None
) -> Tuple[Dict[str, Any], int]:
    pending = db.list_reply_queue(status="pending", limit=500)
    if not pending:
        raise RuntimeError("No pending replies in queue.")

    if reply_id is not None:
        for i, item in enumerate(pending, start=1):
            if int(item.get("id") or 0) == int(reply_id):
                return item, i
        raise ValueError(f"Reply id not found in pending queue: {reply_id}")

    if index is None:
        raise ValueError("Either --id or --index is required.")
    if index < 1 or index > len(pending):
        raise ValueError("Index out of range")
    return pending[index - 1], index


def reply_prepare(index: int | None = None, reply_id: int | None = None) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    item, resolved_index = _resolve_pending_reply_target(db, index=index, reply_id=reply_id)
    msg = db.get_message(item["message_id"])
    if not msg:
        raise RuntimeError("Message not found")

    # Create draft
    hint = "Thanks for your email. "  # MVP default
    subj, body = _draft_reply(msg.get("subject") or "", hint, s.disclosure_text(), incoming=msg)
    db.update_reply_draft(item["id"], subj, body, utc_now_iso())

    to_addr = _extract_reply_to(msg.get("from_addr") or "")
    return {
        "index": resolved_index,
        "id": int(item["id"]),
        "queue_id": item["id"],
        "message_id": item["message_id"],
        "selected_by": "id" if reply_id is not None else "index",
        "resolved_command": f"mailhub reply prepare --id {int(item['id'])}",
        "preview": {
            "from": (_choose_sender_for_message(db, msg.get("provider_id") or "") or {}).get("email") or "",
            "to": to_addr,
            "subject": subj,
            "body": body,
        },
        "note": "Confirm before sending: use reply send --id <ID> --confirm-text '<text with send>'.",
    }


def reply_send(
    index: int | None = None,
    *,
    reply_id: int | None = None,
    confirm_text: str,
    send_mode: str = "manual",
) -> Dict[str, Any]:
    """
    Sends the prepared draft for the index-th pending item.
    confirm_text is a safety gate (should be user-provided in chat).
    """
    if not confirm_text or "send" not in confirm_text.lower():
        raise ValueError("Confirmation text must include 'send'")

    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    rq, resolved_index = _resolve_pending_reply_target(db, index=index, reply_id=reply_id)
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
    return {
        "ok": True,
        "index": resolved_index,
        "id": int(rq["id"]),
        "selected_by": "id" if reply_id is not None else "index",
        "resolved_command": f"mailhub reply send --id {int(rq['id'])} --confirm-text \"send\"",
        "sent_via": provider["kind"],
        "to": to_addr,
        "send_mode": send_mode,
        "result": send_result,
    }


def send_queue_list(limit: int = 200) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    pending = db.list_reply_queue(status="pending", limit=limit)
    items: List[Dict[str, Any]] = []
    not_ready: List[int] = []
    for x in pending:
        rid = int(x.get("id") or 0)
        if not (x.get("drafted_subject") or "").strip() or not (x.get("drafted_body") or "").strip():
            not_ready.append(rid)
            continue
        source_title = x.get("subject") or ""
        new_title = (x.get("drafted_subject") or "").strip() or _reply_subject(source_title)
        provider = _choose_sender_for_message(db, x.get("provider_id") or "")
        sender_addr = (provider or {}).get("email") or ""
        items.append(
            {
                "id": rid,
                "new_title": new_title,
                "source_title": source_title,
                "from_address": x.get("from_addr") or "",
                "sender_address": sender_addr,
                "message_id": x.get("message_id") or "",
                "display": f"(Id: {rid}) {new_title}",
                "send_cmd": f"mailhub send --id {rid} --confirm",
            }
        )
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "not_ready_count": len(not_ready),
        "not_ready_ids": not_ready,
    }


def send_queue_send_one(reply_id: int, confirm: bool) -> Dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint": f"Run `mailhub send --id {reply_id} --confirm` to send.",
        }
    return reply_send(reply_id=reply_id, confirm_text="send")


def send_queue_send_all(confirm: bool, limit: int = 500) -> Dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint": "Run `mailhub send --list --confirm` to send all pending drafts.",
        }
    queue = send_queue_list(limit=limit)
    sent: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for item in queue.get("items", []):
        rid = int(item.get("id") or 0)
        if rid <= 0:
            continue
        try:
            sent.append(reply_send(reply_id=rid, confirm_text="send"))
        except Exception as exc:
            failed.append({"id": rid, "error": str(exc)})
    return {
        "ok": len(failed) == 0,
        "sent_count": len(sent),
        "failed_count": len(failed),
        "sent": sent,
        "failed": failed,
        "remaining": send_queue_list(limit=limit),
    }


def reply_compose(
    *,
    message_id: str,
    mode: str = "auto",
    content: str = "",
    review: bool = True,
) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    msg = db.get_message(message_id)
    if not msg:
        return {"ok": False, "reason": "message_not_found", "message_id": message_id}

    rq_id = db.enqueue_reply(message_id, "pending", "manual compose", utc_now_iso())
    subj, body = _build_draft_for_mode(mode=mode, message=msg, disclosure=s.disclosure_text(), content=content)
    db.update_reply_draft(rq_id, subj, body, utc_now_iso())

    if review and sys.stdin.isatty():
        while True:
            cur = _pending_item_by_id(db, rq_id)
            print("")
            print(f"Draft review for Id {rq_id}")
            print(f"Subject: {cur.get('drafted_subject') or ''}")
            print("a) confirm")
            print("b) optimize")
            print("c) manual edit")
            choice = input("Select [a]: ").strip().lower() or "a"
            if choice in ("a", "confirm"):
                break
            if choice in ("b", "optimize"):
                hint = input("Optimization hint: ").strip()
                subj2, body2 = _build_draft_for_mode(
                    mode="optimize",
                    message=msg,
                    disclosure=s.disclosure_text(),
                    content=hint,
                )
                db.update_reply_draft(rq_id, subj2, body2, utc_now_iso())
                continue
            if choice in ("c", "manual", "edit"):
                subj_new = input(f"New subject [{cur.get('drafted_subject') or _reply_subject(msg.get('subject') or '')}]: ").strip()
                body_new = input("New body (single-line input, disclosure auto-appended): ").strip()
                subj3 = subj_new or (cur.get("drafted_subject") or _reply_subject(msg.get("subject") or ""))
                body3 = _ensure_disclosure(body_new or (cur.get("drafted_body") or ""), s.disclosure_text())
                db.update_reply_draft(rq_id, subj3, body3, utc_now_iso())
                continue

    cur = _pending_item_by_id(db, rq_id)
    return {
        "ok": True,
        "id": rq_id,
        "message_id": message_id,
        "new_title": cur.get("drafted_subject") or "",
        "source_title": msg.get("subject") or "",
        "from_address": msg.get("from_addr") or "",
        "sender_address": (_choose_sender_for_message(db, msg.get("provider_id") or "") or {}).get("email") or "",
        "review_options": {
            "a": "confirm",
            "b": "optimize",
            "c": "manual_edit",
        },
        "next_steps": [
            f"mailhub reply revise --id {rq_id} --mode optimize --content \"<instructions>\"",
            f"mailhub reply revise --id {rq_id} --mode raw --content \"<manual body>\"",
            f"mailhub send --id {rq_id} --confirm",
        ],
        "send_queue": send_queue_list(),
    }


def reply_revise(
    *,
    reply_id: int,
    mode: str,
    content: str = "",
    review: bool = False,
) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    cur = _pending_item_by_id(db, reply_id)
    msg = db.get_message(cur["message_id"])
    if not msg:
        return {"ok": False, "reason": "message_not_found", "message_id": cur["message_id"]}

    subj, body = _build_draft_for_mode(mode=mode, message=msg, disclosure=s.disclosure_text(), content=content)
    db.update_reply_draft(reply_id, subj, body, utc_now_iso())

    if review and sys.stdin.isatty():
        return reply_compose(message_id=cur["message_id"], mode=mode, content=content, review=True)

    updated = _pending_item_by_id(db, reply_id)
    return {
        "ok": True,
        "id": reply_id,
        "mode": mode,
        "new_title": updated.get("drafted_subject") or "",
        "source_title": updated.get("subject") or "",
        "from_address": updated.get("from_addr") or "",
        "sender_address": (_choose_sender_for_message(db, updated.get("provider_id") or "") or {}).get("email") or "",
        "send_queue": send_queue_list(),
    }


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

    for rq in pending:
        # draft if missing
        if not rq.get("drafted_body"):
            msg = db.get_message(rq["message_id"]) or {}
            subj, body = _draft_reply(msg.get("subject") or "", "Thanks for your email.", s.disclosure_text(), incoming=msg)
            db.update_reply_draft(rq["id"], subj, body, utc_now_iso())
            drafted.append({"queue_id": rq["id"], "subject": subj})

        if not dry_run:
            # send with implicit confirmation (user enabled auto_reply)
            reply_send(reply_id=int(rq["id"]), confirm_text="send (auto)", send_mode="auto")

            sent.append({"queue_id": rq["id"]})

    return {"ok": True, "dry_run": dry_run, "drafted": drafted, "sent": sent}


def reply_sent_list(date: str = "today", limit: int = 50) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_reply_queue_by_message_date(day, status="sent", limit=limit)
    return {"ok": True, "day": day, "count": len(rows), "items": _indexed(rows, db)}


def reply_suggested_list(date: str = "today", limit: int = 50) -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    db = DB(Settings.load().db_path)
    db.init()
    rows = db.list_reply_queue_by_message_date(day, status="pending", limit=limit)
    return {"ok": True, "day": day, "count": len(rows), "items": _indexed(rows, db)}


def reply_center(date: str = "today") -> Dict[str, Any]:
    day = today_yyyy_mm_dd_utc() if date == "today" else date
    if not sys.stdin.isatty():
        return {
            "ok": False,
            "reason": "interactive_tty_required",
            "menu": {
                "1": "show sent list",
                "2": "show suggested-not-replied list",
                "3": "prepare reply for suggested item (id preferred)",
            },
            "next_steps": [
                f"mailhub reply sent-list --date {day}",
                f"mailhub reply suggested-list --date {day}",
                "mailhub reply prepare --id <ID>",
                "mailhub reply prepare --index <N>",
                "mailhub send --id <ID> --confirm",
            ],
        }

    print("")
    print("Reply center")
    print(f"Date: {day}")
    print("1) Sent list")
    print("2) Suggested not replied list")
    print("3) Prepare reply for suggested item (id preferred)")
    choice = input("Select [1]: ").strip() or "1"
    if choice == "1":
        return reply_sent_list(date=day)
    if choice == "2":
        return reply_suggested_list(date=day)
    if choice == "3":
        suggested = reply_suggested_list(date=day).get("items", [])
        if not suggested:
            return {"ok": False, "message": "No suggested-not-replied items for selected day."}
        rid = input("Reply id to prepare (press Enter to use index): ").strip()
        if rid:
            return reply_prepare(reply_id=int(rid))
        idx = input("Suggested index to prepare [1]: ").strip() or "1"
        return reply_prepare(index=int(idx))
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


def _indexed(items: list[Dict[str, Any]], db: DB) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for i, x in enumerate(items, start=1):
        item_id = int(x.get("id") or 0)
        title = x.get("subject") or ""
        sender_address = (
            _choose_sender_for_message(db, x.get("provider_id") or "") or {}
        ).get("email") or ""
        out.append(
            {
                "index": i,
                "id": item_id,
                "queue_id": x.get("id"),
                "message_id": x.get("message_id"),
                "title": title,
                "from": x.get("from_addr") or "",
                "from_address": x.get("from_addr") or "",
                "sender_address": sender_address,
                "subject": title,
                "status": x.get("status") or "",
                "send_mode": x.get("send_mode") or "",
                "display": f"index {i}. (Id: {item_id}) {title}",
                "prepare_cmd": f"mailhub reply prepare --id {item_id}",
                "send_cmd": f"mailhub send --id {item_id} --confirm",
            }
        )
    return out
