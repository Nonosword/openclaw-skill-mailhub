from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .store import DB
from .utils.time import utc_now_iso


def list_accounts(db: DB, hide_email_when_alias: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in db.list_providers():
        meta = _load_meta(p.get("meta_json"))
        alias = (meta.get("alias") or "").strip()
        email = p.get("email") or ""
        display = alias or email or p["id"]
        if hide_email_when_alias and alias:
            email = ""
        out.append(
            {
                "id": p["id"],
                "kind": p["kind"],
                "display_name": display,
                "alias": alias,
                "email": email,
                "client_id_set": bool(meta.get("client_id")),
                "password_ref": meta.get("password_ref") or "",
                "oauth_token_ref": meta.get("oauth_token_ref") or "",
                "oauth_scopes": meta.get("oauth_scopes") or [],
                "servers": {
                    "imap_host": meta.get("imap_host") or "",
                    "smtp_host": meta.get("smtp_host") or "",
                },
                "capabilities": {
                    "is_mail": bool(meta.get("is_mail", _default_caps(p["kind"])["is_mail"])),
                    "is_calendar": bool(meta.get("is_calendar", _default_caps(p["kind"])["is_calendar"])),
                    "is_contacts": bool(meta.get("is_contacts", _default_caps(p["kind"])["is_contacts"])),
                },
                "status": meta.get("status") or "configured",
                "created_at": p.get("created_at") or "",
            }
        )
    return out


def update_account_profile(
    db: DB,
    account_id: str,
    *,
    alias: Optional[str] = None,
    is_mail: Optional[bool] = None,
    is_calendar: Optional[bool] = None,
    is_contacts: Optional[bool] = None,
) -> Dict[str, Any]:
    p = db.get_provider(account_id)
    if not p:
        raise RuntimeError(f"Account not found: {account_id}")

    meta = _load_meta(p.get("meta_json"))
    if alias is not None:
        meta["alias"] = alias.strip()
    if is_mail is not None:
        meta["is_mail"] = bool(is_mail)
    if is_calendar is not None:
        meta["is_calendar"] = bool(is_calendar)
    if is_contacts is not None:
        meta["is_contacts"] = bool(is_contacts)
    meta["updated_at"] = utc_now_iso()

    db.upsert_provider(
        pid=p["id"],
        kind=p["kind"],
        email=p.get("email"),
        meta_json=json.dumps(meta),
        created_at=p.get("created_at") or utc_now_iso(),
    )
    return {"ok": True, "updated": account_id}


def _default_caps(kind: str) -> Dict[str, bool]:
    k = kind.lower()
    if k in ("google", "microsoft"):
        return {"is_mail": True, "is_calendar": True, "is_contacts": True}
    if k == "imap":
        return {"is_mail": True, "is_calendar": False, "is_contacts": False}
    if k == "caldav":
        return {"is_mail": False, "is_calendar": True, "is_contacts": False}
    if k == "carddav":
        return {"is_mail": False, "is_calendar": False, "is_contacts": True}
    return {"is_mail": False, "is_calendar": False, "is_contacts": False}


def _load_meta(meta_json: Any) -> Dict[str, Any]:
    if isinstance(meta_json, dict):
        return dict(meta_json)
    if not meta_json:
        return {}
    try:
        return json.loads(str(meta_json))
    except Exception:
        return {}
