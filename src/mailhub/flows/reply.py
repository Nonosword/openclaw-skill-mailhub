from __future__ import annotations

import email.utils
import sys
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from ..core.agent_bridge import draft_reply_with_agent
from ..core.config import Settings
from ..core.logging import get_logger, log_event
from ..core.store import DB
from ..shared.time import utc_now_iso, today_yyyy_mm_dd_utc
from ..connectors.providers.imap_smtp import send_email as imap_send
from ..connectors.providers.google_gmail import gmail_send
from ..connectors.providers.ms_graph import graph_send_mail

OPENCLAW_MESSAGE_CONTEXT_SUFFIX = "\n\n\n<this reply is auto genertated by Mailhub skill>"
logger = get_logger(__name__)


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


def _draft_reply(subject: str, body_hint: str, disclosure: str = "", incoming: Dict[str, Any] | None = None) -> Tuple[str, str]:
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
            if disclosure and not a_body.endswith(disclosure):
                a_body = (a_body.rstrip() + "\n\n" + disclosure).strip()
            return a_subj, a_body + "\n"

    # Fallback: rule-based draft.
    subj = subject.strip()
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    body = body_hint.strip()
    if disclosure:
        body = (body + "\n\n" + disclosure).strip()
    body = body.strip() + "\n"
    return subj, body


def _reply_subject(source_subject: str) -> str:
    subj = (source_subject or "").strip()
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    return subj


def _normalize_send_message_payload(raw: Dict[str, Any]) -> Dict[str, str]:
    payload = {
        str(k).strip().lower(): ("" if v is None else str(v).strip())
        for k, v in (raw or {}).items()
    }
    unknown = [k for k in payload.keys() if k not in ("subject", "to", "from", "context")]
    if unknown:
        raise ValueError(
            "Invalid --message payload. Allowed keys only: subject, to, from, context. "
            f"Unexpected: {', '.join(unknown)}"
        )
    out = {
        "subject": payload.get("subject", ""),
        "to": payload.get("to", ""),
        "from": payload.get("from", ""),
        "context": payload.get("context", ""),
    }
    if not out["context"]:
        raise ValueError(
            "Invalid --message payload. `context` is required and cannot be empty."
        )
    return out


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
        return _draft_reply(subject, hint, incoming=message)
    if m == "raw":
        base = content.strip() or "Thanks for your email."
        return _reply_subject(subject), base.strip() + "\n"
    raise ValueError("Unsupported mode. Use auto|optimize|raw.")


def _send_cmd_for_mode(reply_id: int, mode: str) -> str:
    if (mode or "").strip().lower() == "openclaw":
        return (
            f"mailhub send --id {reply_id} --confirm --message "
            "'{\"Subject\":\"<subject>\",\"to\":\"<to>\",\"from\":\"<from>\",\"context\":\"<context>\"}'"
        )
    return f"mailhub send --id {reply_id} --confirm --bypass-message"


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
    mode = s.effective_mode()

    item, resolved_index = _resolve_pending_reply_target(db, index=index, reply_id=reply_id)
    msg = db.get_message(item["message_id"])
    if not msg:
        raise RuntimeError("Message not found")

    # Create draft
    hint = "Thanks for your email. "  # MVP default
    subj, body = _draft_reply(msg.get("subject") or "", hint, s.disclosure_text(), incoming=msg)
    db.update_reply_draft(item["id"], subj, body, utc_now_iso())
    log_event(
        logger,
        "reply_prepare_draft_updated",
        reply_id=int(item["id"]),
        message_id=str(item["message_id"]),
        selected_by=("id" if reply_id is not None else "index"),
    )

    to_addr = _extract_reply_to(msg.get("from_addr") or "")
    return {
        "index": resolved_index,
        "id": int(item["id"]),
        "queue_id": item["id"],
        "message_id": item["message_id"],
        "selected_by": "id" if reply_id is not None else "index",
        "resolved_command": f"mailhub mail reply prepare --id {int(item['id'])}",
        "preview": {
            "from": (_choose_sender_for_message(db, msg.get("provider_id") or "") or {}).get("email") or "",
            "to": to_addr,
            "subject": subj,
            "body": body,
        },
        "send_cmd": _send_cmd_for_mode(int(item["id"]), mode),
        "note": "Confirm before sending: use mail reply send --id <ID> --confirm-text '<text with send>'.",
    }


