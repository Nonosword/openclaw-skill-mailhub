from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from .. import __version__
from .accounts import list_accounts
from .config import Settings
from .dbkey_backend import BACKEND_LOCAL, default_local_dbkey_path, detect_backends
from .security import SecretStore
from .store import DB
from ..shared.time import utc_now_iso
from ..shared.time import parse_since
from ..flows.ingest import inbox_poll
from ..flows.triage import triage_day, triage_suggest
from ..flows.reply import reply_auto
from ..flows.billing import billing_analyze, billing_detect, billing_month
from ..flows.summary import daily_summary
from ..flows.calendar import calendar_event
from .logging import get_logger, log_event


logger = get_logger(__name__)


def _runtime_mode_info(s: Settings) -> Dict[str, Any]:
    mode = s.effective_mode()
    info: Dict[str, Any] = {"mode": mode}
    if mode != "standalone":
        return info

    models = s.load_standalone_models()
    runner = models.get("runner", {}) if isinstance(models, dict) else {}
    agent = models.get("agent", {}) if isinstance(models, dict) else {}
    defaults = models.get("defaults", {}) if isinstance(models, dict) else {}
    if not isinstance(runner, dict):
        runner = {}
    if not isinstance(agent, dict):
        agent = {}
    if not isinstance(defaults, dict):
        defaults = {}

    cmd = str(runner.get("command") or "").strip()
    agent_id = str(agent.get("id") or "").strip()
    production_model = str(
        defaults.get("primary_model") or agent.get("model") or agent_id or ""
    ).strip()
    image_model = str(defaults.get("image_model") or "").strip()
    info["standalone"] = {
        "agent_id": agent_id,
        "production_model": production_model,
        "image_model": image_model,
        "runner_command_set": bool(cmd),
    }
    return info


def config_checklist(s: Settings) -> Dict[str, Any]:
    db = DB(s.db_path)
    db.init()
    accounts = list_accounts(db, hide_email_when_alias=True)
    google_env = bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip())
    ms_env = bool(os.environ.get("MS_OAUTH_CLIENT_ID", "").strip())
    models_path = Path(os.path.expandvars(s.effective_standalone_models_path())).expanduser()
    models = s.load_standalone_models()
    runner = models.get("runner", {}) if isinstance(models, dict) else {}
    runner_cmd_set = bool(str(runner.get("command") or "").strip()) if isinstance(runner, dict) else False
    return {
        "reviewed": s.runtime.config_reviewed,
        "confirmed": s.runtime.config_confirmed,
        "review_hint": "Run `mailhub config` first to review defaults.",
        "confirm_hint": "Then run `mailhub config --confirm` (or `mailhub mail run --confirm-config`).",
        "modify_hint": "Use `mailhub settings-set <key> <value>` or `mailhub config --wizard` to change values.",
        "settings": {
            "general": asdict(s.general),
            "mail": asdict(s.mail),
            "calendar": asdict(s.calendar),
            "summary": asdict(s.summary),
            "scheduler": asdict(s.scheduler),
            "routing": {
                "mode": s.effective_mode(),
                "openclaw_json_path": s.effective_openclaw_json_path(),
                "standalone_agent_enabled": s.effective_standalone_agent_enabled(),
                "standalone_models_path": s.effective_standalone_models_path(),
                "standalone_models_exists": models_path.exists(),
                "standalone_runner_command_set": runner_cmd_set,
                "runtime": _runtime_mode_info(s),
            },
            "oauth_defaults": {
                "google_client_id_set": bool(s.effective_google_client_id()),
                "google_client_id_source": "env" if google_env else ("settings" if s.oauth.google_client_id else ""),
                "ms_client_id_set": bool(s.effective_ms_client_id()),
                "ms_client_id_source": "env" if ms_env else ("settings" if s.oauth.ms_client_id else ""),
            },
            "security": {
                "dbkey_backend": s.effective_dbkey_backend(),
                "dbkey_keychain_account": s.effective_dbkey_keychain_account(),
                "dbkey_local_path": str(default_local_dbkey_path(s.state_dir, s.security.dbkey_local_path)),
            },
            "accounts": {"count": len(accounts)},
        },
    }


