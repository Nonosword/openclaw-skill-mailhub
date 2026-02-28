from __future__ import annotations

import email
import imaplib
import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..config import Settings
from ..security import SecretStore
from ..store import DB
from ..utils.time import utc_now_iso, parse_since


def _decode_mime_words(s: str | None) -> str:
    if not s:
        return ""
    parts = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            parts.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return "".join(parts)


@dataclass
class IMAPConfig:
    email: str
    imap_host: str
    smtp_host: str
    imap_port: int = 993
    smtp_port: int = 587
    smtp_starttls: bool = True


def _imap_cfg_from_meta(meta_json: str) -> IMAPConfig:
    raw = json.loads(meta_json)
    return IMAPConfig(
        email=raw["email"],
        imap_host=raw["imap_host"],
        smtp_host=raw["smtp_host"],
        imap_port=int(raw.get("imap_port", 993)),
        smtp_port=int(raw.get("smtp_port", 587)),
        smtp_starttls=bool(raw.get("smtp_starttls", True)),
    )


def auth_imap(
    email: str,
    imap_host: str,
    smtp_host: str,
    *,
    alias: str = "",
    is_mail: bool = True,
    is_calendar: bool = False,
    is_contacts: bool = False,
) -> None:
    """
    Store IMAP/SMTP config; prompt for app password locally and store in secrets.
    """
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    import getpass
    app_password = getpass.getpass(f"Enter app password for {email} (IMAP/SMTP): ")

    cfg = IMAPConfig(email=email, imap_host=imap_host, smtp_host=smtp_host)
    pid = f"imap:{email}"
    meta = json.dumps(
        {
            **cfg.__dict__,
            "alias": alias.strip(),
            "client_id": "",
            "oauth_scopes": [],
            "oauth_token_ref": "",
            "password_ref": f"{pid}:password",
            "is_mail": bool(is_mail),
            "is_calendar": bool(is_calendar),
            "is_contacts": bool(is_contacts),
            "status": "configured",
        }
    )

    SecretStore(s.secrets_path).set(f"{pid}:password", app_password)
    db.upsert_provider(pid=pid, kind="imap", email=email, meta_json=meta, created_at=utc_now_iso())


def _imap_connect(cfg: IMAPConfig, password: str) -> imaplib.IMAP4_SSL:
    im = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    im.login(cfg.email, password)
    return im


def _smtp_connect(cfg: IMAPConfig, password: str) -> smtplib.SMTP:
    smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    if cfg.smtp_starttls:
        smtp.starttls()
    smtp.login(cfg.email, password)
    return smtp


def list_recent_headers(since: str = "15m", mailbox: str = "INBOX") -> List[Dict[str, Any]]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    providers = [p for p in db.list_providers() if p["kind"] == "imap"]
    results: List[Dict[str, Any]] = []

    dt = parse_since(since)

    for p in providers:
        pid = p["id"]
        cfg = _imap_cfg_from_meta(p["meta_json"])
        password = SecretStore(s.secrets_path).get(f"{pid}:password")
        if not password:
            continue

        im = _imap_connect(cfg, password)
        try:
            im.select(mailbox)
            # IMAP search by date is day precision; use SINCE + fetch INTERNALDATE
            since_day = dt.strftime("%d-%b-%Y")
            typ, data = im.search(None, f'(SINCE "{since_day}")')
            if typ != "OK":
                continue
            ids = data[0].split()
            # Fetch latest 50
            for uid in ids[-50:]:
                typ, msg_data = im.fetch(uid, "(BODY.PEEK[HEADER] INTERNALDATE)")
                if typ != "OK":
                    continue
                raw = msg_data[0][1]
                m = email.message_from_bytes(raw)
                subj = _decode_mime_words(m.get("Subject"))
                from_ = _decode_mime_words(m.get("From"))
                to_ = _decode_mime_words(m.get("To"))
                message_id = _decode_mime_words(m.get("Message-ID")) or f"{pid}:{uid.decode()}"
                results.append(
                    {
                        "id": message_id,
                        "provider_id": pid,
                        "from_addr": from_,
                        "to_addrs": to_,
                        "subject": subj,
                        "snippet": "",
                    }
                )
        finally:
            try:
                im.logout()
            except Exception:
                pass

    return results


