from __future__ import annotations

import json
import requests
from dataclasses import dataclass

from ..config import Settings
from ..security import SecretStore
from ..store import DB
from ..utils.time import utc_now_iso


@dataclass
class CalDAVConfig:
    username: str
    host: str  # e.g. https://caldav.icloud.com


def auth_caldav(username: str, host: str) -> None:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    import getpass
    password = getpass.getpass(f"Enter CalDAV app password for {username}: ")

    pid = f"caldav:{username}"
    SecretStore(s.secrets_path).set(f"{pid}:password", password)
    db.upsert_provider(pid=pid, kind="caldav", email=None, meta_json=json.dumps({"username": username, "host": host}), created_at=utc_now_iso())

