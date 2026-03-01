from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

from ...core.config import Settings
from ...core.security import SecretStore
from ...core.store import DB
from ...shared.time import utc_now_iso, parse_since


AUTH_BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"
DEVICE_CODE_URL = f"{AUTH_BASE}/devicecode"
TOKEN_URL = f"{AUTH_BASE}/token"
GRAPH = "https://graph.microsoft.com/v1.0"

SCOPE_MAP = {
    "mail": ["Mail.Read", "Mail.Send"],
    "calendar": ["Calendars.Read"],
    "contacts": ["Contacts.Read"],
}


def _build_scopes(scopes: str) -> str:
    parts = [p.strip().lower() for p in scopes.split(",") if p.strip()]
    if "all" in parts:
        parts = list(SCOPE_MAP.keys())
    s: List[str] = []
    for p in parts:
        s.extend(SCOPE_MAP.get(p, []))
    if not s:
        raise ValueError("No valid scopes requested")
    # MS requires "offline_access" for refresh token + "openid profile email"
    s.extend(["offline_access", "openid", "profile", "email"])
    return " ".join(sorted(set(s)))


def auth_microsoft(
    scopes: str = "mail,calendar,contacts",
    *,
    alias: str = "",
    is_mail: bool = True,
    is_calendar: bool = True,
    is_contacts: bool = True,
    mail_cold_start_days: int = 30,
    client_id_override: str = "",
) -> str:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    client_id = (client_id_override or s.effective_ms_client_id()).strip()
    if not client_id:
        raise RuntimeError("Missing Microsoft OAuth client id (settings oauth.ms_client_id or MS_OAUTH_CLIENT_ID env var)")

    scope_str = _build_scopes(scopes)

    r = requests.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": scope_str}, timeout=30)
    r.raise_for_status()
    dc = r.json()

    print("\nMicrosoft device login:")
    print(dc["message"])
    device_code = dc["device_code"]
    interval = int(dc.get("interval", 5))
    expires_in = int(dc.get("expires_in", 900))
    start = time.time()

    tok: Optional[Dict[str, Any]] = None
    while time.time() - start < expires_in:
        tr = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=30,
        )
        if tr.status_code == 200:
            tok = tr.json()
            break
        # authorization_pending / slow_down are expected
        time.sleep(interval)

    if not tok:
        raise TimeoutError("Timed out waiting for Microsoft device authorization")

    access = tok["access_token"]
    refresh = tok.get("refresh_token")
    expires_in2 = int(tok.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in2 - 30

    # Get user profile
    me = requests.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {access}"}, timeout=30)
    me.raise_for_status()
    email = me.json().get("mail") or me.json().get("userPrincipalName") or "me"
    pid = f"microsoft:{email}"

    store = SecretStore(s.db_path)
    store.set(f"{pid}:access_token", access)
    if refresh:
        store.set(f"{pid}:refresh_token", refresh)
    store.set(f"{pid}:expires_at", str(expires_at))

    db.upsert_provider(
        pid=pid,
        kind="microsoft",
        email=email,
        meta_json=json.dumps(
            {
                "alias": alias.strip(),
                "client_id": client_id,
                "oauth_scopes": scope_str.split(),
                "oauth_token_ref": f"{pid}:access_token",
                "password_ref": "",
                "imap_host": "",
                "smtp_host": "",
                "is_mail": bool(is_mail),
                "is_calendar": bool(is_calendar),
                "is_contacts": bool(is_contacts),
                "mail_cold_start_days": int(mail_cold_start_days),
                "status": "configured",
            }
        ),
        created_at=utc_now_iso(),
    )
    return pid


def _refresh_if_needed(pid: str, store: SecretStore) -> str:
    access = store.get(f"{pid}:access_token")
    exp = store.get(f"{pid}:expires_at")
    if access and exp and int(exp) > int(time.time()):
        return access

    refresh = store.get(f"{pid}:refresh_token")
    if not refresh:
        if not access:
            raise RuntimeError("No access token available")
        return access

    client_id = Settings.load().effective_ms_client_id()
    if not client_id:
        raise RuntimeError("Missing Microsoft OAuth client id (settings oauth.ms_client_id or MS_OAUTH_CLIENT_ID env var)")

    tr = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        timeout=30,
    )
    tr.raise_for_status()
    tok = tr.json()
    access_token = tok["access_token"]
    expires_in = int(tok.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in - 30

    store.set(f"{pid}:access_token", access_token)
    store.set(f"{pid}:expires_at", str(expires_at))
    if tok.get("refresh_token"):
        store.set(f"{pid}:refresh_token", tok["refresh_token"])
    return access_token


def graph_list_recent_messages(
    since: str = "15m",
    top: int = 25,
    *,
    provider_id: str = "",
    after_iso: str = "",
    page_url: str = "",
    include_next: bool = False,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    providers = [p for p in db.list_providers() if p["kind"] == "microsoft"]
    if provider_id.strip():
        providers = [p for p in providers if p["id"] == provider_id.strip()]
    if not providers:
        return {"items": [], "next_page_url": ""} if include_next else []

    dt = (after_iso.strip() or parse_since(since).isoformat())
    store = SecretStore(s.db_path)
    out: List[Dict[str, Any]] = []
    next_page_url = ""

    for p in providers:
        pid = p["id"]
        access = _refresh_if_needed(pid, store)
        if page_url.strip():
            r = requests.get(
                page_url.strip(),
                headers={"Authorization": f"Bearer {access}"},
                timeout=30,
            )
        else:
            r = requests.get(
                f"{GRAPH}/me/mailFolders/Inbox/messages",
                params={
                    "$top": top,
                    "$orderby": "receivedDateTime desc",
                    "$filter": f"receivedDateTime ge {dt}",
                    "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,conversationId",
                },
                headers={"Authorization": f"Bearer {access}"},
                timeout=30,
            )
        r.raise_for_status()
        body = r.json()
        next_page_url = str(body.get("@odata.nextLink") or "")
        for m in body.get("value", []):
            out.append({"provider_id": pid, "graph_id": m["id"], "raw": m})
    if include_next:
        return {"items": out, "next_page_url": next_page_url}
    return out


def graph_get_message(provider_id: str, graph_id: str) -> Dict[str, Any]:
    store = SecretStore(Settings.load().db_path)
    access = _refresh_if_needed(provider_id, store)
    r = requests.get(
        f"{GRAPH}/me/messages/{graph_id}",
        params={"$select": "id,subject,from,toRecipients,receivedDateTime,body,bodyPreview,conversationId"},
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def graph_send_mail(provider_id: str, to_addr: str, subject: str, body_text: str) -> Dict[str, Any]:
    store = SecretStore(Settings.load().db_path)
    access = _refresh_if_needed(provider_id, store)

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
        },
        "saveToSentItems": True,
    }
    r = requests.post(
        f"{GRAPH}/me/sendMail",
        json=payload,
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return {"ok": True}


def graph_calendar_agenda(provider_id: str, time_min_iso: str, time_max_iso: str, top: int = 50) -> List[Dict[str, Any]]:
    store = SecretStore(Settings.load().db_path)
    access = _refresh_if_needed(provider_id, store)
    r = requests.get(
        f"{GRAPH}/me/calendarView",
        params={
            "startDateTime": time_min_iso,
            "endDateTime": time_max_iso,
            "$top": top,
            "$orderby": "start/dateTime",
        },
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("value", [])


def graph_calendar_create_event(
    provider_id: str,
    *,
    subject: str,
    start_utc_iso: str,
    end_utc_iso: str,
    location: str = "",
    body_text: str = "",
) -> Dict[str, Any]:
    store = SecretStore(Settings.load().db_path)
    access = _refresh_if_needed(provider_id, store)
    # Graph DateTimeTimeZone expects dateTime without timezone suffix when timeZone is provided.
    start_dt = start_utc_iso.replace("Z", "")
    end_dt = end_utc_iso.replace("Z", "")
    payload: Dict[str, Any] = {
        "subject": subject.strip(),
        "start": {"dateTime": start_dt, "timeZone": "UTC"},
        "end": {"dateTime": end_dt, "timeZone": "UTC"},
    }
    if location.strip():
        payload["location"] = {"displayName": location.strip()}
    if body_text.strip():
        payload["body"] = {"contentType": "Text", "content": body_text.strip()}
    r = requests.post(
        f"{GRAPH}/me/events",
        json=payload,
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def graph_calendar_delete_event(provider_id: str, event_id: str) -> Dict[str, Any]:
    store = SecretStore(Settings.load().db_path)
    access = _refresh_if_needed(provider_id, store)
    r = requests.delete(
        f"{GRAPH}/me/events/{event_id}",
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return {"ok": True, "provider_id": provider_id, "event_id": event_id}
