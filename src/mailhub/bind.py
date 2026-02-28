from __future__ import annotations

import getpass
import os
import sys
from typing import Any, Dict, Optional

import typer

from .accounts import list_accounts, update_account_profile
from .config import Settings
from .providers.google_gmail import auth_google
from .providers.ms_graph import auth_microsoft
from .providers.imap_smtp import auth_imap
from .providers.caldav import auth_caldav
from .providers.carddav import auth_carddav
from .store import DB


def bind_menu() -> Dict[str, Any]:
    """
    Unified interactive entry for provider binding + account profile updates.
    """
    s = Settings.load()
    s.ensure_dirs()
    if not sys.stdin.isatty():
        raise RuntimeError("Interactive bind menu needs a TTY. Use `mailhub bind --provider ...` or `mailhub bind --list`.")

    db = DB(s.db_path)
    db.init()

    typer.echo("")
    typer.echo("MailHub account binding")
    _print_accounts(db)
    typer.echo("")
    typer.echo("1) Add Google (Gmail/Calendar/Contacts)")
    typer.echo("2) Add Microsoft (Mail/Calendar/Contacts)")
    typer.echo("3) Add IMAP/SMTP")
    typer.echo("4) Add CalDAV")
    typer.echo("5) Add CardDAV")
    typer.echo("6) Modify existing account (alias/capabilities)")
    typer.echo("0) Exit")
    choice = typer.prompt("Select action", default="1").strip()

    if choice in ("1", "2", "3", "4", "5"):
        return _bind_add_choice(choice)
    if choice == "6":
        return _bind_modify_choice(db)
    return {"ok": True, "bound": None, "message": "No change"}


def bind_provider(
    provider: str,
    scopes: str | None = None,
    google_client_id: str | None = None,
    google_client_secret: str | None = None,
    ms_client_id: str | None = None,
    email: str | None = None,
    imap_host: str | None = None,
    smtp_host: str | None = None,
    username: str | None = None,
    host: str | None = None,
    alias: str | None = None,
    is_mail: Optional[bool] = None,
    is_calendar: Optional[bool] = None,
    is_contacts: Optional[bool] = None,
) -> Dict[str, Any]:
    s = Settings.load()
    s.ensure_dirs()

    p = provider.strip().lower()
    a = (alias or "").strip()

    if p == "google":
        if google_client_id:
            s.oauth.google_client_id = google_client_id.strip()
        if google_client_secret:
            s.oauth.google_client_secret = google_client_secret.strip()
        s.save()
        auth_google(
            scopes=(scopes or "gmail,calendar,contacts"),
            alias=a,
            is_mail=True if is_mail is None else bool(is_mail),
            is_calendar=True if is_calendar is None else bool(is_calendar),
            is_contacts=True if is_contacts is None else bool(is_contacts),
            client_id_override=(google_client_id or "").strip(),
            client_secret_override=(google_client_secret or "").strip(),
        )
        return {"ok": True, "bound": "google"}

    if p == "microsoft":
        if ms_client_id:
            s.oauth.ms_client_id = ms_client_id.strip()
            s.save()
        auth_microsoft(
            scopes=(scopes or "mail,calendar,contacts"),
            alias=a,
            is_mail=True if is_mail is None else bool(is_mail),
            is_calendar=True if is_calendar is None else bool(is_calendar),
            is_contacts=True if is_contacts is None else bool(is_contacts),
            client_id_override=(ms_client_id or "").strip(),
        )
        return {"ok": True, "bound": "microsoft"}

    if p == "imap":
        if not (email and imap_host and smtp_host):
            raise RuntimeError("IMAP requires --email --imap-host --smtp-host")
        auth_imap(
            email=email,
            imap_host=imap_host,
            smtp_host=smtp_host,
            alias=a,
            is_mail=True if is_mail is None else bool(is_mail),
            is_calendar=False if is_calendar is None else bool(is_calendar),
            is_contacts=False if is_contacts is None else bool(is_contacts),
        )
        return {"ok": True, "bound": "imap", "email": email}

    if p == "caldav":
        if not (username and host):
            raise RuntimeError("CalDAV requires --username --host")
        auth_caldav(
            username=username,
            host=host,
            alias=a,
            is_mail=False if is_mail is None else bool(is_mail),
            is_calendar=True if is_calendar is None else bool(is_calendar),
            is_contacts=False if is_contacts is None else bool(is_contacts),
        )
        return {"ok": True, "bound": "caldav", "username": username}

    if p == "carddav":
        if not (username and host):
            raise RuntimeError("CardDAV requires --username --host")
        auth_carddav(
            username=username,
            host=host,
            alias=a,
            is_mail=False if is_mail is None else bool(is_mail),
            is_calendar=False if is_calendar is None else bool(is_calendar),
            is_contacts=True if is_contacts is None else bool(is_contacts),
        )
        return {"ok": True, "bound": "carddav", "username": username}

    raise RuntimeError(f"Unknown provider: {provider}")


