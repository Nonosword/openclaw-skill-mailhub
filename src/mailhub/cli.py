from __future__ import annotations

import json
from typing import Any, Dict
import typer
from rich.console import Console

from .bind import bind_list, bind_menu, bind_provider, bind_update_account
from .cli_wizard import run_wizard
from .config import Settings
from .jobs import (
    config_checklist,
    doctor_report,
    ensure_config_confirmed,
    mark_config_reviewed,
    run_jobs,
    should_offer_bind_interactive,
)
from .pipelines.billing import billing_analyze, billing_detect, billing_month
from .pipelines.calendar import agenda
from .pipelines.ingest import inbox_ingest_day, inbox_poll, inbox_read
from .pipelines.analysis import analysis_list, analysis_record
from .pipelines.reply import (
    reply_auto,
    reply_center,
    reply_compose,
    reply_prepare,
    reply_revise,
    reply_send,
    reply_sent_list,
    reply_suggested_list,
    send_queue_list,
    send_queue_send_all,
    send_queue_send_one,
)
from .pipelines.summary import daily_summary
from .pipelines.triage import triage_day, triage_suggest
from .providers.caldav import auth_caldav
from .providers.carddav import auth_carddav
from .providers.google_gmail import auth_google
from .providers.imap_smtp import auth_imap
from .providers.ms_graph import auth_microsoft


app = typer.Typer(
    no_args_is_help=True,
    help=(
        "MailHub CLI for multi-account email/calendar/contacts workflows.\n\n"
        "Start with: `mailhub --help` and `mailhub <command> --help`.\n"
        "Main automation entrypoint: `mailhub jobs run`."
    ),
)
console = Console()


def _require_first_run_confirmation() -> None:
    pre = ensure_config_confirmed(confirm_config=False)
    if pre and not pre.get("ok", False):
        console.print(pre)
        raise typer.Exit(code=2)


def _print_std_error(exc: Exception, stage: str) -> None:
    msg = str(exc).strip()
    payload: Dict[str, Any] = {
        "ok": False,
        "stage": stage,
        "error_type": exc.__class__.__name__,
        "message": msg,
    }
    if msg.startswith("{"):
        try:
            payload["details"] = json.loads(msg)
        except Exception:
            payload["details_raw"] = msg
    console.print(payload)
    raise typer.Exit(code=1)


@app.command("doctor")
def doctor(
    all: bool = typer.Option(False, "--all", "-a", help="Show full doctor output including paths/account details."),
):
    """Comprehensive diagnostics for state/config/provider readiness."""
    console.print(doctor_report(full=all))


@app.command("config")
def config_cmd(
    confirm: bool = typer.Option(False, "--confirm", help="Mark current config as confirmed."),
    wizard: bool = typer.Option(False, "--wizard", help="Open interactive settings wizard."),
):
    """Review or confirm first-run settings, optionally with wizard prompts."""
    console.print(mark_config_reviewed())
    if wizard:
        run_wizard()
    if confirm:
        confirm_result = ensure_config_confirmed(confirm_config=True)
        if confirm_result:
            console.print(confirm_result)
    console.print(config_checklist(Settings.load()))


@app.command("wizard")
def wizard_cmd():
    """Open interactive settings wizard."""
    run_wizard()


@app.command("daily_summary")
def daily_summary_cmd(date: str = "today"):
    _require_first_run_confirmation()
    console.print(daily_summary(date=date))


@app.command("daily-summary")
def daily_summary_cmd_dash(date: str = "today"):
    daily_summary_cmd(date=date)


