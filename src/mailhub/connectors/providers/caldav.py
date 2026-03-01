from __future__ import annotations

import json
import requests
from dataclasses import dataclass

from ...core.config import Settings
from ...core.security import SecretStore
from ...core.store import DB
from ...shared.time import utc_now_iso


@dataclass
class CalDAVConfig:
    username: str
    host: str  # e.g. https://caldav.icloud.com


def auth_caldav(
    username: str,
    host: str,
    *,
    alias: str = "",
    is_mail: bool = False,
    is_calendar: bool = True,
    is_contacts: bool = False,
) -> None:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    import getpass
    password = getpass.getpass(f"Enter CalDAV app password for {username}: ")

    pid = f"caldav:{username}"
    SecretStore(s.db_path).set(f"{pid}:password", password)
    db.upsert_provider(
        pid=pid,
        kind="caldav",
        email=None,
        meta_json=json.dumps(
            {
                "username": username,
                "host": host,
                "alias": alias.strip(),
                "client_id": "",
                "oauth_scopes": [],
                "oauth_token_ref": "",
                "password_ref": f"{pid}:password",
                "imap_host": "",
                "smtp_host": "",
                "is_mail": bool(is_mail),
                "is_calendar": bool(is_calendar),
                "is_contacts": bool(is_contacts),
                "status": "configured",
            }
        ),
        created_at=utc_now_iso(),
    )
