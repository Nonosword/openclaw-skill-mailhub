from __future__ import annotations

import getpass
import os
import sys
from typing import Any, Dict, Optional

import typer

from ..core.accounts import list_accounts, update_account_profile
from ..core.config import Settings
from ..connectors.providers.google_gmail import auth_google
from ..connectors.providers.ms_graph import auth_microsoft
from ..connectors.providers.imap_smtp import auth_imap
from ..connectors.providers.caldav import auth_caldav
from ..connectors.providers.carddav import auth_carddav
from ..flows.ingest import inbox_bootstrap_provider
from ..core.logging import get_logger, log_event
from ..core.store import DB

logger = get_logger(__name__)


def _bootstrap_total_from_out(out: Dict[str, Any]) -> int:
    bootstrap = out.get("bootstrap")
    if not isinstance(bootstrap, dict):
        return 0
    poll = bootstrap.get("bootstrap")
    if not isinstance(poll, dict):
        return 0
    items = poll.get("items")
    if not isinstance(items, list):
        return 0
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        total += int(item.get("count") or 0)
    return total


def bind_menu() -> Dict[str, Any]:
    """
    Unified interactive entry for provider binding + account profile updates.
    """
    s = Settings.load()
    s.ensure_dirs()
    db = DB(s.db_path)
    db.init()
    accounts = list_accounts(db, hide_email_when_alias=False)
    menu = {
        "1": "google",
        "2": "microsoft",
        "3": "imap",
        "4": "caldav",
        "5": "carddav",
        "0": "exit",
    }
    if not sys.stdin.isatty():
        return {
            "ok": False,
            "reason": "interactive_tty_required",
            "message": "Interactive bind menu needs a TTY session.",
            "accounts": accounts,
            "menu": menu,
            "next_steps": [
                "mailhub bind --provider google --google-client-id \"<CLIENT_ID>\" --scopes all",
                "mailhub bind --provider google --google-client-id \"<CLIENT_ID>\" --scopes all --google-code \"<CODE_OR_CALLBACK_URL>\"",
                "mailhub bind --provider microsoft --ms-client-id \"<CLIENT_ID>\" --scopes all",
                "mailhub bind --provider imap --email <email> --imap-host <host> --smtp-host <host>",
            ],
        }

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
    return {"ok": True, "bound": None, "message": "No change", "accounts": accounts, "menu": menu}