def fetch_full_message(message_id: str) -> Optional[Dict[str, Any]]:
    """
    For IMAP provider: message_id may include Message-ID header or fallback composite.
    If composite, we cannot re-fetch reliably unless we saved uid. In MVP, rely on saved content in DB.
    """
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    return db.get_message(message_id)


def send_email(from_addr: str, to_addr: str, subject: str, body: str) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    # choose provider by from email
    providers = [p for p in db.list_providers() if p["kind"] == "imap" and (p.get("email") == from_addr)]
    if not providers:
        raise RuntimeError(f"No IMAP provider configured for from={from_addr}")

    p = providers[0]
    pid = p["id"]
    cfg = _imap_cfg_from_meta(p["meta_json"])
    password = SecretStore(s.secrets_path).get(f"{pid}:password")
    if not password:
        raise RuntimeError("Missing SMTP password in secrets store")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    smtp = _smtp_connect(cfg, password)
    try:
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return {"ok": True, "provider_id": pid}


def fetch_and_store_recent_full(since: str = "36h", mailbox: str = "INBOX") -> Dict[str, Any]:
    from ..utils.mime import parse_mime, sha256_bytes
    from ..utils.html import html_to_text
    from ..utils.time import utc_now_iso

    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    providers = [p for p in db.list_providers() if p["kind"] == "imap"]
    store = SecretStore(s.secrets_path)

    saved = []
    for p in providers:
        pid = p["id"]
        cfg = _imap_cfg_from_meta(p["meta_json"])
        password = store.get(f"{pid}:password")
        if not password:
            continue

        im = _imap_connect(cfg, password)
        try:
            im.select(mailbox)
            since_day = parse_since(since).strftime("%d-%b-%Y")
            typ, data = im.search(None, f'(SINCE "{since_day}")')
            if typ != "OK":
                continue
            ids = data[0].split()[-30:]  # MVP cap
            for uid in ids:
                typ, msg_data = im.fetch(uid, "(RFC822)")
                if typ != "OK":
                    continue
                raw_bytes = msg_data[0][1]
                m = email.message_from_bytes(raw_bytes)

                subj = _decode_mime_words(m.get("Subject"))
                from_ = _decode_mime_words(m.get("From"))
                to_ = _decode_mime_words(m.get("To"))
                mid = _decode_mime_words(m.get("Message-ID")) or f"{pid}:{uid.decode()}"

                parsed = parse_mime(m)
                body_text = parsed.body_text
                body_html = parsed.body_html
                if body_html and not body_text:
                    body_text = html_to_text(body_html)

                now = utc_now_iso()
                db.upsert_message(
                    {
                        "id": mid,
                        "provider_id": pid,
                        "thread_id": None,
                        "from_addr": from_,
                        "to_addrs": to_,
                        "subject": subj,
                        "date_utc": now[:10] + "T00:00:00Z",
                        "snippet": (body_text or "")[:500],
                        "body_text": body_text,
                        "body_html": body_html,
                        "has_attachments": 1 if parsed.attachments else 0,
                        "raw_json": json.dumps({"imap_uid": uid.decode(errors="ignore")})[:200_000],
                        "created_at": now,
                    }
                )

                # store attachments
                att_dir = s.state_dir / "attachments" / pid.replace(":", "_")
                att_dir.mkdir(parents=True, exist_ok=True)
                for a in parsed.attachments:
                    b = a["bytes"]
                    h = sha256_bytes(b)
                    fname = a["filename"]
                    safe = fname.replace("/", "_").replace("\\", "_")
                    path = att_dir / f"{h[:16]}_{safe}"
                    path.write_bytes(b)

                    con = db.connect()
                    try:
                        con.execute(
                            """
                            INSERT INTO attachments
                            (message_id, filename, content_type, size_bytes, stored_path, sha256, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (mid, fname, a["content_type"], len(b), str(path), h, now),
                        )
                        con.commit()
                    finally:
                        con.close()

                saved.append({"id": mid, "subject": subj, "attachments": len(parsed.attachments)})
        finally:
            try:
                im.logout()
            except Exception:
                pass

    return {"ok": True, "saved": saved}