@app.command("bind")
def bind_cmd(
    confirm_config: bool = typer.Option(False, "--confirm-config", help="Confirm config and continue binding."),
    list_accounts: bool = typer.Option(False, "--list", help="List configured accounts."),
    provider: str | None = typer.Option(None, "--provider", help="google|microsoft|imap|caldav|carddav"),
    account_id: str | None = typer.Option(None, "--account-id", help="Existing account id to update."),
    scopes: str | None = typer.Option(None, "--scopes", help="OAuth scopes, e.g. gmail,calendar,contacts"),
    google_client_id: str | None = typer.Option(None, "--google-client-id", help="Google OAuth client id."),
    google_client_secret: str | None = typer.Option(None, "--google-client-secret", help="Google OAuth client secret."),
    google_code: str | None = typer.Option(None, "--google-code", help="Manual Google OAuth code (or callback URL)."),
    ms_client_id: str | None = typer.Option(None, "--ms-client-id", help="Microsoft OAuth client id."),
    email: str | None = typer.Option(None, "--email", help="Email for IMAP binding."),
    imap_host: str | None = typer.Option(None, "--imap-host", help="IMAP host for IMAP binding."),
    smtp_host: str | None = typer.Option(None, "--smtp-host", help="SMTP host for IMAP binding."),
    username: str | None = typer.Option(None, "--username", help="Username for CalDAV/CardDAV."),
    host: str | None = typer.Option(None, "--host", help="Host for CalDAV/CardDAV."),
    alias: str | None = typer.Option(None, "--alias", help="Account alias for external display."),
    is_mail: bool | None = typer.Option(None, "--is-mail/--no-mail", help="Enable/disable mail capability."),
    is_calendar: bool | None = typer.Option(None, "--is-calendar/--no-calendar", help="Enable/disable calendar capability."),
    is_contacts: bool | None = typer.Option(None, "--is-contacts/--no-contacts", help="Enable/disable contacts capability."),
):
    """Unified account binding and account-capability management."""
    pre = ensure_config_confirmed(confirm_config=confirm_config)
    if pre and not pre.get("ok", False):
        console.print(pre)
        raise typer.Exit(code=2)
    if pre and pre.get("ok"):
        console.print(pre)
    if list_accounts:
        console.print(bind_list())
        return
    if account_id:
        console.print(
            bind_update_account(
                account_id=account_id,
                alias=alias,
                is_mail=is_mail,
                is_calendar=is_calendar,
                is_contacts=is_contacts,
            )
        )
        return
    if provider:
        try:
            console.print(
                bind_provider(
                    provider=provider,
                    scopes=scopes,
                    google_client_id=google_client_id,
                    google_client_secret=google_client_secret,
                    google_code=google_code,
                    ms_client_id=ms_client_id,
                    email=email,
                    imap_host=imap_host,
                    smtp_host=smtp_host,
                    username=username,
                    host=host,
                    alias=alias,
                    is_mail=is_mail,
                    is_calendar=is_calendar,
                    is_contacts=is_contacts,
                )
            )
        except Exception as exc:
            _print_std_error(exc, "bind")
        return
    try:
        console.print(bind_menu())
    except Exception as exc:
        _print_std_error(exc, "bind")


auth_app = typer.Typer(help="Direct provider auth commands (advanced/fallback path).")
app.add_typer(auth_app, name="auth")


@auth_app.command("google")
def _auth_google(scopes: str = "gmail,calendar,contacts", code: str = ""):
    _require_first_run_confirmation()
    Settings.load().ensure_dirs()
    try:
        auth_google(scopes=scopes, manual_code=code)
    except Exception as exc:
        _print_std_error(exc, "auth_google")


@auth_app.command("microsoft")
def _auth_ms(scopes: str = "mail,calendar,contacts"):
    _require_first_run_confirmation()
    Settings.load().ensure_dirs()
    auth_microsoft(scopes=scopes)


@auth_app.command("imap")
def _auth_imap(email: str, imap_host: str, smtp_host: str):
    _require_first_run_confirmation()
    Settings.load().ensure_dirs()
    auth_imap(email=email, imap_host=imap_host, smtp_host=smtp_host)