def ensure_config_confirmed(confirm_config: bool = False) -> Dict[str, Any] | None:
    s = Settings.load()
    if s.runtime.config_confirmed:
        return None
    if confirm_config:
        if not s.runtime.config_reviewed:
            return {
                "ok": False,
                "reason": "config_not_reviewed",
                "message": "Please run `mailhub config` to review settings before confirming.",
                "checklist": config_checklist(s),
            }
        s.runtime.config_confirmed = True
        s.runtime.config_confirmed_at = utc_now_iso()
        s.save()
        return {"ok": True, "config_confirmed": True, "confirmed_at": s.runtime.config_confirmed_at}
    return {
        "ok": False,
        "reason": "config_not_confirmed",
        "message": "First-run confirmation is required before execution.",
        "checklist": config_checklist(s),
    }


def mark_config_reviewed() -> Dict[str, Any]:
    s = Settings.load()
    if not s.runtime.config_reviewed:
        s.runtime.config_reviewed = True
        s.runtime.config_reviewed_at = utc_now_iso()
        s.save()
    return {
        "ok": True,
        "config_reviewed": s.runtime.config_reviewed,
        "reviewed_at": s.runtime.config_reviewed_at,
    }


def doctor_report(*, full: bool = False) -> Dict[str, Any]:
    s = Settings.load()
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    local_dbkey_path = default_local_dbkey_path(s.state_dir, s.security.dbkey_local_path)
    dbkey_checks = detect_backends(state_dir=s.state_dir, local_dbkey_path=local_dbkey_path)
    selected_backend = s.effective_dbkey_backend()
    selected_backend_check = dbkey_checks.get(selected_backend)

    checks.append(
        {
            "name": "dbkey_backend",
            "ok": bool(selected_backend_check and selected_backend_check.available),
            "backend": selected_backend,
            "details": {
                "selected": selected_backend,
                "keychain": dbkey_checks["keychain"].to_dict(),
                "systemd": dbkey_checks["systemd"].to_dict(),
                "local": dbkey_checks["local"].to_dict(),
            },
            **(
                {"error": selected_backend_check.reason}
                if selected_backend_check and not selected_backend_check.available
                else {}
            ),
        }
    )

    if not selected_backend_check:
        warnings.append(f"unknown dbkey backend: {selected_backend}")
    elif not selected_backend_check.available:
        warnings.append(f"dbkey backend unavailable: {selected_backend_check.reason}")
        if selected_backend_check.suggestion:
            warnings.append(f"dbkey hint: {selected_backend_check.suggestion}")
    if selected_backend == BACKEND_LOCAL:
        warnings.append(
            "⚠ local dbkey backend in use: if the whole state directory is leaked, offline decryption cannot be prevented"
        )
        warnings.append("recommendation: switch to keychain or systemd credential backend")
        warnings.append("recommendation: enable full-disk encryption on host")
        warnings.append("recommendation: avoid syncing/uploading full state directory to untrusted backup targets")

    db = DB(
        s.db_path,
        dbkey_backend=selected_backend,
        dbkey_local_path=local_dbkey_path,
        dbkey_keychain_account=s.effective_dbkey_keychain_account(),
    )
    try:
        _ = db._resolve_dbkey()
        checks.append({"name": "dbkey_read", "ok": True, "backend": selected_backend})
    except Exception as exc:
        checks.append({"name": "dbkey_read", "ok": False, "backend": selected_backend, "error": str(exc)})
        warnings.append(f"dbkey read failed: {exc}")

    try:
        s.ensure_dirs()
        checks.append({"name": "state_dir", "ok": True, "path": str(s.state_dir)})
    except Exception as exc:
        errors.append(f"state_dir error: {exc}")
        checks.append({"name": "state_dir", "ok": False, "error": str(exc)})

    try:
        db.init()
        checks.append({"name": "db_init", "ok": True, "path": str(s.db_path)})
    except Exception as exc:
        errors.append(f"db_init error: {exc}")
        checks.append({"name": "db_init", "ok": False, "error": str(exc)})

    providers: List[Dict[str, Any]] = []
    try:
        providers = db.list_providers()
    except Exception as exc:
        errors.append(f"providers error: {exc}")
    db_stats = _db_stats(db)

    kinds = _provider_kind_counts(providers)
    accounts: List[Dict[str, Any]] = []
    try:
        accounts = list_accounts(db, hide_email_when_alias=True)
    except Exception as exc:
        errors.append(f"accounts error: {exc}")
    if not providers:
        warnings.append("no providers bound")

    schedule_check = validate_schedule(s.scheduler.tz, s.scheduler.digest_weekdays, s.scheduler.digest_times_local)
    if not schedule_check[0]:
        warnings.append(schedule_check[1])
    bill_check = validate_billing_schedule(
        s.scheduler.tz,
        s.mail.billing.days_of_month,
        s.mail.billing.trigger_times_local,
    )
    if not bill_check[0]:
        warnings.append(bill_check[1])
    if s.calendar.reminder.enabled:
        cal_remind_check = validate_schedule(
            s.scheduler.tz,
            s.calendar.reminder.weekdays,
            s.calendar.reminder.trigger_times_local,
        )
        if not cal_remind_check[0]:
            warnings.append(f"calendar reminder schedule invalid: {cal_remind_check[1]}")
    if s.summary.enabled:
        summary_check = validate_schedule(
            s.scheduler.tz,
            s.summary.weekdays,
            s.summary.trigger_times_local,
        )
        if not summary_check[0]:
            warnings.append(f"summary schedule invalid: {summary_check[1]}")

    if not s.runtime.config_confirmed:
        warnings.append("config not confirmed yet")
    if not s.runtime.config_reviewed:
        warnings.append("config not reviewed yet")

    standalone_health = _standalone_models_health(s)
    standalone_check = {
        "name": "standalone_models_link",
        "ok": bool(standalone_health.get("ok")),
        "mode": s.effective_mode(),
        "details": standalone_health,
    }
    if not standalone_check["ok"]:
        standalone_check["error"] = str(standalone_health.get("message") or "standalone models link invalid")
    checks.append(standalone_check)
    if s.effective_mode() == "standalone" and not standalone_health.get("ok"):
        msg = str(standalone_health.get("message") or "").strip()
        if msg:
            warnings.append(f"standalone models link: {msg}")

    fs_perm_health = _fs_privacy_health(s)
    checks.append(
        {
            "name": "state_fs_privacy",
            "ok": bool(fs_perm_health.get("ok")),
            "details": fs_perm_health,
            **({"error": str(fs_perm_health.get("message") or "")} if not fs_perm_health.get("ok") else {}),
        }
    )
    if not fs_perm_health.get("ok"):
        msg = str(fs_perm_health.get("message") or "").strip()
        if msg:
            warnings.append(f"filesystem privacy: {msg}")

    token_hints = _provider_secret_hints(providers, SecretStore(s.db_path))
    secret_stats = _secret_store_stats(db)
    checks.append(
        {
            "name": "encrypted_secret_store",
            "ok": True,
            "details": secret_stats,
        }
    )

    provider_items: List[Dict[str, Any]] = []
    alias_by_id = {a["id"]: (a.get("alias") or "").strip() for a in accounts}
    for p in providers:
        alias = alias_by_id.get(p["id"], "")
        provider_items.append(
            {
                "id": p["id"],
                "kind": p["kind"],
                "alias": alias,
                "email": "" if alias else (p.get("email") or ""),
            }
        )

    report = {
        "ok": len(errors) == 0,
        "version": {"mailhub": __version__, "python": platform.python_version()},
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "providers": {
            "total": len(providers),
            "by_kind": kinds,
            "items": provider_items,
            "secret_hints": token_hints,
            "secret_store": secret_stats,
        },
        "accounts": accounts,
        "settings": {
            "config_confirmed": s.runtime.config_confirmed,
            "config_confirmed_at": s.runtime.config_confirmed_at,
            "scheduler_tz": s.scheduler.tz,
            "mode": s.effective_mode(),
            "dbkey_backend": selected_backend,
            "dbkey_local_path": str(local_dbkey_path),
            "dbkey_detection": {
                "keychain": dbkey_checks["keychain"].to_dict(),
                "systemd": dbkey_checks["systemd"].to_dict(),
                "local": dbkey_checks["local"].to_dict(),
            },
            "standalone_models_path": s.effective_standalone_models_path(),
            "standalone_models_link": standalone_health,
            "runtime": _runtime_mode_info(s),
        },
        "db_stats": db_stats,
    }
    if full:
        return report

    # Compact/default view for user-facing status checks.
    compact_checks: List[Dict[str, Any]] = []
    for c in report["checks"]:
        item = {"name": c.get("name"), "ok": c.get("ok")}
        if "error" in c:
            item["error"] = c.get("error")
        compact_checks.append(item)
    report["checks"] = compact_checks

    providers = report.get("providers", {})
    providers.pop("items", None)
    providers.pop("secret_hints", None)
    report["providers"] = providers
    report.pop("accounts", None)
    return report


