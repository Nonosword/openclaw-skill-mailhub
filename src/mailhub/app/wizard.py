from __future__ import annotations

import getpass
import os

import typer
from rich.console import Console

from .bind import bind_menu, bind_provider
from ..core.config import Settings

app = typer.Typer()
console = Console()


def run_wizard() -> dict:
    s = Settings.load()
    s.ensure_dirs()
    _mark_reviewed(s)

    console.print("[bold]MailHub setup wizard[/bold]")
    if not s.runtime.config_confirmed:
        console.print(
            "[yellow]First-time setup detected.[/yellow] "
            "Using guided flow mode. Interactive section menu is available after first confirmation."
        )
        _run_first_time_flow(s)
    else:
        _run_interactive_flow(s)

    s.save()
    out = {"ok": True, "settings_path": str(s.settings_path), "config_confirmed": s.runtime.config_confirmed}
    console.print(out)
    return out


def _run_first_time_flow(s: Settings) -> None:
    _configure_general(s)
    _configure_routing(s)
    _configure_oauth(s)
    _configure_mail(s)
    _configure_calendar(s)
    _configure_summary(s)
    _configure_scheduler(s)

    if _prompt_bool("Run bind flow now?", default=True):
        _configure_bind(s)

    if _prompt_bool("Confirm current config for execution?", default=True):
        _mark_confirmed(s)


def _run_interactive_flow(s: Settings) -> None:
    while True:
        console.print("")
        console.print("Wizard sections")
        console.print("1) routing/mode")
        console.print("2) bind/accounts")
        console.print("3) mail")
        console.print("4) calendar")
        console.print("5) summary")
        console.print("6) scheduler")
        console.print("7) oauth")
        console.print("8) general")
        console.print("9) confirm + finish")
        console.print("0) finish")
        ch = typer.prompt("Select section", default="3").strip()

        if ch == "1":
            _configure_routing(s)
            continue
        if ch == "2":
            _configure_bind(s)
            continue
        if ch == "3":
            _configure_mail(s)
            continue
        if ch == "4":
            _configure_calendar(s)
            continue
        if ch == "5":
            _configure_summary(s)
            continue
        if ch == "6":
            _configure_scheduler(s)
            continue
        if ch == "7":
            _configure_oauth(s)
            continue
        if ch == "8":
            _configure_general(s)
            continue
        if ch == "9":
            _mark_confirmed(s)
            break
        if ch == "0":
            break

        console.print({"ok": False, "reason": "invalid_section_choice"})


def _configure_general(s: Settings) -> None:
    s.general.agent_display_name = typer.prompt("Agent display name", default=s.general.agent_display_name)
    s.general.disclosure_line = typer.prompt("Disclosure line", default=s.general.disclosure_line)


def _configure_routing(s: Settings) -> None:
    mode = typer.prompt("Runtime mode (openclaw|standalone)", default=s.routing.mode).strip().lower()
    if mode in ("openclaw", "standalone"):
        s.routing.mode = mode
    s.routing.openclaw_json_path = typer.prompt("OpenClaw JSON path", default=s.routing.openclaw_json_path)
    s.routing.standalone_agent_enabled = _prompt_bool(
        "Standalone agent enabled?", default=bool(s.routing.standalone_agent_enabled)
    )
    s.routing.standalone_models_path = typer.prompt(
        "Standalone models path (blank = default)",
        default=s.routing.standalone_models_path,
    )


def _configure_oauth(s: Settings) -> None:
    google_id_default = s.effective_google_client_id() or s.oauth.google_client_id
    google_id = typer.prompt("Google OAuth Client ID (blank keeps)", default=google_id_default).strip()
    if google_id:
        s.oauth.google_client_id = google_id

    if os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip():
        console.print("[dim]Using GOOGLE_OAUTH_CLIENT_SECRET from environment.[/dim]")
    else:
        secret = getpass.getpass("Google OAuth Client Secret (hidden, blank keeps): ").strip()
        if secret:
            s.oauth.google_client_secret = secret

    ms_id_default = s.effective_ms_client_id() or s.oauth.ms_client_id
    ms_id = typer.prompt("Microsoft OAuth Client ID (blank keeps)", default=ms_id_default).strip()
    if ms_id:
        s.oauth.ms_client_id = ms_id