def reply_send(
    index: int | None = None,
    *,
    reply_id: int | None = None,
    confirm_text: str,
    send_mode: str = "manual",
    message_payload: Dict[str, Any] | None = None,
    bypass_message: bool = False,
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
    mode = s.effective_mode()

    if send_mode != "auto":
        if bypass_message:
            if mode != "standalone":
                raise ValueError("--bypass-message is only allowed in standalone mode.")
        elif not message_payload:
            raise ValueError(
                "Manual send requires --message JSON payload. "
                'Use `--message \'{"Subject":"...","to":"...","from":"...","context":"..."}\'`, '
                "or use --bypass-message only in standalone mode."
            )

    rq, resolved_index = _resolve_pending_reply_target(db, index=index, reply_id=reply_id)
    msg = db.get_message(rq["message_id"])
    if not msg:
        raise RuntimeError("Message not found")

    provider = _choose_sender_for_message(db, msg.get("provider_id") or "")
    if not provider:
        raise RuntimeError("No provider configured to send")

    default_to = _extract_reply_to(msg.get("from_addr") or "")
    default_from = provider.get("email") or ""
    default_subject = (rq.get("drafted_subject") or "").strip() or _reply_subject(msg.get("subject") or "")

    normalized_message: Dict[str, str] | None = None
    if message_payload:
        normalized_message = _normalize_send_message_payload(message_payload)
        to_addr = normalized_message["to"] or default_to
        from_addr = normalized_message["from"] or default_from
        subject = normalized_message["subject"] or default_subject
        body = normalized_message["context"].rstrip() + OPENCLAW_MESSAGE_CONTEXT_SUFFIX + "\n"
        db.update_reply_draft(rq["id"], subject, body, utc_now_iso())
        rq = _pending_item_by_id(db, int(rq["id"]))
        subject = rq["drafted_subject"]
        body = rq["drafted_body"]
    else:
        if not rq.get("drafted_body"):
            raise RuntimeError("Draft not prepared. Run reply prepare first.")
        to_addr = default_to
        from_addr = default_from
        subject = rq["drafted_subject"]
        body = rq["drafted_body"]

    if not to_addr:
        raise RuntimeError("Missing recipient address. Provide `to` in --message.")
    if not from_addr:
        raise RuntimeError("Missing sender address. Provide `from` in --message.")
    if not subject.strip():
        raise RuntimeError("Missing subject. Provide `Subject` in --message.")
    if not body.strip():
        raise RuntimeError("Missing email body content.")

    log_event(
        logger,
        "reply_send_attempt",
        reply_id=int(rq["id"]),
        message_id=str(rq.get("message_id") or ""),
        provider_kind=str(provider.get("kind") or ""),
        to=to_addr,
        from_addr=from_addr,
        subject_len=len(subject or ""),
        send_mode=send_mode,
        message_source=("message_payload" if normalized_message else "stored_draft"),
    )
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
    log_event(
        logger,
        "reply_send_success",
        reply_id=int(rq["id"]),
        provider_kind=str(provider.get("kind") or ""),
        to=to_addr,
        send_mode=send_mode,
    )
    return {
        "ok": True,
        "index": resolved_index,
        "id": int(rq["id"]),
        "selected_by": "id" if reply_id is not None else "index",
        "resolved_command": f"mailhub mail reply send --id {int(rq['id'])} --confirm-text \"send\"",
        "sent_via": provider["kind"],
        "to": to_addr,
        "from": from_addr,
        "send_mode": send_mode,
        "message_source": "message_payload" if normalized_message else "stored_draft",
        "result": send_result,
    }


def send_queue_list(limit: int = 200) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    mode = s.effective_mode()
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
                "send_cmd": _send_cmd_for_mode(rid, mode),
            }
        )
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "not_ready_count": len(not_ready),
        "not_ready_ids": not_ready,
    }


def send_queue_send_one(
    reply_id: int,
    confirm: bool,
    *,
    message_payload: Dict[str, Any] | None = None,
    bypass_message: bool = False,
) -> Dict[str, Any]:
    if not confirm:
        mode = Settings.load().effective_mode()
        hint_cmd = _send_cmd_for_mode(reply_id, mode)
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint": f"Run `{hint_cmd}` to send.",
        }
    out = reply_send(
        reply_id=reply_id,
        confirm_text="send",
        message_payload=message_payload,
        bypass_message=bypass_message,
    )
    log_event(
        logger,
        "send_queue_send_one_done",
        reply_id=reply_id,
        ok=bool(out.get("ok", False)),
    )
    return out