def bind_list() -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    return {"ok": True, "accounts": list_accounts(db, hide_email_when_alias=False)}


def bind_update_account(
    account_id: str,
    *,
    alias: str | None = None,
    is_mail: Optional[bool] = None,
    is_calendar: Optional[bool] = None,
    is_contacts: Optional[bool] = None,
) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    return update_account_profile(
        db,
        account_id,
        alias=alias,
        is_mail=is_mail,
        is_calendar=is_calendar,
        is_contacts=is_contacts,
    )


def _bind_add_choice(choice: str) -> Dict[str, Any]:
    if choice == "1":
        s = Settings.load()
        _ensure_google_client(s)
        alias = typer.prompt("Alias (optional)", default="")
        scopes = typer.prompt("Scopes (comma separated)", default="gmail,calendar,contacts")
        return bind_provider(provider="google", scopes=scopes, alias=alias)
    if choice == "2":
        s = Settings.load()
        _ensure_ms_client(s)
        alias = typer.prompt("Alias (optional)", default="")
        scopes = typer.prompt("Scopes (comma separated)", default="mail,calendar,contacts")
        return bind_provider(provider="microsoft", scopes=scopes, alias=alias)
    if choice == "3":
        email = typer.prompt("Email address")
        alias = typer.prompt("Alias (optional)", default="")
        imap_host = typer.prompt("IMAP host", default="imap.gmail.com")
        smtp_host = typer.prompt("SMTP host", default="smtp.gmail.com")
        typer.echo("You will be prompted for app password securely (input hidden).")
        return bind_provider(provider="imap", email=email, imap_host=imap_host, smtp_host=smtp_host, alias=alias)
    if choice == "4":
        username = typer.prompt("CalDAV username")
        alias = typer.prompt("Alias (optional)", default="")
        host = typer.prompt("CalDAV host (without scheme)")
        typer.echo("You will be prompted for app password securely (input hidden).")
        return bind_provider(provider="caldav", username=username, host=host, alias=alias)
    if choice == "5":
        username = typer.prompt("CardDAV username")
        alias = typer.prompt("Alias (optional)", default="")
        host = typer.prompt("CardDAV host (without scheme)")
        typer.echo("You will be prompted for app password securely (input hidden).")
        return bind_provider(provider="carddav", username=username, host=host, alias=alias)
    return {"ok": False, "message": "Unsupported add choice"}


def _bind_modify_choice(db: DB) -> Dict[str, Any]:
    accounts = list_accounts(db, hide_email_when_alias=False)
    if not accounts:
        return {"ok": False, "message": "No accounts to modify"}
    for idx, a in enumerate(accounts, start=1):
        typer.echo(f"{idx}) {a['display_name']} [{a['id']}] {a['capabilities']}")
    raw = typer.prompt("Select account index", default="1")
    try:
        i = int(raw)
    except ValueError:
        raise RuntimeError("Invalid index")
    if i < 1 or i > len(accounts):
        raise RuntimeError("Index out of range")
    target = accounts[i - 1]
    alias = typer.prompt("Alias (blank keeps current)", default=target.get("alias") or "")
    is_mail = typer.confirm("Enable mail capability?", default=bool(target["capabilities"]["is_mail"]))
    is_calendar = typer.confirm("Enable calendar capability?", default=bool(target["capabilities"]["is_calendar"]))
    is_contacts = typer.confirm("Enable contacts capability?", default=bool(target["capabilities"]["is_contacts"]))
    return update_account_profile(
        db,
        target["id"],
        alias=alias,
        is_mail=is_mail,
        is_calendar=is_calendar,
        is_contacts=is_contacts,
    )


def _print_accounts(db: DB) -> None:
    accounts = list_accounts(db, hide_email_when_alias=False)
    if not accounts:
        typer.echo("Configured accounts: (none)")
        return
    typer.echo("Configured accounts:")
    for a in accounts:
        email_part = f" <{a['email']}>" if a.get("email") else ""
        typer.echo(f"- {a['display_name']}{email_part} [{a['id']}] caps={a['capabilities']}")


def _ensure_google_client(s: Settings) -> None:
    if s.oauth.google_client_id:
        return
    current = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    if current:
        typer.echo("Using GOOGLE_OAUTH_CLIENT_ID from environment.")
        return
    cid = typer.prompt("Google OAuth Client ID")
    secret = getpass.getpass("Google OAuth Client Secret (optional, hidden input): ").strip()
    s.oauth.google_client_id = cid.strip()
    if secret:
        s.oauth.google_client_secret = secret
    s.save()


def _ensure_ms_client(s: Settings) -> None:
    if s.oauth.ms_client_id:
        return
    current = os.environ.get("MS_OAUTH_CLIENT_ID", "")
    if current:
        typer.echo("Using MS_OAUTH_CLIENT_ID from environment.")
        return
    cid = typer.prompt("Microsoft OAuth Client ID")
    s.oauth.ms_client_id = cid.strip()
    s.save()
