from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .bind import bind_list, bind_menu, bind_provider, bind_update_account
from .wizard import run_wizard
from ..core.config import Settings
from ..core.dbkey_backend import (
    BACKEND_KEYCHAIN,
    BACKEND_LOCAL,
    BACKEND_SYSTEMD,
    default_local_dbkey_path,
    delete_dbkey,
    detect_backends,
    generate_dbkey,
    normalize_backend,
    pick_backend,
    read_dbkey,
    write_dbkey,
)
from ..core.jobs import (
    cache_latest_result,
    config_checklist,
    doctor_report,
    ensure_config_confirmed,
    get_cached_result,
    mark_config_reviewed,
    run_jobs,
    should_offer_bind_interactive,
)
from ..core.logging import configure_logging, get_logger, log_event
from ..flows.billing import billing_analyze, billing_detect, billing_month
from ..flows.calendar import agenda, calendar_event
from ..flows.ingest import inbox_ingest_day, inbox_poll, inbox_read
from ..flows.analysis import analysis_list, analysis_record
from ..flows.reply import (
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
from ..flows.summary import daily_summary
from ..flows.triage import triage_day, triage_suggest
from ..connectors.providers.caldav import auth_caldav
from ..connectors.providers.carddav import auth_carddav
from ..connectors.providers.google_gmail import auth_google
from ..connectors.providers.imap_smtp import auth_imap
from ..connectors.providers.ms_graph import auth_microsoft
from ..core.store import DB
from ..shared.time import utc_now_iso


app = typer.Typer(
    no_args_is_help=True,
    help=(
        "MailHub unified CLI.\n\n"
        "Primary entrypoints:\n"
        "- mailhub bind      (provider/account binding)\n"
        "- mailhub mail      (mail workflow)\n"
        "- mailhub calendar  (calendar workflow)\n"
        "- mailhub summary   (mail/calendar summary)\n"
        "- mailhub openclaw  (OpenClaw bridge)\n\n"
        "Automation:\n"
        "- mailhub mail run\n"
        "- mailhub mail loop\n\n"
        "Configuration:\n"
        "- mailhub wizard\n"
        "- mailhub dbkey-setup\n"
        "- mailhub settings-show\n"
        "- mailhub settings-set <key> <value>\n\n"
        "Mode behavior:\n"
        "- standalone: `mail/calendar/summary` support interactive menu flow.\n"
        "- openclaw: keep direct command execution for SKILL/agent routing.\n\n"
        "Run `mailhub <entrypoint> --help` for detailed options."
    ),
)
console = Console()
configure_logging()
logger = get_logger(__name__)


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
    log_event(
        logger,
        "cli_error",
        level="error",
        stage=stage,
        error_type=exc.__class__.__name__,
        message=msg,
    )
    console.print(payload)
    raise typer.Exit(code=1)


def _backend_display_label(name: str) -> str:
    if name == BACKEND_KEYCHAIN:
        return "Keychain"
    if name == BACKEND_SYSTEMD:
        return "systemd credentials"
    return "Local dbkey.enc"


def _healthcheck_db_cipher(settings: Settings, backend: str, local_dbkey_path) -> Dict[str, Any]:
    key = read_dbkey(
        backend=backend,
        state_dir=settings.state_dir,
        local_dbkey_path=local_dbkey_path,
        keychain_account=settings.effective_dbkey_keychain_account(),
    )
    db = DB(settings.db_path, dbkey=key)
    db.init()
    probe_value = utc_now_iso()
    db.kv_set("health.sqlcipher_probe", probe_value, utc_now_iso())
    got = db.kv_get("health.sqlcipher_probe")
    if got != probe_value:
        raise RuntimeError("SQLCipher probe read/write mismatch")
    return {"ok": True, "db_path": str(settings.db_path), "probe_key": "health.sqlcipher_probe"}


def _prompt_dbkey_backend_choice(available_backends: List[str]) -> str:
    mapping = {str(i + 1): b for i, b in enumerate(available_backends)}
    console.print("[bold]Select dbkey storage backend[/bold]")
    for i, b in enumerate(available_backends, start=1):
        if b == BACKEND_LOCAL:
            suffix = " (lower security if whole state dir leaks)"
        elif b == BACKEND_KEYCHAIN:
            suffix = " (recommended)"
        else:
            suffix = ""
        console.print(f"{i}) {_backend_display_label(b)}{suffix}")
    choice = typer.prompt("Select", default="1").strip()
    return mapping.get(choice, available_backends[0])


def _render_doctor(report: Dict[str, Any], *, full: bool) -> None:
    ok = bool(report.get("ok"))
    title = "MailHub Doctor: PASS" if ok else "MailHub Doctor: FAIL"
    style = "green" if ok else "red"
    console.print(Panel.fit(title, style=style))

    v = report.get("version", {})
    mode = report.get("settings", {}).get("mode", "")
    console.print(
        f"[bold]Version[/bold] mailhub={v.get('mailhub','')} python={v.get('python','')} mode={mode}"
    )

    checks = report.get("checks", []) or []
    t = Table(title="Checks", show_header=True, header_style="bold")
    t.add_column("Check", style="cyan")
    t.add_column("Status", width=8)
    t.add_column("Details")
    for c in checks:
        c_ok = bool(c.get("ok"))
        status = "[green]OK[/green]" if c_ok else "[red]FAIL[/red]"
        details = c.get("error") or c.get("backend") or ""
        t.add_row(str(c.get("name") or ""), status, str(details))
    console.print(t)

    dbkey_backend = report.get("settings", {}).get("dbkey_backend", "")
    detection = report.get("settings", {}).get("dbkey_detection", {}) or {}
    if dbkey_backend:
        dt = Table(title="DB Key Security", show_header=True, header_style="bold")
        dt.add_column("Backend")
        dt.add_column("Available")
        dt.add_column("Reason")
        dt.add_column("Suggestion")
        if full:
            dt.add_column("Evidence")
        for backend in (BACKEND_KEYCHAIN, BACKEND_SYSTEMD, BACKEND_LOCAL):
            item = detection.get(backend, {}) if isinstance(detection, dict) else {}
            available = bool(item.get("available"))
            label = _backend_display_label(backend)
            if backend == dbkey_backend:
                label = f"{label} (selected)"
            row = [
                label,
                "[green]yes[/green]" if available else "[red]no[/red]",
                str(item.get("reason") or ""),
                str(item.get("suggestion") or ""),
            ]
            if full:
                row.append(str(item.get("evidence") or ""))
            dt.add_row(*row)
        console.print(dt)
        if dbkey_backend == BACKEND_LOCAL:
            console.print(
                "[bold yellow]WARNING:[/bold yellow] local dbkey backend is selected; if the whole state directory leaks, offline decryption cannot be prevented."
            )

    providers = report.get("providers", {}) or {}
    console.print(
        f"[bold]Providers[/bold] total={providers.get('total',0)} by_kind={providers.get('by_kind',{})}"
    )

    warnings = report.get("warnings", []) or []
    if warnings:
        wt = Table(title="Warnings", show_header=False)
        wt.add_column("Warning", style="yellow")
        for w in warnings:
            wt.add_row(str(w))
        console.print(wt)

    errors = report.get("errors", []) or []
    if errors:
        et = Table(title="Errors", show_header=False)
        et.add_column("Error", style="red")
        for e in errors:
            et.add_row(str(e))
        console.print(et)

    if full:
        db_stats = report.get("db_stats", {})
        st = Table(title="DB Stats", show_header=True, header_style="bold")
        st.add_column("Table")
        st.add_column("Rows", justify="right")
        for name, count in (db_stats or {}).items():
            st.add_row(str(name), str(count))
        console.print(st)
        if providers.get("items"):
            pt = Table(title="Provider Accounts", show_header=True, header_style="bold")
            pt.add_column("ID")
            pt.add_column("Kind")
            pt.add_column("Alias")
            pt.add_column("Email")
            for item in providers.get("items", []):
                pt.add_row(
                    str(item.get("id") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("alias") or ""),
                    str(item.get("email") or ""),
                )
            console.print(pt)


def _normalize_section(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v == "calendar":
        return "calendar"
    if v in ("mail", "bind", "summary"):
        return v
    return ""


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(raw: str, *, fallback: datetime) -> datetime:
    v = (raw or "").strip()
    if not v:
        return fallback
    p = v.replace("Z", "+00:00")
    dt = datetime.fromisoformat(p)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prompt_bool(label: str, default: bool = True) -> bool:
    base = "true" if default else "false"
    raw = typer.prompt(label, default=base).strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _menu_select(title: str, options: Dict[str, str], *, default: str) -> str:
    console.print(title)
    resolved: Dict[str, str] = {}
    for k, label in options.items():
        token = label
        view = label
        if "|" in label:
            token, view = label.split("|", 1)
            token = token.strip()
            view = view.strip()
        resolved[k] = token
        resolved[token.lower()] = token
        console.print(f"{k}) {view}")
    choice = typer.prompt("Select", default=default).strip().lower()
    return resolved.get(choice, "")


def _require_tty_for_interactive(entrypoint: str) -> None:
    if sys.stdin.isatty():
        return
    console.print(
        {
            "ok": False,
            "reason": "interactive_tty_required",
            "entrypoint": entrypoint,
            "hint": f"Use `mailhub {entrypoint} --help` to run non-interactive commands.",
        }
    )
    raise typer.Exit(code=2)


def _summary_range_utc(datetime_range_raw: str) -> tuple[datetime, datetime]:
    raw = (datetime_range_raw or "").strip().lower()
    now = datetime.now(timezone.utc)
    day0 = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    if not raw or raw == "today":
        return day0, day0 + timedelta(days=1)
    if raw == "tomorrow":
        return day0 + timedelta(days=1), day0 + timedelta(days=2)
    if raw == "yesterday":
        return day0 - timedelta(days=1), day0
    if raw == "past_week":
        return now - timedelta(days=7), now
    if raw == "this_week":
        start = day0 - timedelta(days=day0.weekday())
        return start, start + timedelta(days=7)
    if raw == "this_week_remaining":
        end = day0 + timedelta(days=(7 - day0.weekday()))
        return now, end
    if raw == "next_week":
        start = day0 - timedelta(days=day0.weekday()) + timedelta(days=7)
        return start, start + timedelta(days=7)
    if "/" in raw:
        a, b = raw.split("/", 1)
        start = _parse_iso_utc(a, fallback=now)
        end = _parse_iso_utc(b, fallback=now + timedelta(days=1))
        if end <= start:
            return end, start
        return start, end
    dt = _parse_iso_utc(raw, fallback=now)
    return dt, dt + timedelta(days=1)


def _run_summary(*, include_mail: bool, include_calendar: bool, datetime_range_raw: str) -> Dict[str, Any]:
    if not include_mail and not include_calendar:
        return {
            "ok": False,
            "reason": "summary_target_required",
            "hint": "Use --mail and/or --calendar.",
        }

    out: Dict[str, Any] = {
        "ok": True,
        "targets": {"mail": include_mail, "calendar": include_calendar},
        "datetime_range": datetime_range_raw or "today",
    }
    start_dt, end_dt = _summary_range_utc(datetime_range_raw)
    start_utc = _iso_utc(start_dt)
    end_utc = _iso_utc(end_dt)
    out["window"] = {"start_utc": start_utc, "end_utc": end_utc}

    if include_mail:
        s = Settings.load()
        db = DB(s.db_path)
        db.init()
        messages = db.list_messages_in_range(start_utc=start_utc, end_utc=end_utc, limit=5000)
        tag_counts = db.list_tag_counts_in_range(start_utc=start_utc, end_utc=end_utc)
        reply_counts = db.reply_status_counts_in_range(start_utc=start_utc, end_utc=end_utc)
        by_type = {k: int(v) for k, v in tag_counts}
        top_subjects = [
            {
                "mail_id": int(m.get("mail_id") or 0),
                "message_id": str(m.get("id") or ""),
                "date_utc": str(m.get("date_utc") or ""),
                "from": str(m.get("from_addr") or ""),
                "subject": str(m.get("subject") or ""),
            }
            for m in messages[:20]
        ]
        out["mail_summary"] = {
            "window": {"start_utc": start_utc, "end_utc": end_utc},
            "stats": {
                "total": len(messages),
                "by_type": by_type,
                "replied": int(reply_counts.get("sent", 0)),
                "suggested_not_replied": int(reply_counts.get("pending", 0)),
                "auto_replied": int(reply_counts.get("auto_sent", 0)),
            },
            "top_subjects": top_subjects,
            "summary_text": (
                f"Mail summary {start_utc} ~ {end_utc}: "
                f"total={len(messages)}, replied={int(reply_counts.get('sent', 0))}, "
                f"suggested_not_replied={int(reply_counts.get('pending', 0))}, "
                f"auto_replied={int(reply_counts.get('auto_sent', 0))}, "
                f"types={by_type if by_type else {}}."
            ),
        }

    if include_calendar:
        out["calendar_summary"] = calendar_event(
            event="summary",
            datetime_range_raw=(datetime_range_raw or "this_week_remaining"),
        )

    return out


def _mail_standalone_interactive() -> Dict[str, Any]:
    s = Settings.load()
    history: List[Dict[str, Any]] = []
    while True:
        action = _menu_select(
            "Mail standalone menu",
            {
                "1": "run_workflow|Run workflow",
                "2": "inbox_poll|Inbox poll",
                "3": "inbox_ingest|Inbox ingest",
                "4": "inbox_read|Inbox read",
                "5": "reply_compose|Reply compose",
                "6": "reply_auto|Auto reply",
                "7": "send_queue|Send queue",
                "8": "reply_center|Reply center",
                "9": "reply_prepare|Reply prepare",
                "10": "reply_revise|Reply revise",
                "11": "reply_send|Reply send",
                "12": "reply_sent_list|Reply sent-list",
                "13": "reply_suggested_list|Reply suggested-list",
                "0": "exit|Exit",
            },
            default="1",
        )
        if not action:
            console.print({"ok": False, "reason": "invalid_action"})
            continue
        if action == "exit":
            return {"ok": True, "history": history}

        if action == "run_workflow":
            since = typer.prompt("Poll window", default=s.mail.poll_since).strip() or s.mail.poll_since
            bind_if_needed = _prompt_bool("Bind if needed", default=True)
            out = run_jobs(since=since)
            if bind_if_needed and should_offer_bind_interactive(out):
                out["bind"] = bind_menu()
                if out["bind"].get("bound"):
                    out["after_bind"] = run_jobs(since=since)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "inbox_poll":
            since = typer.prompt("Poll window", default=s.mail.poll_since).strip() or s.mail.poll_since
            mode = typer.prompt("Mode (alerts|jobs)", default="alerts").strip() or "alerts"
            out = inbox_poll(since=since, mode=mode)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "inbox_ingest":
            date = typer.prompt("Date (today|YYYY-MM-DD)", default="today").strip() or "today"
            out = inbox_ingest_day(date=date)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "inbox_read":
            message_id = typer.prompt("Message id (mail id or message id)").strip()
            include_raw = _prompt_bool("Include raw payload", default=False)
            out = inbox_read(message_id=message_id, include_raw=include_raw)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_compose":
            message_id = typer.prompt("Message id (mail id or message id)").strip()
            mode = _menu_select(
                "Compose mode",
                {"1": "auto|Auto", "2": "optimize|Optimize", "3": "raw|Raw", "0": "back|Back"},
                default="1",
            )
            if mode == "back":
                continue
            if not mode:
                console.print({"ok": False, "reason": "invalid_compose_mode"})
                continue
            content = ""
            if mode in ("optimize", "raw"):
                content = typer.prompt("Content / instruction", default="").strip()
            review = _prompt_bool("Interactive review loop", default=True)
            out = reply_compose(message_id=message_id, mode=mode, content=content, review=review)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_auto":
            since = typer.prompt("Poll window", default=s.mail.poll_since).strip() or s.mail.poll_since
            dry_run_default = s.mail.auto_reply_send != "on"
            dry_run = _prompt_bool("Dry run", default=dry_run_default)
            out = reply_auto(since=since, dry_run=dry_run)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "send_queue":
            send_action = _menu_select(
                "Send queue action",
                {
                    "1": "list|List pending",
                    "2": "send_one|Send one",
                    "3": "send_all|Send all",
                    "0": "back|Back",
                },
                default="1",
            )
            if send_action == "back":
                continue
            if send_action == "list":
                limit = int(typer.prompt("List limit", default="200").strip() or "200")
                out = send_queue_list(limit=limit)
                history.append({"action": "send_queue.list", "result": out})
                console.print(out)
                continue
            if send_action == "send_all":
                limit = int(typer.prompt("Send-all limit", default="200").strip() or "200")
                out = send_queue_send_all(confirm=True, limit=limit, bypass_message=True)
                history.append({"action": "send_queue.send_all", "result": out})
                console.print(out)
                continue
            if send_action == "send_one":
                reply_id = int(typer.prompt("Reply queue id").strip())
                use_message = _prompt_bool("Provide message payload", default=False)
                payload: Dict[str, Any] | None = None
                bypass = True
                if use_message:
                    context = typer.prompt("context (required)", default="").strip()
                    if not context:
                        out = {"ok": False, "reason": "message_context_required"}
                        history.append({"action": "send_queue.send_one", "result": out})
                        console.print(out)
                        continue
                    payload = {"context": context}
                    subject = typer.prompt("Subject (optional)", default="").strip()
                    to_addr = typer.prompt("to (optional)", default="").strip()
                    from_addr = typer.prompt("from (optional)", default="").strip()
                    if subject:
                        payload["Subject"] = subject
                    if to_addr:
                        payload["to"] = to_addr
                    if from_addr:
                        payload["from"] = from_addr
                    bypass = False
                out = send_queue_send_one(
                    reply_id=reply_id,
                    confirm=True,
                    message_payload=payload,
                    bypass_message=bypass,
                )
                history.append({"action": "send_queue.send_one", "result": out})
                console.print(out)
                continue
            console.print({"ok": False, "reason": "invalid_send_action"})
            continue

        if action == "reply_center":
            date = typer.prompt("Date (today|YYYY-MM-DD)", default="today").strip() or "today"
            out = reply_center(date=date)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_prepare":
            use_id = _prompt_bool("Use reply id? (otherwise use index)", default=True)
            if use_id:
                rid = int(typer.prompt("Reply queue id").strip())
                out = reply_prepare(reply_id=rid)
            else:
                idx = int(typer.prompt("Pending index (1-based)", default="1").strip() or "1")
                out = reply_prepare(index=idx)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_revise":
            rid = int(typer.prompt("Reply queue id").strip())
            mode = _menu_select(
                "Revise mode",
                {"1": "optimize|Optimize", "2": "raw|Raw", "0": "back|Back"},
                default="1",
            )
            if mode == "back":
                continue
            content = typer.prompt("Content / instruction", default="").strip()
            out = reply_revise(reply_id=rid, mode=mode, content=content)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_send":
            rid = int(typer.prompt("Reply queue id").strip())
            confirm_text = typer.prompt("Confirm text (must include send)", default="send").strip() or "send"
            use_message = _prompt_bool("Provide message payload", default=True)
            payload: Dict[str, Any] | None = None
            bypass = not use_message
            if use_message:
                context = typer.prompt("context (required)", default="").strip()
                if not context:
                    out = {"ok": False, "reason": "message_context_required"}
                    history.append({"action": action, "result": out})
                    console.print(out)
                    continue
                payload = {"context": context}
                subject = typer.prompt("Subject (optional)", default="").strip()
                to_addr = typer.prompt("to (optional)", default="").strip()
                from_addr = typer.prompt("from (optional)", default="").strip()
                if subject:
                    payload["Subject"] = subject
                if to_addr:
                    payload["to"] = to_addr
                if from_addr:
                    payload["from"] = from_addr
            out = reply_send(
                reply_id=rid,
                confirm_text=confirm_text,
                message_payload=payload,
                bypass_message=bypass,
            )
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_sent_list":
            date = typer.prompt("Date (today|YYYY-MM-DD)", default="today").strip() or "today"
            limit = int(typer.prompt("Limit", default="50").strip() or "50")
            out = reply_sent_list(date=date, limit=limit)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        if action == "reply_suggested_list":
            date = typer.prompt("Date (today|YYYY-MM-DD)", default="today").strip() or "today"
            limit = int(typer.prompt("Limit", default="50").strip() or "50")
            out = reply_suggested_list(date=date, limit=limit)
            history.append({"action": action, "result": out})
            console.print(out)
            continue

        console.print({"ok": False, "reason": "unsupported_action"})


def _calendar_standalone_interactive() -> Dict[str, Any]:
    history: List[Dict[str, Any]] = []
    while True:
        action = _menu_select(
            "Calendar standalone menu",
            {
                "1": "view|View events",
                "2": "add|Add event",
                "3": "delete|Delete event",
                "4": "sync|Sync events",
                "5": "summary|Summary",
                "6": "remind|Reminder",
                "0": "exit|Exit",
            },
            default="1",
        )
        if not action:
            console.print({"ok": False, "reason": "invalid_action"})
            continue
        if action == "exit":
            return {"ok": True, "history": history}

        kwargs: Dict[str, Any] = {
            "event": action,
            "datetime_raw": "",
            "datetime_range_raw": "",
            "title": "",
            "location": "",
            "context": "",
            "provider_id": "",
            "event_id": "",
            "duration_minutes": 30,
        }

        if action in ("view", "sync", "summary"):
            kwargs["datetime_range_raw"] = (
                typer.prompt("datetime-range", default="this_week_remaining").strip()
                or "this_week_remaining"
            )
        elif action == "remind":
            kwargs["datetime_range_raw"] = typer.prompt("datetime-range", default="tomorrow").strip() or "tomorrow"
        elif action == "add":
            kwargs["datetime_raw"] = typer.prompt("datetime (ISO8601, optional)", default="").strip()
            if not kwargs["datetime_raw"]:
                kwargs["datetime_range_raw"] = typer.prompt(
                    "datetime-range (start/end or keyword)",
                    default="",
                ).strip()
            kwargs["title"] = typer.prompt("title", default="").strip()
            kwargs["location"] = typer.prompt("location", default="").strip()
            kwargs["context"] = typer.prompt("context", default="").strip()
            kwargs["provider_id"] = typer.prompt("provider-id (optional)", default="").strip()
            kwargs["duration_minutes"] = int(typer.prompt("duration-minutes", default="30").strip() or "30")
        elif action == "delete":
            kwargs["provider_id"] = typer.prompt("provider-id (optional)", default="").strip()
            kwargs["event_id"] = typer.prompt("event-id").strip()

        out = calendar_event(**kwargs)
        cache_latest_result("calendar", out)
        history.append({"action": action, "result": out})
        console.print(out)


def _summary_standalone_interactive() -> Dict[str, Any]:
    history: List[Dict[str, Any]] = []
    while True:
        scope = _menu_select(
            "Summary standalone scope",
            {
                "1": "mail|Mail only",
                "2": "calendar|Calendar only",
                "3": "both|Mail + Calendar",
                "0": "exit|Exit",
            },
            default="3",
        )
        if not scope:
            console.print({"ok": False, "reason": "invalid_scope"})
            continue
        if scope == "exit":
            return {"ok": True, "history": history}

        datetime_range_raw = typer.prompt("datetime-range", default="today").strip() or "today"
        include_mail = scope in ("mail", "both")
        include_calendar = scope in ("calendar", "both")
        out = _run_summary(
            include_mail=include_mail,
            include_calendar=include_calendar,
            datetime_range_raw=datetime_range_raw,
        )
        cache_latest_result("summary", out)
        history.append({"scope": scope, "result": out})
        console.print(out)


def _openclaw_human_summary(section: str, result: Dict[str, Any], *, source: str) -> str:
    sec = _normalize_section(section)
    if source == "cached_background_result":
        updated = str(result.get("updated_at") or "")
        return f"Loaded cached {sec} result{f' at {updated}' if updated else ''}."

    if sec == "mail":
        poll = (result.get("steps") or {}).get("poll") or {}
        items = poll.get("items") or []
        return f"Mail workflow executed. Polled {len(items)} item(s)."
    if sec == "calendar":
        count = int(result.get("count", 0)) if isinstance(result, dict) else 0
        return f"Calendar query executed. Returned {count} event(s)."
    if sec == "summary":
        m = (result.get("mail_summary") or {}).get("stats") or {}
        c = result.get("calendar_summary") or {}
        return (
            f"Summary executed. Mail total={int(m.get('total', 0))}; "
            f"calendar events={int(c.get('count', 0) if isinstance(c, dict) else 0)}."
        )
    if sec == "bind":
        return "Bind flow executed."
    return "Execution finished."


def _run_openclaw_section(
    *,
    section: str,
    since: str | None,
    datetime_range_raw: str,
    include_mail_summary: bool,
    include_calendar_summary: bool,
    bind_if_needed: bool,
) -> Dict[str, Any]:
    sec = _normalize_section(section)
    if sec == "bind":
        return bind_menu()
    if sec == "mail":
        out = run_jobs(since=since)
        if bind_if_needed and should_offer_bind_interactive(out):
            out["bind"] = bind_menu()
            if out["bind"].get("bound"):
                out["after_bind"] = run_jobs(since=since)
        cache_latest_result("mail", out)
        return out
    if sec == "calendar":
        out = calendar_event(
            event="view",
            datetime_range_raw=(datetime_range_raw or "this_week_remaining"),
        )
        cache_latest_result("calendar", out)
        return out
    if sec == "summary":
        out = _run_summary(
            include_mail=include_mail_summary,
            include_calendar=include_calendar_summary,
            datetime_range_raw=(datetime_range_raw or "today"),
        )
        cache_latest_result("summary", out)
        return out
    return {
        "ok": False,
        "reason": "invalid_section",
        "supported": ["bind", "mail", "calendar", "summary"],
    }


@app.command("dbkey-setup")
def dbkey_setup_cmd(
    backend: str = typer.Option(
        "",
        "--backend",
        help="Force backend: keychain|systemd|local",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Auto-select by priority: keychain -> systemd -> local.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Do not prompt; equivalent to --auto when backend is not specified.",
    ),
):
    """Setup SQLCipher dbkey backend and verify encrypted DB access."""
    s = Settings.load()
    s.ensure_dirs()
    local_dbkey_path = default_local_dbkey_path(s.state_dir, s.security.dbkey_local_path)
    checks = detect_backends(state_dir=s.state_dir, local_dbkey_path=local_dbkey_path)
    if sys.stdin.isatty() and not non_interactive:
        console.print("[bold]dbkey backend detection[/bold]")
        for b in (BACKEND_KEYCHAIN, BACKEND_SYSTEMD):
            chk = checks[b]
            state = "[green]available[/green]" if chk.available else "[yellow]unavailable[/yellow]"
            console.print(f"- {_backend_display_label(b)}: {state} - {chk.reason}")
            if chk.suggestion:
                console.print(f"  hint: {chk.suggestion}")

    requested = normalize_backend(backend)
    if backend and not requested:
        console.print({"ok": False, "reason": "invalid_backend", "backend": backend})
        raise typer.Exit(code=2)

    if requested:
        selected = requested
    else:
        use_auto = auto or non_interactive or not sys.stdin.isatty()
        if use_auto:
            selected = pick_backend(checks)
        else:
            available_options = [b for b in (BACKEND_KEYCHAIN, BACKEND_SYSTEMD) if checks[b].available]
            if not available_options:
                available_options = [BACKEND_LOCAL]
            selected = _prompt_dbkey_backend_choice(available_options)

    selected_check = checks.get(selected)
    if not selected_check or not selected_check.available:
        console.print(
            {
                "ok": False,
                "reason": "backend_not_available",
                "backend": selected,
                "check": selected_check.to_dict() if selected_check else None,
                "hint": (selected_check.suggestion if selected_check else ""),
            }
        )
        raise typer.Exit(code=2)

    key_written = False
    try:
        try:
            _ = read_dbkey(
                backend=selected,
                state_dir=s.state_dir,
                local_dbkey_path=local_dbkey_path,
                keychain_account=s.effective_dbkey_keychain_account(),
            )
        except Exception:
            new_key = generate_dbkey()
            write_dbkey(
                backend=selected,
                key=new_key,
                state_dir=s.state_dir,
                local_dbkey_path=local_dbkey_path,
                keychain_account=s.effective_dbkey_keychain_account(),
            )
            key_written = True

        health = _healthcheck_db_cipher(s, selected, local_dbkey_path)
    except Exception as exc:
        if key_written:
            delete_dbkey(
                backend=selected,
                state_dir=s.state_dir,
                local_dbkey_path=local_dbkey_path,
                keychain_account=s.effective_dbkey_keychain_account(),
            )
        console.print(
            {
                "ok": False,
                "reason": "dbkey_setup_failed",
                "backend": selected,
                "error": str(exc),
                "hint": "Check keychain/systemd permissions and SQLCipher dependency availability.",
            }
        )
        raise typer.Exit(code=1)

    s.security.dbkey_backend = selected
    s.security.dbkey_local_path = str(local_dbkey_path)
    s.save()

    out = {
        "ok": True,
        "selected_backend": selected,
        "health": health,
        "availability": {k: v.to_dict() for k, v in checks.items()},
        "summary": {
            "keychain": {
                "available": checks[BACKEND_KEYCHAIN].available,
                "reason": checks[BACKEND_KEYCHAIN].reason,
                "suggestion": checks[BACKEND_KEYCHAIN].suggestion,
            },
            "systemd": {
                "available": checks[BACKEND_SYSTEMD].available,
                "reason": checks[BACKEND_SYSTEMD].reason,
                "suggestion": checks[BACKEND_SYSTEMD].suggestion,
            },
            "selected": selected,
        },
        "warning": (
            "local backend selected: if the entire state directory leaks, offline decryption cannot be prevented"
            if selected == BACKEND_LOCAL
            else ""
        ),
    }
    console.print(out)


@app.command("doctor")
def doctor(
    all: bool = typer.Option(False, "--all", "-a", help="Show full doctor output including paths/account details."),
):
    """Comprehensive diagnostics for state/config/provider readiness."""
    report = doctor_report(full=all)
    _render_doctor(report, full=all)


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


@app.command("summary")
def summary_cmd(
    mail: bool = typer.Option(False, "--mail", help="Include mail summary."),
    calendar: bool = typer.Option(False, "--calendar", help="Include calendar summary."),
    datetime_range_raw: str = typer.Option(
        "",
        "--datetime-range",
        help="Range keyword/start-end for summary scope. Supports today|tomorrow|past_week|this_week|this_week_remaining|next_week|start/end.",
    ),
):
    """
    Summary entrypoint for mail and/or calendar.

    Standalone mode:
    - `mailhub summary` opens interactive scope selection.

    OpenClaw mode:
    - run directly with flags, e.g. `mailhub summary --mail --calendar --datetime-range today`.
    """
    _require_first_run_confirmation()
    s = Settings.load()
    if s.effective_mode() == "standalone" and sys.stdin.isatty() and not mail and not calendar and not datetime_range_raw.strip():
        _summary_standalone_interactive()
        return

    include_mail = mail
    include_calendar = calendar
    if not include_mail and not include_calendar:
        include_mail = True
        include_calendar = True
    out = _run_summary(
        include_mail=include_mail,
        include_calendar=include_calendar,
        datetime_range_raw=datetime_range_raw,
    )
    cache_latest_result("summary", out)
    console.print(out)


@app.command("openclaw")
def openclaw_cmd(
    section: str = typer.Option(
        "",
        "--section",
        help="bind|mail|calendar|summary. Empty in TTY will prompt for choice.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="In standalone mode, refresh immediately instead of reading cached background result.",
    ),
    since: str | None = typer.Option(None, help="Override poll window for mail refresh path."),
    datetime_range_raw: str = typer.Option(
        "",
        "--datetime-range",
        help="Calendar/summary range when refresh executes corresponding task.",
    ),
    mail: bool = typer.Option(False, "--mail", help="For summary section: include mail summary."),
    calendar: bool = typer.Option(False, "--calendar", help="For summary section: include calendar summary."),
    bind_if_needed: bool = typer.Option(
        True,
        "--bind-if-needed/--no-bind-if-needed",
        help="When immediate execution is selected, open bind flow if needed.",
    ),
):
    """
    OpenClaw bridge entrypoint.
    - openclaw mode: execute selected interface immediately.
    - standalone mode: return cached background results by default; use --refresh to execute now.
    """
    _require_first_run_confirmation()
    s = Settings.load()
    mode = s.effective_mode()

    sec = _normalize_section(section)
    if not sec:
        if not sys.stdin.isatty():
            console.print(
                {
                    "ok": False,
                    "reason": "section_required",
                    "hint": "Use --section bind|mail|calendar|summary.",
                    "choices": {"1": "bind", "2": "mail", "3": "calendar", "4": "summary"},
                }
            )
            raise typer.Exit(code=2)
        choices = {"1": "bind", "2": "mail", "3": "calendar", "4": "summary"}
        console.print("1) bind")
        console.print("2) mail")
        console.print("3) calendar")
        console.print("4) summary")
        choice = typer.prompt("Select interface", default="2").strip().lower()
        sec = choices.get(choice, _normalize_section(choice))

    if not sec:
        console.print(
            {
                "ok": False,
                "reason": "invalid_section",
                "supported": ["bind", "mail", "calendar", "summary"],
            }
        )
        raise typer.Exit(code=2)

    include_mail_summary = mail
    include_calendar_summary = calendar
    if sec == "summary" and (not include_mail_summary and not include_calendar_summary):
        include_mail_summary = True
        include_calendar_summary = True

    if mode == "standalone" and not refresh and sec != "bind":
        cached = get_cached_result(sec)
        if not cached.get("ok"):
            console.print(
                {
                    "ok": False,
                    "mode": mode,
                    "section": sec,
                    "reason": "no_cached_result",
                    "hint": "No cached result yet. Run with --refresh to execute immediately.",
                }
            )
            return
        cached_obj = cached.get("cached") or {}
        payload = cached_obj.get("payload") if isinstance(cached_obj, dict) else cached_obj
        updated_at = str(cached_obj.get("updated_at") or "") if isinstance(cached_obj, dict) else ""
        console.print(
            {
                "ok": True,
                "mode": mode,
                "section": sec,
                "source": "cached_background_result",
                "message": "Current mode is standalone; returning loaded background result. Use --refresh for immediate execution.",
                "cached_updated_at": updated_at,
                "human_summary": _openclaw_human_summary(sec, cached_obj if isinstance(cached_obj, dict) else {}, source="cached_background_result"),
                "output": payload,
            }
        )
        return

    out = _run_openclaw_section(
        section=sec,
        since=since,
        datetime_range_raw=datetime_range_raw,
        include_mail_summary=include_mail_summary,
        include_calendar_summary=include_calendar_summary,
        bind_if_needed=bind_if_needed,
    )
    source = "immediate_execution"
    console.print(
        {
            "ok": bool(out.get("ok", True)),
            "mode": mode,
            "section": sec,
            "source": source,
            "human_summary": _openclaw_human_summary(sec, out, source=source),
            "output": out,
        }
    )


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
    cold_start_days: int = typer.Option(30, "--cold-start-days", min=1, help="Initial backfill days for first incremental pull after bind."),
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
                    cold_start_days=cold_start_days,
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


mail_app = typer.Typer(
    help=(
        "Mail entrypoint.\n"
        "- standalone mode: `mailhub mail` starts interactive menu.\n"
        "- openclaw mode: use subcommands directly (run/inbox/reply)."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(mail_app, name="mail")

mail_inbox_app = typer.Typer(help="Mail inbox operations.")
mail_reply_app = typer.Typer(help="Mail reply operations.")
mail_app.add_typer(mail_inbox_app, name="inbox")
mail_app.add_typer(mail_reply_app, name="reply")


@mail_app.callback()
def _mail_root(ctx: typer.Context):
    """
    Mail unified entrypoint.

    Standalone mode:
    - `mailhub mail` opens interactive action menu.

    OpenClaw mode:
    - use subcommands directly: `run`, `inbox`, `reply`.
    """
    if ctx.invoked_subcommand:
        return
    s = Settings.load()
    mode = s.effective_mode()
    if mode == "standalone":
        _require_first_run_confirmation()
        _require_tty_for_interactive("mail")
        _mail_standalone_interactive()
        raise typer.Exit(code=0)
    console.print(ctx.get_help())
    raise typer.Exit(code=0)


@mail_app.command("run")
def _mail_run(
    since: str | None = typer.Option(None, help="Override poll window, e.g. 15m/2h/1d."),
    confirm_config: bool = typer.Option(False, "--confirm-config", help="Confirm current config on first run and continue."),
    bind_if_needed: bool = typer.Option(True, "--bind-if-needed/--no-bind-if-needed"),
):
    """Unified mail workflow (poll/triage/daily summary + optional alerts/auto-reply/scheduled tasks)."""
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


@mail_app.command("loop")
def _mail_loop(
    since: str | None = typer.Option(None, help="Override poll window, e.g. 15m/2h/1d."),
    interval_seconds: int | None = typer.Option(
        None,
        "--interval-seconds",
        help="Loop interval seconds. Default from scheduler.standalone_loop_interval_seconds.",
    ),
    max_runs: int = typer.Option(
        0,
        "--max-runs",
        help="Optional max run count. 0 means infinite loop.",
    ),
    bind_if_needed: bool = typer.Option(
        False,
        "--bind-if-needed/--no-bind-if-needed",
        help="Open bind menu if no account is configured (TTY only).",
    ),
):
    """Standalone lightweight scheduler loop (no celery/redis required)."""
    _require_first_run_confirmation()
    s = Settings.load()
    if s.effective_mode() != "standalone":
        console.print(
            {
                "ok": False,
                "reason": "standalone_mode_required",
                "hint": "Set `mailhub settings-set routing.mode standalone` before `mailhub mail loop`.",
            }
        )
        raise typer.Exit(code=2)

    interval = int(interval_seconds or s.scheduler.standalone_loop_interval_seconds or 60)
    if interval < 5:
        interval = 5

    run_count = 0
    log_event(
        logger,
        "mail_loop_start",
        mode=s.effective_mode(),
        interval_seconds=interval,
        max_runs=max_runs,
        since=since or "",
    )
    while True:
        run_started = time.time()
        log_event(logger, "mail_loop_tick_start", run_count=run_count + 1)
        out = run_jobs(since=since)
        out["loop"] = {"run_count": run_count + 1, "interval_seconds": interval}
        if bind_if_needed and should_offer_bind_interactive(out):
            out["bind"] = bind_menu()
            if out["bind"].get("bound"):
                out["after_bind"] = run_jobs(since=since)
        duration_ms = int((time.time() - run_started) * 1000)
        log_event(
            logger,
            "mail_loop_tick_done",
            run_count=run_count + 1,
            ok=bool(out.get("ok", False)),
            duration_ms=duration_ms,
            step_count=len((out.get("steps") or {}).keys()),
        )
        console.print(out)

        run_count += 1
        if max_runs > 0 and run_count >= max_runs:
            break
        time.sleep(interval)


@mail_inbox_app.command("poll")
def _mail_inbox_poll(since: str = "15m", mode: str = "alerts"):
    _require_first_run_confirmation()
    console.print(inbox_poll(since=since, mode=mode))


@mail_inbox_app.command("ingest")
def _mail_inbox_ingest(date: str = "today"):
    _require_first_run_confirmation()
    console.print(inbox_ingest_day(date=date))


@mail_inbox_app.command("read")
def _mail_inbox_read(
    message_id: str = typer.Option(..., "--id", help="Mail id (numeric) or MailHub message id."),
    include_raw: bool = typer.Option(False, "--raw", help="Include raw JSON payload."),
):
    _require_first_run_confirmation()
    console.print(inbox_read(message_id=message_id, include_raw=include_raw))


@mail_reply_app.command("prepare")
def _mail_reply_prepare(
    index: int | None = typer.Option(None, "--index"),
    reply_id: int | None = typer.Option(None, "--id"),
):
    _require_first_run_confirmation()
    console.print(reply_prepare(index=index, reply_id=reply_id))


@mail_reply_app.command("compose")
def _mail_reply_compose(
    message_id: str = typer.Option(..., "--message-id"),
    mode: str = typer.Option("auto", "--mode"),
    content: str = typer.Option("", "--content"),
    review: bool = typer.Option(True, "--review/--no-review"),
):
    _require_first_run_confirmation()
    console.print(reply_compose(message_id=message_id, mode=mode, content=content, review=review))


@mail_reply_app.command("revise")
def _mail_reply_revise(
    reply_id: int = typer.Option(..., "--id"),
    mode: str = typer.Option("optimize", "--mode"),
    content: str = typer.Option("", "--content"),
):
    _require_first_run_confirmation()
    console.print(reply_revise(reply_id=reply_id, mode=mode, content=content))


@mail_reply_app.command("send")
def _mail_reply_send(
    confirm_text: str = typer.Option(..., "--confirm-text"),
    index: int | None = typer.Option(None, "--index"),
    reply_id: int | None = typer.Option(None, "--id"),
    message: str | None = typer.Option(None, "--message"),
    bypass_message: bool = typer.Option(False, "--bypass-message", "--bypass_message"),
):
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


@mail_reply_app.command("center")
def _mail_reply_center(date: str = "today"):
    _require_first_run_confirmation()
    console.print(reply_center(date=date))


@mail_reply_app.command("auto")
def _mail_reply_auto(since: str = "15m", dry_run: bool = True):
    _require_first_run_confirmation()
    console.print(reply_auto(since=since, dry_run=dry_run))


@mail_reply_app.command("sent-list")
def _mail_reply_sent_list(date: str = "today", limit: int = 50):
    _require_first_run_confirmation()
    console.print(reply_sent_list(date=date, limit=limit))


@mail_reply_app.command("suggested-list")
def _mail_reply_suggested_list(date: str = "today", limit: int = 50):
    _require_first_run_confirmation()
    console.print(reply_suggested_list(date=date, limit=limit))


cal_app = typer.Typer(
    help=(
        "Calendar entrypoint.\n"
        "- standalone mode: `mailhub calendar` starts interactive menu.\n"
        "- openclaw mode: use `mailhub calendar --event ...` (or subcommands `event`, `agenda`)."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(cal_app, name="calendar")


@cal_app.callback()
def _calendar_root(
    ctx: typer.Context,
    event: str = typer.Option("", "--event", help="view|add|delete|sync|summary|remind"),
    datetime_raw: str = typer.Option("", "--datetime"),
    datetime_range_raw: str = typer.Option("", "--datetime-range"),
    title: str = typer.Option("", "--title"),
    location: str = typer.Option("", "--location"),
    context: str = typer.Option("", "--context"),
    provider_id: str = typer.Option("", "--provider-id"),
    event_id: str = typer.Option("", "--event-id"),
    duration_minutes: int = typer.Option(30, "--duration-minutes"),
):
    """
    Calendar unified entrypoint.

    Standalone mode:
    - `mailhub calendar` opens interactive action menu.

    OpenClaw mode:
    - use `--event` directly or subcommands (`event`, `agenda`).
    """
    if ctx.invoked_subcommand:
        return
    if event.strip():
        _require_first_run_confirmation()
        console.print(
            calendar_event(
                event=event,
                datetime_raw=datetime_raw,
                datetime_range_raw=datetime_range_raw,
                title=title,
                location=location,
                context=context,
                provider_id=provider_id,
                event_id=event_id,
                duration_minutes=duration_minutes,
            )
        )
        raise typer.Exit(code=0)
    s = Settings.load()
    mode = s.effective_mode()
    if mode == "standalone":
        _require_first_run_confirmation()
        _require_tty_for_interactive("calendar")
        _calendar_standalone_interactive()
        raise typer.Exit(code=0)
    console.print(ctx.get_help())
    raise typer.Exit(code=0)


@cal_app.command("agenda")
def _agenda(days: int = 3):
    _require_first_run_confirmation()
    console.print(agenda(days=days))


@cal_app.command("event")
def _calendar_event(
    event: str = typer.Option(..., "--event", help="view|add|delete|sync|summary|remind"),
    datetime_raw: str = typer.Option("", "--datetime", help="Normalized datetime, e.g. 2026-03-01T09:00:00Z."),
    datetime_range_raw: str = typer.Option(
        "",
        "--datetime-range",
        help="Range `start/end`, JSON {start,end}, or keyword: today|tomorrow|past_week|this_week|this_week_remaining|next_week.",
    ),
    title: str = typer.Option("", "--title", help="Event title for add."),
    location: str = typer.Option("", "--location", help="Event location for add."),
    context: str = typer.Option("", "--context", help="Event context/description for add."),
    provider_id: str = typer.Option("", "--provider-id", help="Optional provider id."),
    event_id: str = typer.Option("", "--event-id", help="Provider event id for delete."),
    duration_minutes: int = typer.Option(30, "--duration-minutes", help="Default duration for add when only --datetime is given."),
):
    _require_first_run_confirmation()
    console.print(
        calendar_event(
            event=event,
            datetime_raw=datetime_raw,
            datetime_range_raw=datetime_range_raw,
            title=title,
            location=location,
            context=context,
            provider_id=provider_id,
            event_id=event_id,
            duration_minutes=duration_minutes,
        )
    )


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


@app.command("settings-show")
def settings_show():
    """Print current effective settings snapshot."""
    s = Settings.load()
    console.print(s.as_dict())


@app.command("settings-set")
def settings_set(key: str, value: str):
    """Set settings key. Supports: general.*, mail.*, calendar.*, summary.*, scheduler.*, oauth.*, runtime.*, routing.*."""
    s = Settings.load()
    try:
        canonical_key = s.set_setting_value(key, value)
        resolved_value = s.get_setting_value(canonical_key)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except AttributeError as exc:
        raise typer.BadParameter(f"Unknown key: {key}") from exc
    except TypeError as exc:
        raise typer.BadParameter(f"Invalid value for {key}: {value}") from exc

    s.save()
    console.print({"ok": True, "set": {key: resolved_value}, "canonical_key": canonical_key})