def _configure_mail(s: Settings) -> None:
    s.mail.alerts_mode = typer.prompt("Mail alerts mode (off|all|suggested)", default=s.mail.alerts_mode).strip()
    s.mail.auto_reply = typer.prompt("Auto reply (off|on)", default=s.mail.auto_reply).strip()
    s.mail.auto_reply_send = typer.prompt("Auto reply send immediately (off|on)", default=s.mail.auto_reply_send).strip()
    s.mail.poll_since = typer.prompt(
        "Jobs run interval window (e.g. 15m/1h) [scheduler cadence window]",
        default=s.mail.poll_since,
    ).strip()
    s.mail.suggest_max_items = _prompt_int("Suggest max items", default=s.mail.suggest_max_items, min_value=1)
    s.mail.reply_needed_max_items = _prompt_int(
        "Reply-needed max items",
        default=s.mail.reply_needed_max_items,
        min_value=1,
    )
    s.mail.fetch.default_cold_start_days = _prompt_int(
        "Fetch default cold start days",
        default=s.mail.fetch.default_cold_start_days,
        min_value=1,
    )
    s.mail.fetch.max_results_per_page = _prompt_int(
        "Fetch max results per page",
        default=s.mail.fetch.max_results_per_page,
        min_value=1,
    )
    s.mail.fetch.min_results_per_page = _prompt_int(
        "Fetch min results per page",
        default=s.mail.fetch.min_results_per_page,
        min_value=1,
    )
    s.mail.fetch.max_pages_per_run = _prompt_int(
        "Fetch max pages per run",
        default=s.mail.fetch.max_pages_per_run,
        min_value=1,
    )
    s.mail.fetch.backoff_retries = _prompt_int(
        "Fetch backoff retries (429/403)",
        default=s.mail.fetch.backoff_retries,
        min_value=0,
    )
    s.mail.fetch.backoff_initial_seconds = _prompt_int(
        "Fetch backoff initial seconds",
        default=s.mail.fetch.backoff_initial_seconds,
        min_value=1,
    )
    s.mail.fetch.backoff_max_seconds = _prompt_int(
        "Fetch backoff max seconds",
        default=s.mail.fetch.backoff_max_seconds,
        min_value=1,
    )

    s.mail.billing.analysis_mode = typer.prompt("Bill analysis (off|on)", default=s.mail.billing.analysis_mode).strip()
    s.mail.billing.days_of_month = typer.prompt(
        "Billing days of month (comma list 1-31)",
        default=s.mail.billing.days_of_month,
    ).strip()
    s.mail.billing.trigger_times_local = typer.prompt(
        "Billing trigger times local (comma HH:MM)",
        default=s.mail.billing.trigger_times_local,
    ).strip()


def _configure_calendar(s: Settings) -> None:
    s.calendar.management_mode = typer.prompt(
        "Calendar management mode (off|on)",
        default=s.calendar.management_mode,
    ).strip()
    s.calendar.days_window = _prompt_int("Calendar window days", default=s.calendar.days_window, min_value=1)
    s.calendar.reminder.enabled = _prompt_bool("Calendar reminder enabled?", default=s.calendar.reminder.enabled)
    s.calendar.reminder.in_jobs_run = _prompt_bool(
        "Calendar reminder in mail run flow?",
        default=s.calendar.reminder.in_jobs_run,
    )
    s.calendar.reminder.range = typer.prompt(
        "Calendar reminder range",
        default=s.calendar.reminder.range,
    ).strip()
    s.calendar.reminder.weekdays = typer.prompt(
        "Calendar reminder weekdays (comma mon..sun)",
        default=s.calendar.reminder.weekdays,
    ).strip()
    s.calendar.reminder.trigger_times_local = typer.prompt(
        "Calendar reminder trigger times (comma HH:MM)",
        default=s.calendar.reminder.trigger_times_local,
    ).strip()


def _configure_summary(s: Settings) -> None:
    s.summary.enabled = _prompt_bool("Summary enabled?", default=s.summary.enabled)
    s.summary.in_jobs_run = _prompt_bool("Summary in mail run flow?", default=s.summary.in_jobs_run)
    s.summary.range = typer.prompt("Summary range", default=s.summary.range).strip()
    s.summary.weekdays = typer.prompt("Summary weekdays (comma mon..sun)", default=s.summary.weekdays).strip()
    s.summary.trigger_times_local = typer.prompt(
        "Summary trigger times (comma HH:MM)",
        default=s.summary.trigger_times_local,
    ).strip()


