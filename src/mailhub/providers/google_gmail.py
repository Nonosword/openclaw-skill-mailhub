from __future__ import annotations

import base64
import json
import os
import time
import hashlib
import secrets
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, parse_qs, urlparse
import requests

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


def _local_server_get_code(port: int = 8765, timeout: int = 180) -> str:
    httpd = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    httpd.timeout = 1
    start = time.time()
    while time.time() - start < timeout:
        httpd.handle_request()
        q = _CallbackHandler.query
        if "code" in q and q["code"]:
            return q["code"][0]
        if "error" in q and q["error"]:
            raise RuntimeError(f"OAuth error: {q['error'][0]}")
    raise TimeoutError("OAuth timeout waiting for code")


def _build_scopes(scopes: str) -> List[str]:
    parts = [p.strip().lower() for p in scopes.split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        out.extend(SCOPE_MAP.get(p, []))
    if not out:
        raise ValueError("No valid scopes requested")
    return sorted(set(out))


def _pkce_pair() -> tuple[str, str]:
    """
    Returns (code_verifier, code_challenge) for PKCE S256.
    """
    verifier = secrets.token_urlsafe(48)  # 43-128 chars recommended
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def auth_google(scopes: str = "gmail,calendar,contacts") -> None:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    client_id = s.oauth.google_client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Missing Google OAuth client id (settings oauth.google_client_id or GOOGLE_OAUTH_CLIENT_ID env var)")

    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    redirect_uri = "http://127.0.0.1:8765/callback"
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
    webbrowser.open(url)

    code = _local_server_get_code()

    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
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
    meta = json.dumps({"scopes": scope_list})
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

    client_id = s.oauth.google_client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id:
        raise RuntimeError("Missing Google OAuth client id (settings oauth.google_client_id or GOOGLE_OAUTH_CLIENT_ID env var)")

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
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