def _standalone_models_health(s: Settings) -> Dict[str, Any]:
    mode = s.effective_mode()
    models_path = Path(os.path.expandvars(s.effective_standalone_models_path())).expanduser()
    openclaw_json_path = Path(os.path.expandvars(s.effective_openclaw_json_path())).expanduser()
    out: Dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "models_path": str(models_path),
        "models_exists": models_path.exists(),
        "models_json_valid": False,
        "runner_command": "",
        "runner_command_resolved": "",
        "runner_command_available": False,
        "openclaw_json_path": str(openclaw_json_path),
        "openclaw_json_exists": openclaw_json_path.exists(),
        "openclaw_json_required_by_runner": False,
        "message": "",
    }

    if mode != "standalone":
        out["message"] = "mode is not standalone; check skipped"
        return out

    if not models_path.exists():
        out["ok"] = False
        out["message"] = "standalone models file missing"
        return out

    try:
        raw = json.loads(models_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["ok"] = False
        out["message"] = f"standalone models JSON parse failed: {exc}"
        return out
    if not isinstance(raw, dict):
        out["ok"] = False
        out["message"] = "standalone models root must be JSON object"
        return out
    out["models_json_valid"] = True

    runner = raw.get("runner", {})
    runner = runner if isinstance(runner, dict) else {}
    cmd = str(runner.get("command") or "").strip()
    out["runner_command"] = cmd
    if not cmd:
        out["ok"] = False
        out["message"] = "runner.command is empty"
        return out

    try:
        argv0 = shlex.split(cmd)[0]
    except Exception:
        argv0 = cmd
    resolved = ""
    p = Path(argv0).expanduser()
    if p.is_absolute() or argv0.startswith("."):
        if p.exists():
            resolved = str(p)
    else:
        found = shutil.which(argv0)
        if found:
            resolved = found
    out["runner_command_resolved"] = resolved
    out["runner_command_available"] = bool(resolved)
    if not resolved:
        out["ok"] = False
        out["message"] = f"runner.command not found in PATH: {argv0}"
        return out

    args_raw = runner.get("args", [])
    runner_args_text = ""
    if isinstance(args_raw, list):
        runner_args_text = " ".join(str(x) for x in args_raw)
    elif isinstance(args_raw, str):
        runner_args_text = args_raw
    out["openclaw_json_required_by_runner"] = (
        "{openclaw_json_path}" in cmd or "{openclaw_json_path}" in runner_args_text
    )

    if out["openclaw_json_required_by_runner"] and not openclaw_json_path.exists():
        out["ok"] = False
        out["message"] = "openclaw json path not found"
        return out

    out["message"] = "standalone models link is healthy"
    return out


def _fs_privacy_health(s: Settings) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": True,
        "platform": os.name,
        "checks": [],
        "message": "fs privacy checks passed",
    }
    if os.name == "nt":
        out["message"] = "permission mode check skipped on Windows"
        return out

    local_dbkey_path = default_local_dbkey_path(s.state_dir, s.security.dbkey_local_path)
    targets = [
        (s.state_dir, 0o700, "state_dir"),
        (s.db_path, 0o600, "sqlite_db"),
        (s.settings_path, 0o600, "settings_json"),
        (local_dbkey_path, 0o600, "dbkey_local_file"),
    ]
    strict_failures: List[str] = []
    for p, expect, name in targets:
        exists = p.exists()
        mode = None
        ok = True
        if exists:
            try:
                mode = p.stat().st_mode & 0o777
                ok = (mode & 0o077) == 0 and (mode & 0o700) <= expect
            except Exception:
                ok = False
        item = {"name": name, "path": str(p), "exists": exists, "mode": (oct(mode) if mode is not None else "")}
        item["ok"] = ok
        out["checks"].append(item)
        if exists and not ok:
            strict_failures.append(f"{name} permissions too open")

    if strict_failures:
        out["ok"] = False
        out["message"] = "; ".join(strict_failures)
    return out


