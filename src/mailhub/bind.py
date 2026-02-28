from __future__ import annotations

import getpass
import os
from typing import Any, Dict

import typer

from .config import Settings
from .providers.google_gmail import auth_google
from .providers.ms_graph import auth_microsoft
from .providers.imap_smtp import auth_imap
from .providers.caldav import auth_caldav
from .providers.carddav import auth_carddav


def bind_menu() -> Dict[str, Any]:
    """
    Unified interactive entry for provider binding.
    """
    s = Settings.load()
    s.ensure_dirs()

    typer.echo("")
    typer.echo("MailHub account binding menu")
    typer.echo("1) Google (Gmail/Calendar/Contacts)")
    typer.echo("2) Microsoft (Mail/Calendar/Contacts)")
    typer.echo("3) IMAP/SMTP")
    typer.echo("4) CalDAV")
    typer.echo("5) CardDAV")
    typer.echo("0) Exit")
    choice = typer.prompt("Select provider", default="1").strip()

    if choice == "1":
        _ensure_google_client(s)
        scopes = typer.prompt("Scopes (comma separated)", default="gmail,calendar,contacts")
        auth_google(scopes=scopes)
        return {"ok": True, "bound": "google"}

    if choice == "2":
        _ensure_ms_client(s)
        scopes = typer.prompt("Scopes (comma separated)", default="mail,calendar,contacts")
        auth_microsoft(scopes=scopes)
        return {"ok": True, "bound": "microsoft"}

    if choice == "3":
        email = typer.prompt("Email address")
        imap_host = typer.prompt("IMAP host", default="imap.gmail.com")
        smtp_host = typer.prompt("SMTP host", default="smtp.gmail.com")
        typer.echo("You will be prompted for app password securely (input hidden).")
        auth_imap(email=email, imap_host=imap_host, smtp_host=smtp_host)
        return {"ok": True, "bound": "imap", "email": email}

    if choice == "4":
        username = typer.prompt("CalDAV username")
        host = typer.prompt("CalDAV host (without scheme)")
        typer.echo("You will be prompted for app password securely (input hidden).")
        auth_caldav(username=username, host=host)
        return {"ok": True, "bound": "caldav", "username": username}

    if choice == "5":
        username = typer.prompt("CardDAV username")
        host = typer.prompt("CardDAV host (without scheme)")
        typer.echo("You will be prompted for app password securely (input hidden).")
        auth_carddav(username=username, host=host)
        return {"ok": True, "bound": "carddav", "username": username}

    return {"ok": True, "bound": None, "message": "No change"}


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
