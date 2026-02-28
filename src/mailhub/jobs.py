from __future__ import annotations

import os
import platform
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from . import __version__
from .accounts import list_accounts
from .config import Settings
from .security import SecretStore
from .store import DB
from .utils.time import utc_now_iso
from .pipelines.ingest import inbox_poll
from .pipelines.triage import triage_day, triage_suggest
from .pipelines.reply import reply_auto
from .pipelines.billing import billing_analyze, billing_detect, billing_month
from .pipelines.summary import daily_summary


def config_checklist(s: Settings) -> Dict[str, Any]:
    db = DB(s.db_path)
    db.init()
    accounts = list_accounts(db, hide_email_when_alias=True)
    google_env = bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip())
    ms_env = bool(os.environ.get("MS_OAUTH_CLIENT_ID", "").strip())
    return {
        "reviewed": s.runtime.config_reviewed,
        "confirmed": s.runtime.config_confirmed,
        "review_hint": "Run `mailhub config` first to review defaults.",
        "confirm_hint": "Then run `mailhub config --confirm` (or `mailhub jobs run --confirm-config`).",
        "modify_hint": "Use `mailhub settings-set <key> <value>` or `mailhub config --wizard` to change values.",
        "settings": {
            "toggles": asdict(s.toggles),
            "oauth_defaults": {
                "google_client_id_set": bool(s.effective_google_client_id()),
                "google_client_id_source": "env" if google_env else ("settings" if s.oauth.google_client_id else ""),
                "ms_client_id_set": bool(s.effective_ms_client_id()),
                "ms_client_id_source": "env" if ms_env else ("settings" if s.oauth.ms_client_id else ""),
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
    db = DB(s.db_path)

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
    accounts = list_accounts(db, hide_email_when_alias=True)
    if not providers:
        warnings.append("no providers bound")

    schedule_check = validate_schedule(s.toggles.scheduler_tz, s.toggles.digest_weekdays, s.toggles.digest_times_local)
    if not schedule_check[0]:
        warnings.append(schedule_check[1])
    bill_check = validate_billing_schedule(s.toggles.scheduler_tz, s.toggles.billing_days_of_month, s.toggles.billing_times_local)
    if not bill_check[0]:
        warnings.append(bill_check[1])

    if not s.runtime.config_confirmed:
        warnings.append("config not confirmed yet")
    if not s.runtime.config_reviewed:
        warnings.append("config not reviewed yet")

    token_hints = _provider_secret_hints(providers, SecretStore(s.secrets_path))

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
        },
        "accounts": accounts,
        "settings": {
            "config_confirmed": s.runtime.config_confirmed,
            "config_confirmed_at": s.runtime.config_confirmed_at,
            "scheduler_tz": s.toggles.scheduler_tz,
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


def run_jobs(since: str | None = None) -> Dict[str, Any]:
    s = Settings.load()
    db = DB(s.db_path)
    db.init()

    doctor = doctor_report()
    if not doctor["ok"]:
        return {"ok": False, "reason": "doctor_failed", "doctor": doctor}

    if doctor["providers"]["total"] == 0:
        return {
            "ok": False,
            "reason": "no_provider_bound",
            "message": "No mail provider account is bound yet (this is not LLM/subagent binding). Run `mailhub bind` first.",
            "suggest_bind": True,
        }

    effective_since = (since or s.toggles.poll_since).strip()
    out: Dict[str, Any] = {
        "ok": True,
        "since": effective_since,
        "steps": {},
        "schedule": {},
    }

    out["steps"]["poll"] = inbox_poll(since=effective_since, mode="jobs")

    if s.toggles.mail_alerts_mode in ("all", "suggested"):
        out["steps"]["alerts"] = triage_suggest(since=effective_since)

    # Always produce a DB-based daily summary for status visibility.
    out["steps"]["daily_summary"] = daily_summary("today")

    if s.toggles.auto_reply == "on":
        out["steps"]["auto_reply"] = reply_auto(
            since=effective_since,
            dry_run=(s.toggles.auto_reply_send != "on"),
        )

    now_local, due_digest, due_billing = _due_schedule_slots(s)
    out["schedule"]["now_local"] = now_local.isoformat()
    out["schedule"]["digest_due_slots"] = due_digest
    out["schedule"]["billing_due_slots"] = due_billing

    if due_digest:
        out["steps"]["digest"] = triage_day("today")
        for slot in due_digest:
            db.kv_set(f"jobs.digest.{slot}", now_local.isoformat(), utc_now_iso())

    if due_billing and s.toggles.bill_analysis == "on":
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
        for slot in due_billing:
            db.kv_set(f"jobs.billing.{slot}", now_local.isoformat(), utc_now_iso())

    return out


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


def _check_secret_exists(store: SecretStore, key: str) -> bool | None:
    # Best effort only: encrypted-file fallback may require passphrase prompt.
    try:
        if os.environ.get("MAILHUB_SECRET_PASSPHRASE"):
            return bool(store.get(key))
        return None
    except Exception:
        return None


def _due_schedule_slots(s: Settings) -> Tuple[datetime, List[str], List[str]]:
    tz = ZoneInfo(s.toggles.scheduler_tz)
    now_local = datetime.now(tz)
    db = DB(s.db_path)

    digest_slots: List[str] = []
    bill_slots: List[str] = []

    digest_weekdays = _parse_weekdays(s.toggles.digest_weekdays)
    digest_times = _parse_times(s.toggles.digest_times_local)
    if now_local.weekday() in digest_weekdays:
        for hh, mm in digest_times:
            slot_id = f"{now_local.strftime('%Y-%m-%d')}.{hh:02d}:{mm:02d}"
            if _slot_due(db, f"jobs.digest.{slot_id}", now_local, hh, mm):
                digest_slots.append(slot_id)

    bill_days = _parse_days_of_month(s.toggles.billing_days_of_month)
    bill_times = _parse_times(s.toggles.billing_times_local)
    if now_local.day in bill_days:
        for hh, mm in bill_times:
            slot_id = f"{now_local.strftime('%Y-%m-%d')}.{hh:02d}:{mm:02d}"
            if _slot_due(db, f"jobs.billing.{slot_id}", now_local, hh, mm):
                bill_slots.append(slot_id)

    return now_local, digest_slots, bill_slots


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