def run_jobs(since: str | None = None) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    runtime = _runtime_mode_info(s)
    log_event(
        logger,
        "jobs_run_start",
        mode=s.effective_mode(),
        since=since or "",
    )

    doctor = doctor_report()
    if not doctor["ok"]:
        log_event(
            logger,
            "jobs_run_doctor_failed",
            level="warning",
            warnings=len(doctor.get("warnings") or []),
            errors=len(doctor.get("errors") or []),
        )
        return {"ok": False, "reason": "doctor_failed", "runtime": runtime, "doctor": doctor}

    if doctor["providers"]["total"] == 0:
        log_event(logger, "jobs_run_no_provider_bound", level="warning")
        return {
            "ok": False,
            "reason": "no_provider_bound",
            "runtime": runtime,
            "message": "No mail provider account is bound yet (this is not LLM/subagent binding). Run `mailhub bind` first.",
            "suggest_bind": True,
        }

    effective_since = (since or s.mail.poll_since).strip()
    interval_seconds = _interval_seconds_from_since(effective_since)
    out: Dict[str, Any] = {
        "ok": True,
        "since": effective_since,
        "runtime": runtime,
        "steps": {},
        "schedule": {},
    }
    out["schedule"]["jobs_interval_seconds"] = interval_seconds

    out["steps"]["poll"] = inbox_poll(since=effective_since, mode="jobs")
    poll_items = out["steps"]["poll"].get("items") or []
    log_event(
        logger,
        "jobs_poll_finished",
        provider_runs=len(poll_items),
        provider_counts=[
            {
                "provider_id": str(i.get("provider_id") or ""),
                "count": int(i.get("count") or 0),
            }
            for i in poll_items
        ],
    )
    out["steps"]["triage_today"] = triage_day("today")
    triage_items = (out["steps"]["triage_today"] or {}).get("analyzed_items") or []
    log_event(logger, "jobs_triage_finished", analyzed_count=len(triage_items))

    if s.mail.alerts_mode in ("all", "suggested"):
        out["steps"]["alerts"] = triage_suggest(since=effective_since)
        alerts_items = (out["steps"]["alerts"] or {}).get("items") or []
        log_event(logger, "jobs_alerts_finished", alerts_count=len(alerts_items))

    # Always produce a DB-based daily summary for status visibility.
    out["steps"]["daily_summary"] = daily_summary("today")
    day_summary = (out["steps"]["daily_summary"] or {}).get("stats") or {}
    log_event(
        logger,
        "jobs_daily_summary_finished",
        total=int(day_summary.get("total", 0)),
    )

    if s.mail.auto_reply == "on":
        out["steps"]["auto_reply"] = reply_auto(
            since=effective_since,
            dry_run=(s.mail.auto_reply_send != "on"),
        )
        auto_reply = out["steps"]["auto_reply"] or {}
        log_event(
            logger,
            "jobs_auto_reply_finished",
            dry_run=bool(auto_reply.get("dry_run", True)),
            drafted_count=len(auto_reply.get("drafted") or []),
            sent_count=len(auto_reply.get("sent") or []),
        )

    now_local, due_digest, due_billing = _due_schedule_slots(s)
    out["schedule"]["now_local"] = now_local.isoformat()
    out["schedule"]["digest_due_slots"] = due_digest
    out["schedule"]["billing_due_slots"] = due_billing

    if due_digest:
        out["steps"]["digest"] = triage_day("today")
        log_event(logger, "jobs_digest_triggered", slots=due_digest)
        for slot in due_digest:
            db.kv_set(f"jobs.digest.{slot}", now_local.isoformat(), utc_now_iso())

    if due_billing and s.mail.billing.analysis_mode == "on":
        det = billing_detect(since="45d")
        analyzed = []
        for item in det.get("detected", []):
            sid = item.get("statement_id")
            if not sid:
                continue
            try:
                analyzed.append(billing_analyze(sid))
            except Exception as exc:
                analyzed.append({"statement_id": sid, "error": str(exc)})
        month = now_local.strftime("%Y-%m")
        out["steps"]["billing"] = {
            "detect": det,
            "analyzed": analyzed,
            "month": billing_month(month),
        }
        log_event(
            logger,
            "jobs_billing_triggered",
            slots=due_billing,
            detected_count=len(det.get("detected") or []),
            analyzed_count=len(analyzed),
        )
        for slot in due_billing:
            db.kv_set(f"jobs.billing.{slot}", now_local.isoformat(), utc_now_iso())

    reminder_due_slots = _due_interval_slots(
        db=db,
        key_prefix="jobs.calendar_reminder",
        now_local=now_local,
        weekdays_csv=s.calendar.reminder.weekdays,
        times_csv=s.calendar.reminder.trigger_times_local,
        interval_seconds=interval_seconds,
    )
    summary_due_slots = _due_interval_slots(
        db=db,
        key_prefix="jobs.summary",
        now_local=now_local,
        weekdays_csv=s.summary.weekdays,
        times_csv=s.summary.trigger_times_local,
        interval_seconds=interval_seconds,
    )
    out["schedule"]["calendar_reminder_due_slots"] = reminder_due_slots
    out["schedule"]["summary_due_slots"] = summary_due_slots

    if s.calendar.reminder.enabled:
        if s.calendar.reminder.in_jobs_run:
            if reminder_due_slots:
                out["steps"]["calendar_reminder"] = calendar_event(
                    event="remind",
                    datetime_range_raw=s.calendar.reminder.range,
                )
                reminder = out["steps"]["calendar_reminder"] or {}
                log_event(
                    logger,
                    "jobs_calendar_reminder_triggered",
                    slots=reminder_due_slots,
                    event_count=int(reminder.get("count", 0)),
                )
                for slot in reminder_due_slots:
                    db.kv_set(f"jobs.calendar_reminder.{slot}", now_local.isoformat(), utc_now_iso())
        else:
            out["schedule"]["calendar_reminder_external_cron_hint"] = (
                "Calendar reminder is enabled but excluded from mail run flow. "
                f"Use external scheduler to run: mailhub calendar --event remind --datetime-range \"{s.calendar.reminder.range}\""
            )

    if s.summary.enabled:
        if s.summary.in_jobs_run:
            if summary_due_slots:
                out["steps"]["scheduled_summary"] = {
                    "mail_daily": daily_summary("today"),
                    "calendar": calendar_event(
                        event="summary",
                        datetime_range_raw=s.summary.range,
                    ),
                }
                sched = out["steps"]["scheduled_summary"] or {}
                cal = sched.get("calendar") or {}
                log_event(
                    logger,
                    "jobs_scheduled_summary_triggered",
                    slots=summary_due_slots,
                    calendar_event_count=int(cal.get("count", 0) if isinstance(cal, dict) else 0),
                )
                for slot in summary_due_slots:
                    db.kv_set(f"jobs.summary.{slot}", now_local.isoformat(), utc_now_iso())
        else:
            out["schedule"]["summary_external_cron_hint"] = (
                "Summary is enabled but excluded from mail run flow. "
                "Use external scheduler to run both: "
                "mailhub summary --mail AND "
                f"mailhub calendar --event summary --datetime-range \"{s.summary.range}\""
            )

    # Persist latest snapshots for openclaw/standalone bridge retrieval.
    try:
        cache_latest_result(
            "mail",
            {
                "since": effective_since,
                "poll": out["steps"].get("poll"),
                "triage_today": out["steps"].get("triage_today"),
                "daily_summary": out["steps"].get("daily_summary"),
                "alerts": out["steps"].get("alerts"),
                "auto_reply": out["steps"].get("auto_reply"),
            },
        )
        if "calendar_reminder" in out["steps"]:
            cache_latest_result("calendar", out["steps"]["calendar_reminder"])
        if "scheduled_summary" in out["steps"]:
            cache_latest_result("summary", out["steps"]["scheduled_summary"])
        else:
            cache_latest_result(
                "summary",
                {
                    "mail_daily": out["steps"].get("daily_summary"),
                    "calendar": None,
                },
            )
    except Exception:
        pass

    log_event(
        logger,
        "jobs_run_done",
        ok=bool(out.get("ok", False)),
        steps=list((out.get("steps") or {}).keys()),
    )
    return out