@auth_app.command("caldav")
def _auth_caldav(username: str, host: str):
    _require_first_run_confirmation()
    Settings.load().ensure_dirs()
    auth_caldav(username=username, host=host)


@auth_app.command("carddav")
def _auth_carddav(username: str, host: str):
    _require_first_run_confirmation()
    Settings.load().ensure_dirs()
    auth_carddav(username=username, host=host)


inbox_app = typer.Typer(help="Inbox polling and ingestion commands.")
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("poll")
def _poll(since: str = "15m", mode: str = "alerts"):
    _require_first_run_confirmation()
    console.print(inbox_poll(since=since, mode=mode))


@inbox_app.command("ingest")
def _ingest(date: str = "today"):
    _require_first_run_confirmation()
    console.print(inbox_ingest_day(date=date))


@inbox_app.command("read")
def _read(
    message_id: str = typer.Option(..., "--id", help="Mail id (numeric) or MailHub message id."),
    include_raw: bool = typer.Option(False, "--raw", help="Include raw JSON payload."),
):
    """Read full content of one stored email by MailHub message id."""
    _require_first_run_confirmation()
    console.print(inbox_read(message_id=message_id, include_raw=include_raw))


triage_app = typer.Typer(help="Classification and reply-needed triage commands.")
app.add_typer(triage_app, name="triage")


@triage_app.command("day")
def _triage_day(date: str = "today"):
    _require_first_run_confirmation()
    console.print(triage_day(date=date))


@triage_app.command("suggest")
def _triage_suggest(since: str = "15m"):
    _require_first_run_confirmation()
    console.print(triage_suggest(since=since))


reply_app = typer.Typer(help="Reply draft/send/list commands.")
app.add_typer(reply_app, name="reply")


@reply_app.command("prepare")
def _reply_prepare(
    index: int | None = typer.Option(None, "--index", help="Pending queue index (1-based)."),
    reply_id: int | None = typer.Option(None, "--id", help="Stable reply queue id from list output (preferred)."),
):
    """Prepare reply draft by ID (preferred) or index fallback."""
    _require_first_run_confirmation()
    console.print(reply_prepare(index=index, reply_id=reply_id))


@reply_app.command("compose")
def _reply_compose(
    message_id: str = typer.Option(..., "--message-id", help="Mail id (numeric) or MailHub message id to reply."),
    mode: str = typer.Option("auto", "--mode", help="auto|optimize|raw"),
    content: str = typer.Option("", "--content", help="User-provided content or optimization hint."),
    review: bool = typer.Option(True, "--review/--no-review", help="Interactive a/b/c review loop in TTY."),
):
    """Create draft from message id (auto/optimize/raw) with optional review loop."""
    _require_first_run_confirmation()
    console.print(reply_compose(message_id=message_id, mode=mode, content=content, review=review))


@reply_app.command("revise")
def _reply_revise(
    reply_id: int = typer.Option(..., "--id", help="Reply queue id."),
    mode: str = typer.Option("optimize", "--mode", help="optimize|raw"),
    content: str = typer.Option("", "--content", help="Optimization hint or manual body."),
):
    """Revise an existing pending draft by reply queue id."""
    _require_first_run_confirmation()
    console.print(reply_revise(reply_id=reply_id, mode=mode, content=content))


@reply_app.command("send")
def _reply_send(
    confirm_text: str = typer.Option(..., "--confirm-text", help="Must include word 'send'."),
    index: int | None = typer.Option(None, "--index", help="Pending queue index (1-based)."),
    reply_id: int | None = typer.Option(None, "--id", help="Stable reply queue id from list output (preferred)."),
    message: str | None = typer.Option(
        None,
        "--message",
        help='JSON payload for manual send, e.g. {"Subject":"...","to":"...","from":"...","context":"..."}',
    ),
    bypass_message: bool = typer.Option(
        False,
        "--bypass-message",
        "--bypass_message",
        help="Bypass --message requirement (standalone mode only).",
    ),
):
    """Send prepared reply by ID (preferred) or index fallback."""
    _require_first_run_confirmation()
    message_payload: Dict[str, Any] | None = None
    if message:
        parsed = json.loads(message)
        if not isinstance(parsed, dict):
            raise typer.BadParameter("--message must be a JSON object.")
        message_payload = parsed
    if message_payload and bypass_message:
        raise typer.BadParameter("Do not use --message and --bypass-message together.")
    console.print(
        reply_send(
            index=index,
            reply_id=reply_id,
            confirm_text=confirm_text,
            message_payload=message_payload,
            bypass_message=bypass_message,
        )
    )