def bind_provider(
    provider: str,
    scopes: str | None = None,
    google_client_id: str | None = None,
    google_client_secret: str | None = None,
    google_code: str | None = None,
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
    cold_start_days: int | None = None,
    bootstrap_after_bind: bool = True,
) -> Dict[str, Any]:
    s = Settings.load()
    s.ensure_dirs()

    p = provider.strip().lower()
    a = (alias or "").strip()
    cold_days = int(cold_start_days or 30)
    bound_provider_id = ""
    mail_enabled = True if is_mail is None else bool(is_mail)

    if p == "google":
        if google_client_id:
            s.oauth.google_client_id = google_client_id.strip()
        if google_client_secret:
            s.oauth.google_client_secret = google_client_secret.strip()
        if not s.effective_google_client_id():
            raise RuntimeError("Google OAuth client id is required. Set GOOGLE_OAUTH_CLIENT_ID or run `mailhub config --wizard`.")
        if not (google_client_secret or s.effective_google_client_secret()):
            raise RuntimeError(
                "Google OAuth client secret is required in this flow. "
                "Set GOOGLE_OAUTH_CLIENT_SECRET (exported) or run `mailhub config --wizard`."
            )
        s.save()
        bound_provider_id = auth_google(
            scopes=(scopes or "gmail,calendar,contacts"),
            alias=a,
            is_mail=mail_enabled,
            is_calendar=True if is_calendar is None else bool(is_calendar),
            is_contacts=True if is_contacts is None else bool(is_contacts),
            mail_cold_start_days=cold_days,
            client_id_override=(google_client_id or "").strip(),
            client_secret_override=(google_client_secret or "").strip(),
            manual_code=(google_code or "").strip(),
        )
        out: Dict[str, Any] = {"ok": True, "bound": "google", "provider_id": bound_provider_id}
        if bootstrap_after_bind and mail_enabled and bound_provider_id:
            out["bootstrap"] = inbox_bootstrap_provider(bound_provider_id, cold_start_days=cold_days)
        bootstrap_count = _bootstrap_total_from_out(out)
        log_event(
            logger,
            "bind_provider_done",
            provider="google",
            provider_id=bound_provider_id,
            alias=a,
            mail_enabled=mail_enabled,
            bootstrap_requested=bootstrap_after_bind and mail_enabled,
            bootstrap_first_count=bootstrap_count,
        )
        return out

    if p == "microsoft":
        if ms_client_id:
            s.oauth.ms_client_id = ms_client_id.strip()
            s.save()
        bound_provider_id = auth_microsoft(
            scopes=(scopes or "mail,calendar,contacts"),
            alias=a,
            is_mail=mail_enabled,
            is_calendar=True if is_calendar is None else bool(is_calendar),
            is_contacts=True if is_contacts is None else bool(is_contacts),
            mail_cold_start_days=cold_days,
            client_id_override=(ms_client_id or "").strip(),
        )
        out = {"ok": True, "bound": "microsoft", "provider_id": bound_provider_id}
        if bootstrap_after_bind and mail_enabled and bound_provider_id:
            out["bootstrap"] = inbox_bootstrap_provider(bound_provider_id, cold_start_days=cold_days)
        bootstrap_count = _bootstrap_total_from_out(out)
        log_event(
            logger,
            "bind_provider_done",
            provider="microsoft",
            provider_id=bound_provider_id,
            alias=a,
            mail_enabled=mail_enabled,
            bootstrap_requested=bootstrap_after_bind and mail_enabled,
            bootstrap_first_count=bootstrap_count,
        )
        return out

    if p == "imap":
        if not (email and imap_host and smtp_host):
            raise RuntimeError("IMAP requires --email --imap-host --smtp-host")
        bound_provider_id = auth_imap(
            email=email,
            imap_host=imap_host,
            smtp_host=smtp_host,
            alias=a,
            is_mail=mail_enabled,
            is_calendar=False if is_calendar is None else bool(is_calendar),
            is_contacts=False if is_contacts is None else bool(is_contacts),
            mail_cold_start_days=cold_days,
        )
        out = {"ok": True, "bound": "imap", "email": email, "provider_id": bound_provider_id}
        if bootstrap_after_bind and mail_enabled and bound_provider_id:
            out["bootstrap"] = inbox_bootstrap_provider(bound_provider_id, cold_start_days=cold_days)
        bootstrap_count = _bootstrap_total_from_out(out)
        log_event(
            logger,
            "bind_provider_done",
            provider="imap",
            provider_id=bound_provider_id,
            alias=a,
            mail_enabled=mail_enabled,
            bootstrap_requested=bootstrap_after_bind and mail_enabled,
            bootstrap_first_count=bootstrap_count,
        )
        return out

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
        out = {"ok": True, "bound": "caldav", "username": username}
        log_event(logger, "bind_provider_done", provider="caldav", alias=a, username=username or "")
        return out

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
        out = {"ok": True, "bound": "carddav", "username": username}
        log_event(logger, "bind_provider_done", provider="carddav", alias=a, username=username or "")
        return out

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
        typer.echo("Google bind method:")
        typer.echo("1) OAuth (recommended)")
        typer.echo("2) App Password via IMAP/SMTP")
        method = typer.prompt("Select method", default="1").strip()
        if method == "2":
            email = typer.prompt("Google email address")
            alias = typer.prompt("Alias (optional)", default="")
            cold_start_days = int(typer.prompt("Cold start days", default="30").strip() or "30")
            return bind_provider(
                provider="imap",
                email=email,
                imap_host="imap.gmail.com",
                smtp_host="smtp.gmail.com",
                alias=alias,
                cold_start_days=cold_start_days,
            )
        s = Settings.load()
        _ensure_google_client(s)
        alias = typer.prompt("Alias (optional)", default="")
        scopes = typer.prompt("Scopes (comma separated, or 'all')", default="gmail,calendar,contacts")
        cold_start_days = int(typer.prompt("Cold start days", default="30").strip() or "30")
        typer.echo("Google OAuth will open in browser. Keep this terminal running until callback completes.")
        return bind_provider(provider="google", scopes=scopes, alias=alias, cold_start_days=cold_start_days)
    if choice == "2":
        s = Settings.load()
        _ensure_ms_client(s)
        alias = typer.prompt("Alias (optional)", default="")
        scopes = typer.prompt("Scopes (comma separated, or 'all')", default="mail,calendar,contacts")
        cold_start_days = int(typer.prompt("Cold start days", default="30").strip() or "30")
        return bind_provider(provider="microsoft", scopes=scopes, alias=alias, cold_start_days=cold_start_days)
    if choice == "3":
        proto = typer.prompt("Protocol (imap|pop3)", default="imap").strip().lower()
        if proto == "pop3":
            return {
                "ok": False,
                "reason": "unsupported_protocol",
                "message": "POP3 is not supported yet. Use IMAP/SMTP.",
            }
        email = typer.prompt("Email address")
        alias = typer.prompt("Alias (optional)", default="")
        imap_host = typer.prompt("IMAP host", default="imap.gmail.com")
        smtp_host = typer.prompt("SMTP host", default="smtp.gmail.com")
        cold_start_days = int(typer.prompt("Cold start days", default="30").strip() or "30")
        typer.echo("You will be prompted for app password securely (input hidden).")
        return bind_provider(
            provider="imap",
            email=email,
            imap_host=imap_host,
            smtp_host=smtp_host,
            alias=alias,
            cold_start_days=cold_start_days,
        )
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
    changed = False
    if s.effective_google_client_id():
        if os.environ.get("GOOGLE_OAUTH_CLIENT_ID"):
            typer.echo("Using GOOGLE_OAUTH_CLIENT_ID from environment.")
    else:
        cid = typer.prompt("Google OAuth Client ID")
        s.oauth.google_client_id = cid.strip()
        changed = True

    if s.effective_google_client_secret():
        if os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"):
            typer.echo("Using GOOGLE_OAUTH_CLIENT_SECRET from environment.")
    else:
        secret = getpass.getpass("Google OAuth Client Secret (hidden input, required): ").strip()
        if not secret:
            raise RuntimeError("Google OAuth Client Secret is required.")
        s.oauth.google_client_secret = secret
        changed = True

    if changed:
        s.save()


def _ensure_ms_client(s: Settings) -> None:
    if s.effective_ms_client_id():
        if os.environ.get("MS_OAUTH_CLIENT_ID"):
            typer.echo("Using MS_OAUTH_CLIENT_ID from environment.")
        return
    cid = typer.prompt("Microsoft OAuth Client ID")
    s.oauth.ms_client_id = cid.strip()
    s.save()