def _configure_scheduler(s: Settings) -> None:
    s.scheduler.tz = typer.prompt("Scheduler timezone (IANA)", default=s.scheduler.tz).strip()
    s.scheduler.digest_weekdays = typer.prompt(
        "Digest weekdays (comma mon..sun)",
        default=s.scheduler.digest_weekdays,
    ).strip()
    s.scheduler.digest_times_local = typer.prompt(
        "Digest times local (comma HH:MM)",
        default=s.scheduler.digest_times_local,
    ).strip()
    s.scheduler.standalone_loop_interval_seconds = _prompt_int(
        "Standalone loop interval seconds",
        default=s.scheduler.standalone_loop_interval_seconds,
        min_value=5,
    )


def _configure_bind(s: Settings) -> None:
    while True:
        console.print("")
        console.print("Bind section")
        console.print("1) Unified bind menu")
        console.print("2) Google OAuth")
        console.print("3) Google App Password (IMAP/SMTP)")
        console.print("4) Microsoft OAuth")
        console.print("5) IMAP/SMTP custom")
        console.print("6) POP3/SMTP (not supported)")
        console.print("0) Back")
        ch = typer.prompt("Select bind option", default="1").strip()

        if ch == "0":
            return
        if ch == "1":
            console.print(bind_menu())
            continue
        if ch == "2":
            alias = typer.prompt("Alias (optional)", default="")
            scopes = typer.prompt("Scopes", default="gmail,calendar,contacts")
            cold_start_days = _prompt_int("Cold start days", default=s.mail.fetch.default_cold_start_days, min_value=1)
            console.print(
                bind_provider(
                    provider="google",
                    alias=alias,
                    scopes=scopes,
                    cold_start_days=cold_start_days,
                )
            )
            continue
        if ch == "3":
            email = typer.prompt("Google email")
            alias = typer.prompt("Alias (optional)", default="")
            cold_start_days = _prompt_int("Cold start days", default=s.mail.fetch.default_cold_start_days, min_value=1)
            console.print(
                bind_provider(
                    provider="imap",
                    email=email,
                    imap_host="imap.gmail.com",
                    smtp_host="smtp.gmail.com",
                    alias=alias,
                    cold_start_days=cold_start_days,
                )
            )
            continue
        if ch == "4":
            alias = typer.prompt("Alias (optional)", default="")
            scopes = typer.prompt("Scopes", default="mail,calendar,contacts")
            cold_start_days = _prompt_int("Cold start days", default=s.mail.fetch.default_cold_start_days, min_value=1)
            console.print(
                bind_provider(
                    provider="microsoft",
                    alias=alias,
                    scopes=scopes,
                    cold_start_days=cold_start_days,
                )
            )
            continue
        if ch == "5":
            email = typer.prompt("Email")
            alias = typer.prompt("Alias (optional)", default="")
            imap_host = typer.prompt("IMAP host", default="imap.gmail.com")
            smtp_host = typer.prompt("SMTP host", default="smtp.gmail.com")
            cold_start_days = _prompt_int("Cold start days", default=s.mail.fetch.default_cold_start_days, min_value=1)
            console.print(
                bind_provider(
                    provider="imap",
                    email=email,
                    imap_host=imap_host,
                    smtp_host=smtp_host,
                    alias=alias,
                    cold_start_days=cold_start_days,
                )
            )
            continue
        if ch == "6":
            console.print(
                {
                    "ok": False,
                    "reason": "unsupported_protocol",
                    "message": "POP3/SMTP is not supported yet. Please use IMAP/SMTP.",
                }
            )
            continue

        console.print({"ok": False, "reason": "invalid_bind_choice"})


def _prompt_bool(label: str, default: bool) -> bool:
    raw = typer.prompt(label, default=("true" if default else "false")).strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _prompt_int(label: str, *, default: int, min_value: int = 0) -> int:
    raw = typer.prompt(label, default=str(default)).strip() or str(default)
    try:
        v = int(raw)
    except ValueError:
        v = int(default)
    return max(min_value, v)


def _mark_reviewed(s: Settings) -> None:
    if s.runtime.config_reviewed:
        return
    from ..shared.time import utc_now_iso

    s.runtime.config_reviewed = True
    s.runtime.config_reviewed_at = utc_now_iso()


def _mark_confirmed(s: Settings) -> None:
    from ..shared.time import utc_now_iso

    s.runtime.config_confirmed = True
    s.runtime.config_confirmed_at = utc_now_iso()


@app.command("wizard")
def wizard_cmd():
    run_wizard()