@reply_app.command("auto")
def _reply_auto(since: str = "15m", dry_run: bool = True):
    _require_first_run_confirmation()
    console.print(reply_auto(since=since, dry_run=dry_run))


@reply_app.command("sent-list")
def _reply_sent_list(date: str = "today", limit: int = 50):
    _require_first_run_confirmation()
    console.print(reply_sent_list(date=date, limit=limit))


@reply_app.command("suggested-list")
def _reply_suggested_list(date: str = "today", limit: int = 50):
    _require_first_run_confirmation()
    console.print(reply_suggested_list(date=date, limit=limit))


@reply_app.command("center")
def _reply_center(date: str = "today"):
    _require_first_run_confirmation()
    console.print(reply_center(date=date))


cal_app = typer.Typer(help="Calendar read operations.")
app.add_typer(cal_app, name="cal")


@cal_app.command("agenda")
def _agenda(days: int = 3):
    _require_first_run_confirmation()
    console.print(agenda(days=days))


billing_app = typer.Typer(help="Billing statement detection and analysis.")
app.add_typer(billing_app, name="billing")


@billing_app.command("detect")
def _detect(since: str = "30d"):
    _require_first_run_confirmation()
    console.print(billing_detect(since=since))


@billing_app.command("analyze")
def _analyze(statement_id: str):
    _require_first_run_confirmation()
    console.print(billing_analyze(statement_id=statement_id))


@billing_app.command("month")
def _month(month: str):
    _require_first_run_confirmation()
    console.print(billing_month(month=month))


analysis_app = typer.Typer(help="Persist and query analysis records.")
app.add_typer(analysis_app, name="analysis")


@analysis_app.command("record")
def analysis_record_cmd(
    message_id: str = typer.Option(..., "--message-id", help="Mail id (numeric) or MailHub message id."),
    title: str = typer.Option("", "--title"),
    summary: str = typer.Option("", "--summary"),
    tag: str = typer.Option("other", "--tag"),
    suggest_reply: bool = typer.Option(False, "--suggest-reply/--no-suggest-reply"),
    suggestion: str = typer.Option("", "--suggestion"),
    source: str = typer.Option("openclaw", "--source"),
):
    _require_first_run_confirmation()
    console.print(
        analysis_record(
            message_id=message_id,
            title=title,
            summary=summary,
            tag=tag,
            suggest_reply=suggest_reply,
            suggestion=suggestion,
            source=source,
        )
    )


@analysis_app.command("list")
def analysis_list_cmd(date: str = "today", limit: int = 200):
    _require_first_run_confirmation()
    console.print(analysis_list(date=date, limit=limit))


jobs_app = typer.Typer(help="Unified automation entrypoint.")
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("run")
def jobs_run_cmd(
    since: str | None = typer.Option(None, help="Override poll window, e.g. 15m/2h/1d."),
    confirm_config: bool = typer.Option(False, "--confirm-config", help="Confirm current config on first run and continue."),
    config: bool = typer.Option(False, "--config", help="Open interactive config wizard before running."),
    bind_if_needed: bool = typer.Option(True, "--bind-if-needed/--no-bind-if-needed", help="Open bind menu if no account is configured."),
):
    """Run poll -> triage -> summary (+ optional alerts/auto-reply/scheduled billing)."""
    if config:
        run_wizard()

    pre = ensure_config_confirmed(confirm_config=confirm_config)
    if pre and not pre.get("ok", False):
        console.print(pre)
        raise typer.Exit(code=2)
    if pre and pre.get("ok"):
        console.print(pre)

    out = run_jobs(since=since)
    if bind_if_needed and should_offer_bind_interactive(out):
        out["bind"] = bind_menu()
        if out["bind"].get("bound"):
            out["after_bind"] = run_jobs(since=since)
    console.print(out)


