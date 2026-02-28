from __future__ import annotations

import json

from ..config import Settings
from ..security import SecretStore
from ..store import DB
from ..utils.time import utc_now_iso


def auth_carddav(
    username: str,
    host: str,
    *,
    alias: str = "",
    is_mail: bool = False,
    is_calendar: bool = False,
    is_contacts: bool = True,
) -> None:
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()

    import getpass
    password = getpass.getpass(f"Enter CardDAV app password for {username}: ")

    pid = f"carddav:{username}"
    SecretStore(s.secrets_path).set(f"{pid}:password", password)
    db.upsert_provider(
        pid=pid,
        kind="carddav",
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


# NOTE: Full CardDAV requires PROPFIND + vCard parsing.
# MVP stores credentials for future implementation.