def cache_latest_result(section: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    key = f"openclaw.results.{(section or '').strip().lower()}"
    wrapped = {
        "section": (section or "").strip().lower(),
        "updated_at": utc_now_iso(),
        "payload": payload,
    }
    db.kv_set(key, json.dumps(wrapped, ensure_ascii=False), utc_now_iso())
    return wrapped


def get_cached_result(section: str) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()
    sec = (section or "").strip().lower()
    key = f"openclaw.results.{sec}"
    raw = db.kv_get(key)
    if not raw:
        return {"ok": False, "section": sec, "reason": "no_cached_result"}
    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": False, "section": sec, "reason": "invalid_cached_result"}
    return {"ok": True, "section": sec, "cached": data}


def should_offer_bind_interactive(result: Dict[str, Any]) -> bool:
    return bool(result.get("suggest_bind")) and sys.stdin.isatty()


def validate_schedule(tz_name: str, weekdays_csv: str, times_csv: str) -> Tuple[bool, str]:
    try:
        ZoneInfo(tz_name)
    except Exception:
        return False, f"invalid scheduler_tz={tz_name}"
    weekdays = _parse_weekdays(weekdays_csv)
    if not weekdays:
        return False, "digest_weekdays is empty or invalid"
    times = _parse_times(times_csv)
    if not times:
        return False, "digest_times_local is empty or invalid"
    return True, "ok"


def validate_billing_schedule(tz_name: str, days_csv: str, times_csv: str) -> Tuple[bool, str]:
    try:
        ZoneInfo(tz_name)
    except Exception:
        return False, f"invalid scheduler_tz={tz_name}"
    days = _parse_days_of_month(days_csv)
    if not days:
        return False, "billing_days_of_month is empty or invalid"
    times = _parse_times(times_csv)
    if not times:
        return False, "billing_times_local is empty or invalid"
    return True, "ok"


def _provider_kind_counts(providers: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in providers:
        out[p["kind"]] = out.get(p["kind"], 0) + 1
    return out


def _db_stats(db: DB) -> Dict[str, int]:
    con = db.connect()
    try:
        tables = ["providers", "messages", "message_tags", "reply_queue", "billing_statements", "attachments", "kv"]
        out: Dict[str, int] = {}
        for t in tables:
            row = con.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
            out[t] = int(row["c"]) if row else 0
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _provider_secret_hints(providers: List[Dict[str, Any]], store: SecretStore) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    for p in providers:
        pid = p["id"]
        kind = p["kind"]
        if kind == "imap":
            has_secret = _check_secret_exists(store, f"{pid}:password")
            hints.append({"provider_id": pid, "kind": kind, "credential_ready": has_secret})
        elif kind in ("google", "microsoft"):
            has_token = _check_secret_exists(store, f"{pid}:access_token")
            hints.append({"provider_id": pid, "kind": kind, "credential_ready": has_token})
        else:
            hints.append({"provider_id": pid, "kind": kind, "credential_ready": None})
    return hints


def _secret_store_stats(db: DB) -> Dict[str, Any]:
    con = db.connect()
    try:
        rows = con.execute("SELECT k FROM kv WHERE k LIKE 'secret:%'").fetchall()
        total = len(rows)
        by_type: Dict[str, int] = {}
        for row in rows:
            k = str(row["k"] or "")
            suffix = k.rsplit(":", 1)[-1] if ":" in k else "unknown"
            by_type[suffix] = by_type.get(suffix, 0) + 1
        return {"entries": total, "by_type": by_type}
    except Exception:
        return {"entries": 0, "by_type": {}, "error": "unavailable"}
    finally:
        con.close()


def _check_secret_exists(store: SecretStore, key: str) -> bool | None:
    try:
        return bool(store.get(key))
    except Exception:
        return None


def _due_schedule_slots(s: Settings) -> Tuple[datetime, List[str], List[str]]:
    tz = ZoneInfo(s.scheduler.tz)
    now_local = datetime.now(tz)
    db = DB(s.db_path)

    digest_slots: List[str] = []
    bill_slots: List[str] = []

    digest_weekdays = _parse_weekdays(s.scheduler.digest_weekdays)
    digest_times = _parse_times(s.scheduler.digest_times_local)
    if now_local.weekday() in digest_weekdays:
        for hh, mm in digest_times:
            slot_id = f"{now_local.strftime('%Y-%m-%d')}.{hh:02d}:{mm:02d}"
            if _slot_due(db, f"jobs.digest.{slot_id}", now_local, hh, mm):
                digest_slots.append(slot_id)

    bill_days = _parse_days_of_month(s.mail.billing.days_of_month)
    bill_times = _parse_times(s.mail.billing.trigger_times_local)
    if now_local.day in bill_days:
        for hh, mm in bill_times:
            slot_id = f"{now_local.strftime('%Y-%m-%d')}.{hh:02d}:{mm:02d}"
            if _slot_due(db, f"jobs.billing.{slot_id}", now_local, hh, mm):
                bill_slots.append(slot_id)

    return now_local, digest_slots, bill_slots


def _due_interval_slots(
    *,
    db: DB,
    key_prefix: str,
    now_local: datetime,
    weekdays_csv: str,
    times_csv: str,
    interval_seconds: int,
) -> List[str]:
    weekdays = _parse_weekdays(weekdays_csv)
    times = _parse_times(times_csv)
    if not weekdays or not times:
        return []
    if now_local.weekday() not in weekdays:
        return []

    window = max(60, int(interval_seconds))
    due: List[str] = []
    for hh, mm in times:
        slot_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = abs((now_local - slot_dt).total_seconds())
        slot_id = f"{now_local.strftime('%Y-%m-%d')}.{hh:02d}:{mm:02d}"
        if delta > window:
            continue
        if db.kv_get(f"{key_prefix}.{slot_id}"):
            continue
        due.append(slot_id)
    return due


def _interval_seconds_from_since(since: str) -> int:
    try:
        now = datetime.now(timezone.utc)
        dt = parse_since(since)
        sec = int(abs((now - dt).total_seconds()))
        return max(60, sec)
    except Exception:
        return 900


def _slot_due(db: DB, key: str, now_local: datetime, hh: int, mm: int) -> bool:
    if db.kv_get(key):
        return False
    return (now_local.hour, now_local.minute) >= (hh, mm)


def _parse_weekdays(s: str) -> set[int]:
    mapping = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    out: set[int] = set()
    for part in s.split(","):
        k = part.strip().lower()
        if k in mapping:
            out.add(mapping[k])
    return out


def _parse_times(s: str) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for raw in s.split(","):
        p = raw.strip()
        if not p or ":" not in p:
            continue
        hh_s, mm_s = p.split(":", 1)
        try:
            hh = int(hh_s)
            mm = int(mm_s)
        except ValueError:
            continue
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            out.append((hh, mm))
    return sorted(set(out))


def _parse_days_of_month(s: str) -> set[int]:
    out: set[int] = set()
    for raw in s.split(","):
        p = raw.strip()
        if not p:
            continue
        try:
            day = int(p)
        except ValueError:
            continue
        if 1 <= day <= 31:
            out.add(day)
    return out