def send_queue_send_all(confirm: bool, limit: int = 500, *, bypass_message: bool = False) -> Dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint": "Run `mailhub send --list --confirm --bypass-message` to send all pending drafts in standalone mode.",
        }
    mode = Settings.load().effective_mode()
    if not bypass_message:
        return {
            "ok": False,
            "reason": "message_required",
            "hint": (
                "Bulk send requires --bypass-message, or send one-by-one with "
                "`mailhub send --id <Id> --confirm --message '{\"Subject\":\"...\",\"to\":\"...\",\"from\":\"...\",\"context\":\"...\"}'`."
            ),
        }
    if mode != "standalone":
        return {
            "ok": False,
            "reason": "bypass_not_allowed",
            "hint": "--bypass-message is only allowed in standalone mode.",
        }
    queue = send_queue_list(limit=limit)
    log_event(
        logger,
        "send_queue_send_all_start",
        pending_count=int(queue.get("count") or 0),
        limit=limit,
    )
    sent: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for item in queue.get("items", []):
        rid = int(item.get("id") or 0)
        if rid <= 0:
            continue
        try:
            sent.append(reply_send(reply_id=rid, confirm_text="send", bypass_message=True))
        except Exception as exc:
            failed.append({"id": rid, "error": str(exc)})
            log_event(
                logger,
                "send_queue_send_all_item_error",
                level="error",
                reply_id=rid,
                error=str(exc),
            )
    log_event(
        logger,
        "send_queue_send_all_done",
        sent_count=len(sent),
        failed_count=len(failed),
    )
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
    resolved_message_id = db.resolve_message_id(message_id)
    if not resolved_message_id:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}
    msg = db.get_message(resolved_message_id)
    if not msg:
        return {"ok": False, "reason": "message_not_found", "message_ref": message_id}

    rq_id = db.enqueue_reply(resolved_message_id, "pending", "manual compose", utc_now_iso())
    subj, body = _build_draft_for_mode(mode=mode, message=msg, disclosure=s.disclosure_text(), content=content)
    db.update_reply_draft(rq_id, subj, body, utc_now_iso())
    log_event(
        logger,
        "reply_compose_draft_created",
        reply_id=rq_id,
        message_id=resolved_message_id,
        mode=mode,
    )

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
                body_new = input("New body (single-line input): ").strip()
                subj3 = subj_new or (cur.get("drafted_subject") or _reply_subject(msg.get("subject") or ""))
                body3 = (body_new or (cur.get("drafted_body") or "")).rstrip() + "\n"
                db.update_reply_draft(rq_id, subj3, body3, utc_now_iso())
                continue

    cur = _pending_item_by_id(db, rq_id)
    return {
        "ok": True,
        "id": rq_id,
        "mailhub_id": int(msg.get("mail_id") or 0),
        "mail_id": int(msg.get("mail_id") or 0),
        "message_id": resolved_message_id,
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
            f"mailhub mail reply revise --id {rq_id} --mode optimize --content \"<instructions>\"",
            f"mailhub mail reply revise --id {rq_id} --mode raw --content \"<manual body>\"",
            _send_cmd_for_mode(rq_id, s.effective_mode()),
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
    - Respects Settings.mail.auto_reply
    """
    s = Settings.load()
    if s.mail.auto_reply != "on":
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

    log_event(
        logger,
        "reply_auto_done",
        since=since,
        dry_run=dry_run,
        drafted_count=len(drafted),
        sent_count=len(sent),
    )
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
    mode = Settings.load().effective_mode()
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
                f"mailhub mail reply sent-list --date {day}",
                f"mailhub mail reply suggested-list --date {day}",
                "mailhub mail reply prepare --id <ID>",
                "mailhub mail reply prepare --index <N>",
                _send_cmd_for_mode(111, mode).replace("111", "<ID>"),
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
    mode = Settings.load().effective_mode()
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
                "prepare_cmd": f"mailhub mail reply prepare --id {item_id}",
                "send_cmd": _send_cmd_for_mode(item_id, mode),
            }
        )
    return out