@app.command("send")
def send_cmd(
    reply_id: int | None = typer.Option(None, "--id", help="Reply queue id from pending send list."),
    list_: bool = typer.Option(False, "--list", help="List pending send queue. Use with --confirm to send all."),
    confirm: bool = typer.Option(False, "--confirm", help="Required for sending actions."),
    limit: int = typer.Option(200, "--limit", help="Max queue items when listing/sending all."),
    message: str | None = typer.Option(
        None,
        "--message",
        help='JSON payload for openclaw-mode send, e.g. {"Subject":"...","to":"...","from":"...","context":"..."}',
    ),
    bypass_message: bool = typer.Option(
        False,
        "--bypass-message",
        "--bypass_message",
        help="Bypass --message requirement (standalone mode only).",
    ),
):
    """Send one or all drafts from pending send queue."""
    _require_first_run_confirmation()
    try:
        message_payload: Dict[str, Any] | None = None
        if message:
            parsed = json.loads(message)
            if not isinstance(parsed, dict):
                raise typer.BadParameter("--message must be a JSON object.")
            message_payload = parsed
        if message_payload and bypass_message:
            raise typer.BadParameter("Do not use --message and --bypass-message together.")

        if list_ and message_payload:
            raise typer.BadParameter("--message is only supported with single `--id` send.")
        if list_ and confirm:
            console.print(send_queue_send_all(confirm=True, limit=limit, bypass_message=bypass_message))
            return
        if list_:
            console.print(send_queue_list(limit=limit))
            return
        if reply_id is not None:
            console.print(
                send_queue_send_one(
                    reply_id=reply_id,
                    confirm=confirm,
                    message_payload=message_payload,
                    bypass_message=bypass_message,
                )
            )
            return
        console.print(send_queue_list(limit=limit))
    except Exception as exc:
        _print_std_error(exc, "send")


@app.command("settings_show")
def settings_show():
    """Print current effective settings snapshot."""
    s = Settings.load()
    console.print(s.as_dict())


@app.command("settings-show")
def settings_show_dash():
    settings_show()


@app.command("settings_set")
def settings_set(key: str, value: str):
    """Set settings key. Supports toggles.<key>, oauth.<key>, runtime.<key>, routing.<key>."""
    s = Settings.load()

    target = s.toggles
    attr = key
    if "." in key:
        ns, attr = key.split(".", 1)
        if ns == "toggles":
            target = s.toggles
        elif ns == "oauth":
            target = s.oauth
        elif ns == "runtime":
            target = s.runtime
        elif ns == "routing":
            target = s.routing
        else:
            raise typer.BadParameter(f"Unknown settings namespace: {ns}")

    if not hasattr(target, attr):
        raise typer.BadParameter(f"Unknown key: {key}")

    cur = getattr(target, attr)
    if isinstance(cur, bool):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            setattr(target, attr, True)
        elif v in ("0", "false", "no", "off"):
            setattr(target, attr, False)
        else:
            raise typer.BadParameter(f"Invalid boolean value: {value}")
    elif isinstance(cur, int):
        setattr(target, attr, int(value))
    else:
        setattr(target, attr, value)

    s.save()
    console.print({"ok": True, "set": {key: value}})


@app.command("settings-set")
def settings_set_dash(key: str, value: str):
    settings_set(key=key, value=value)
