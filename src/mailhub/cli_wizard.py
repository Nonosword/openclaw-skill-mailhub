from __future__ import annotations

import typer
from rich.console import Console

from .config import Settings

app = typer.Typer()
console = Console()


def run_wizard() -> dict:
    s = Settings.load()
    s.ensure_dirs()
    if not s.runtime.config_reviewed:
        from .utils.time import utc_now_iso

        s.runtime.config_reviewed = True
        s.runtime.config_reviewed_at = utc_now_iso()

    console.print("[bold]MailHub setup wizard[/bold]")
    name = typer.prompt("Agent display name", default=s.toggles.agent_display_name)
    s.toggles.agent_display_name = name

    google_id = typer.prompt("Google OAuth Client ID (leave blank to skip)", default=s.oauth.google_client_id)
    if google_id.strip():
        s.oauth.google_client_id = google_id.strip()

    ms_id = typer.prompt("Microsoft OAuth Client ID (leave blank to skip)", default=s.oauth.ms_client_id)
    if ms_id.strip():
        s.oauth.ms_client_id = ms_id.strip()

    alerts = typer.prompt("Mail alerts mode (off|all|suggested)", default=s.toggles.mail_alerts_mode)
    s.toggles.mail_alerts_mode = alerts

    auto = typer.prompt("Auto reply (off|on)", default=s.toggles.auto_reply)
    s.toggles.auto_reply = auto
    auto_send = typer.prompt("Auto reply send immediately (off|on)", default=s.toggles.auto_reply_send)
    s.toggles.auto_reply_send = auto_send
    poll_since = typer.prompt("Poll window for jobs run (e.g. 15m/1h)", default=s.toggles.poll_since)
    s.toggles.poll_since = poll_since

    cal = typer.prompt("Calendar management (off|on)", default=s.toggles.calendar_management)
    s.toggles.calendar_management = cal
    if cal == "on":
        s.toggles.calendar_days_window = int(typer.prompt(
            "Calendar window days", default=str(s.toggles.calendar_days_window)))

    bill = typer.prompt("Bill analysis (off|on)", default=s.toggles.bill_analysis)
    s.toggles.bill_analysis = bill
    s.toggles.scheduler_tz = typer.prompt("Scheduler timezone (IANA)", default=s.toggles.scheduler_tz)
    s.toggles.digest_weekdays = typer.prompt(
        "Digest weekdays (comma list, mon..sun)", default=s.toggles.digest_weekdays
    )
    s.toggles.digest_times_local = typer.prompt(
        "Digest times local (comma list HH:MM)", default=s.toggles.digest_times_local
    )
    s.toggles.billing_days_of_month = typer.prompt(
        "Billing days of month (comma list 1-31)", default=s.toggles.billing_days_of_month
    )
    s.toggles.billing_times_local = typer.prompt(
        "Billing times local (comma list HH:MM)", default=s.toggles.billing_times_local
    )

    disclosure = typer.prompt("Disclosure line", default=s.toggles.disclosure_line)
    s.toggles.disclosure_line = disclosure

    confirm = typer.confirm("Confirm current config for execution?", default=s.runtime.config_confirmed or True)
    if confirm:
        from .utils.time import utc_now_iso

        s.runtime.config_confirmed = True
        s.runtime.config_confirmed_at = utc_now_iso()

    s.save()
    out = {"ok": True, "settings_path": str(s.settings_path)}
    console.print(out)
    return out


@app.command("wizard")
def wizard_cmd():
    run_wizard()
