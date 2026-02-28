from __future__ import annotations

import base64
import json
import os
import time
import hashlib
import secrets
import socket
import sys
import select
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
import requests
from requests import HTTPError

from ..config import Settings
from ..security import SecretStore
from ..store import DB
from ..utils.time import utc_now_iso, parse_since


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
PEOPLE_API = "https://people.googleapis.com/v1"
CAL_API = "https://www.googleapis.com/calendar/v3"


SCOPE_MAP = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "calendar": [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ],
    "contacts": [
        "https://www.googleapis.com/auth/contacts.readonly",
    ],
}


class _CallbackHandler(BaseHTTPRequestHandler):
    query: Dict[str, List[str]] = {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        self.__class__.query = parse_qs(u.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OAuth completed. You can close this window.")


class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def _local_server_get_code(port: int, timeout: int = 180) -> str:
    _CallbackHandler.query = {}
    httpd = _ReusableHTTPServer(("127.0.0.1", port), _CallbackHandler)
    try:
        httpd.timeout = 1
        start = time.time()
        while time.time() - start < timeout:
            httpd.handle_request()
            q = _CallbackHandler.query
            if "code" in q and q["code"]:
                return q["code"][0]
            if "error" in q and q["error"]:
                raise RuntimeError(f"OAuth error: {q['error'][0]}")
        raise TimeoutError(f"OAuth timeout waiting for code on 127.0.0.1:{port} after {timeout}s")
    finally:
        httpd.server_close()


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_scopes(scopes: str) -> List[str]:
    parts = [p.strip().lower() for p in scopes.split(",") if p.strip()]
    if "all" in parts:
        parts = list(SCOPE_MAP.keys())
    out: List[str] = []
    for p in parts:
        out.extend(SCOPE_MAP.get(p, []))
    if not out:
        raise ValueError("No valid scopes requested")
    return sorted(set(out))


def _extract_code_value(v: str) -> str:
    raw = (v or "").strip()
    if not raw:
        return ""
    if "code=" in raw:
        q = parse_qs(urlparse(raw).query)
        codes = q.get("code") or []
        if codes:
            return codes[0]
    return raw


def _wait_code_or_manual(port: int, timeout: int = 180) -> str:
    """
    Wait for either:
    1) OAuth callback to local HTTP server
    2) Manual code (or callback URL) pasted to stdin
    """
    _CallbackHandler.query = {}
    httpd = _ReusableHTTPServer(("127.0.0.1", port), _CallbackHandler)
    httpd.timeout = 1

    deadline = time.time() + timeout
    stdin_ok = hasattr(sys.stdin, "fileno")

    try:
        while time.time() < deadline:
            # A) local callback already arrived
            q = _CallbackHandler.query
            if "code" in q and q["code"]:
                return q["code"][0]
            if "error" in q and q["error"]:
                raise RuntimeError(f"OAuth error: {q['error'][0]}")

            # B) manual input available
            if stdin_ok:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0)
                except Exception:
                    r = []
                    stdin_ok = False
                if r:
                    line = sys.stdin.readline()
                    if line:
                        code = _extract_code_value(line)
                        if code:
                            return code
                    else:
                        stdin_ok = False

            # C) serve one callback request window
            httpd.handle_request()

        raise TimeoutError(f"OAuth timeout waiting for callback/manual code on 127.0.0.1:{port} after {timeout}s")
    finally:
        httpd.server_close()


def _response_payload(resp: requests.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"raw": data}
    except Exception:
        return {"raw_text": (resp.text or "")[:1000]}


def _raise_google_http_error(stage: str, resp: requests.Response) -> None:
    payload = _response_payload(resp)
    err = payload.get("error")
    err_desc = payload.get("error_description") or payload.get("error_summary") or ""
    hints = [
        "Use a fresh authorization code (codes are short-lived and one-time).",
        "Ensure redirect_uri used in authorize and token steps is identical.",
        "For Desktop App OAuth client, avoid stale mismatched client_secret.",
        "If needed, clear stored secret: mailhub settings-set oauth.google_client_secret \"\"",
    ]
    raise RuntimeError(
        json.dumps(
            {
                "ok": False,
                "provider": "google",
                "stage": stage,
                "http_status": resp.status_code,
                "endpoint": str(resp.url),
                "error": err or "http_error",
                "error_description": err_desc,
                "response": payload,
                "hints": hints,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _pkce_pair() -> tuple[str, str]:
    """
    Returns (code_verifier, code_challenge) for PKCE S256.
    """
    verifier = secrets.token_urlsafe(48)  # 43-128 chars recommended
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def auth_google(
    scopes: str = "gmail,calendar,contacts",
    *,
    alias: str = "",
    is_mail: bool = True,
    is_calendar: bool = True,
    is_contacts: bool = True,
    client_id_override: str = "",
    client_secret_override: str = "",
    manual_code: str = "",
) -> None:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    client_id = (client_id_override or s.effective_google_client_id()).strip()
    if not client_id:
        raise RuntimeError("Missing Google OAuth client id (settings oauth.google_client_id or GOOGLE_OAUTH_CLIENT_ID env var)")

    client_secret = (client_secret_override or s.effective_google_client_secret()).strip()
    env_port = os.environ.get("MAILHUB_GOOGLE_CALLBACK_PORT", "").strip()
    env_timeout = os.environ.get("MAILHUB_GOOGLE_OAUTH_TIMEOUT", "").strip()
    timeout = int(env_timeout) if env_timeout else 300
    port = int(env_port) if env_port else _pick_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    scope_list = _build_scopes(scopes)

    verifier, challenge = _pkce_pair()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scope_list),
        "access_type": "offline",
        "prompt": "consent",
        # PKCE
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    url = requests.Request("GET", GOOGLE_AUTH_URL, params=params).prepare().url
    assert url
    print(f"Google OAuth URL: {url}", flush=True)
    try:
        opened = webbrowser.open(url)
        if not opened:
            print("Browser did not auto-open. Please open the URL manually.", flush=True)
    except Exception:
        print("Failed to auto-open browser. Please open the URL manually.", flush=True)

    print(
        f"Waiting for OAuth callback on http://127.0.0.1:{port}/callback (timeout {timeout}s)...",
        flush=True,
    )
    print("You can paste OAuth code (or full callback URL) here anytime while waiting.", flush=True)

    code = _extract_code_value(manual_code)
    if not code:
        code = _wait_code_or_manual(port=port, timeout=timeout)
    if not code:
        raise TimeoutError(
            f"OAuth callback/manual code not received. Open the URL above in a browser where 127.0.0.1:{port} is reachable, "
            "or rerun with `--code <oauth_code>`."
        )

    data = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    try:
        r.raise_for_status()
    except HTTPError:
        _raise_google_http_error("token_exchange", r)
    tok = r.json()

    access_token = tok["access_token"]
    refresh_token = tok.get("refresh_token")
    expires_in = int(tok.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in - 30

    # Get profile email
    email_addr = _gmail_get_profile_email(access_token)
    pid = f"google:{email_addr}"

    SecretStore(s.secrets_path).set(f"{pid}:access_token", access_token)
    if refresh_token:
        SecretStore(s.secrets_path).set(f"{pid}:refresh_token", refresh_token)
    SecretStore(s.secrets_path).set(f"{pid}:expires_at", str(expires_at))
    meta = json.dumps(
        {
            "alias": alias.strip(),
            "client_id": client_id,
            "oauth_scopes": scope_list,
            "oauth_token_ref": f"{pid}:access_token",
            "password_ref": "",
            "imap_host": "",
            "smtp_host": "",
            "is_mail": bool(is_mail),
            "is_calendar": bool(is_calendar),
            "is_contacts": bool(is_contacts),
            "status": "configured",
        }
    )
    db.upsert_provider(pid=pid, kind="google", email=email_addr, meta_json=meta, created_at=utc_now_iso())


def _gmail_get_profile_email(access_token: str) -> str:
    r = requests.get(f"{GMAIL_API}/users/me/profile", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("emailAddress") or "me"


def _refresh_if_needed(pid: str, store: SecretStore) -> str:
    s = Settings.load()
    access = store.get(f"{pid}:access_token")
    exp = store.get(f"{pid}:expires_at")
    if access and exp and int(exp) > int(time.time()):
        return access

    refresh = store.get(f"{pid}:refresh_token")
    if not refresh:
        if not access:
            raise RuntimeError("No access token available")
        return access

    client_id = s.effective_google_client_id()
    client_secret = s.effective_google_client_secret()
    if not client_id:
        raise RuntimeError("Missing Google OAuth client id (settings oauth.google_client_id or GOOGLE_OAUTH_CLIENT_ID env var)")

    data = {
        "client_id": client_id,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    try:
        r.raise_for_status()
    except HTTPError:
        _raise_google_http_error("token_refresh", r)
    tok = r.json()

    access_token = tok["access_token"]
    expires_in = int(tok.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in - 30

    store.set(f"{pid}:access_token", access_token)
    store.set(f"{pid}:expires_at", str(expires_at))
    return access_token


def gmail_list_messages(since: str = "15m", max_results: int = 50) -> List[Dict[str, Any]]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    providers = [p for p in db.list_providers() if p["kind"] == "google"]
    if not providers:
        return []

    dt = parse_since(since)
    after_epoch = int(dt.timestamp())

    out: List[Dict[str, Any]] = []
    store = SecretStore(s.secrets_path)

    for p in providers:
        pid = p["id"]
        access = _refresh_if_needed(pid, store)
        q = f"after:{after_epoch}"
        r = requests.get(
            f"{GMAIL_API}/users/me/messages",
            params={"q": q, "maxResults": max_results},
            headers={"Authorization": f"Bearer {access}"},
            timeout=30,
        )
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("messages", [])]
        for mid in ids:
            out.append({"provider_id": pid, "gmail_id": mid})
    return out


def gmail_get_message(provider_id: str, gmail_id: str) -> Dict[str, Any]:
    s = Settings.load()
    store = SecretStore(s.secrets_path)
    access = _refresh_if_needed(provider_id, store)

    r = requests.get(
        f"{GMAIL_API}/users/me/messages/{gmail_id}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def gmail_send(provider_id: str, raw_rfc822: bytes) -> Dict[str, Any]:
    s = Settings.load()
    store = SecretStore(s.secrets_path)
    access = _refresh_if_needed(provider_id, store)

    b64 = base64.urlsafe_b64encode(raw_rfc822).decode("ascii")
    r = requests.post(
        f"{GMAIL_API}/users/me/messages/send",
        json={"raw": b64},
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def google_calendar_list_events(provider_id: str, time_min_iso: str, time_max_iso: str, max_results: int = 50) -> List[Dict[str, Any]]:
    store = SecretStore(Settings.load().secrets_path)
    access = _refresh_if_needed(provider_id, store)

    r = requests.get(
        f"{CAL_API}/calendars/primary/events",
        params={
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max_results,
        },
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def google_calendar_create_event(
    provider_id: str,
    *,
    summary: str,
    start_utc_iso: str,
    end_utc_iso: str,
    location: str = "",
    description: str = "",
) -> Dict[str, Any]:
    store = SecretStore(Settings.load().secrets_path)
    access = _refresh_if_needed(provider_id, store)
    payload: Dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start_utc_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_utc_iso, "timeZone": "UTC"},
    }
    if location.strip():
        payload["location"] = location.strip()
    if description.strip():
        payload["description"] = description.strip()
    r = requests.post(
        f"{CAL_API}/calendars/primary/events",
        json=payload,
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def google_calendar_delete_event(provider_id: str, event_id: str) -> Dict[str, Any]:
    store = SecretStore(Settings.load().secrets_path)
    access = _refresh_if_needed(provider_id, store)
    r = requests.delete(
        f"{CAL_API}/calendars/primary/events/{event_id}",
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    r.raise_for_status()
    return {"ok": True, "provider_id": provider_id, "event_id": event_id}
