from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Any, Dict, Iterable

DEFAULT_DISCLOSURE = "<This reply is auto-genertated by Mailhub skill>"


@dataclass
class GeneralConfig:
    agent_display_name: str = "MailHub"
    disclosure_line: str = DEFAULT_DISCLOSURE


@dataclass
class MailBillingConfig:
    analysis_mode: str = "off"  # off|on
    days_of_month: str = "1"
    trigger_times_local: str = "10:00"


@dataclass
class MailFetchConfig:
    default_cold_start_days: int = 30
    max_results_per_page: int = 50
    min_results_per_page: int = 10
    max_pages_per_run: int = 5
    backoff_retries: int = 4
    backoff_initial_seconds: int = 1
    backoff_max_seconds: int = 16


@dataclass
class MailConfig:
    alerts_mode: str = "off"  # off|all|suggested
    scheduled_analysis: str = "off"  # off|daily|weekly
    scheduled_time_local: str = "09:00"  # HH:MM
    auto_reply: str = "off"  # off|on
    auto_reply_send: str = "off"  # off|on
    poll_since: str = "15m"
    suggest_max_items: int = 10
    reply_needed_max_items: int = 20
    fetch: MailFetchConfig = field(default_factory=MailFetchConfig)
    billing: MailBillingConfig = field(default_factory=MailBillingConfig)


@dataclass
class CalendarReminderConfig:
    enabled: bool = False
    in_jobs_run: bool = True
    range: str = "tomorrow"
    weekdays: str = "mon,tue,wed,thu,fri,sat,sun"
    trigger_times_local: str = "09:00"


@dataclass
class CalendarConfig:
    management_mode: str = "off"  # off|on
    days_window: int = 3
    reminder: CalendarReminderConfig = field(default_factory=CalendarReminderConfig)


@dataclass
class SummaryConfig:
    enabled: bool = True
    in_jobs_run: bool = True
    range: str = "this_week_remaining"
    weekdays: str = "mon,tue,wed,thu,fri"
    trigger_times_local: str = "18:00"


@dataclass
class SchedulerConfig:
    tz: str = "UTC"
    digest_weekdays: str = "mon,tue,wed,thu,fri"
    digest_times_local: str = "09:00"
    standalone_loop_interval_seconds: int = 60


@dataclass
class RuntimeFlags:
    config_reviewed: bool = False
    config_reviewed_at: str = ""
    config_confirmed: bool = False
    config_confirmed_at: str = ""


@dataclass
class RoutingConfig:
    # openclaw: rely on OpenClaw SKILL orchestration for reasoning
    # standalone: reasoning via local agent bridge command + prompts
    mode: str = "openclaw"  # openclaw|standalone
    openclaw_json_path: str = "~/.openclaw/openclaw.json"
    standalone_agent_enabled: bool = True
    standalone_models_path: str = ""


@dataclass
class OAuthClientConfig:
    google_client_id: str = ""
    google_client_secret: str = ""
    ms_client_id: str = ""


@dataclass
class SecurityConfig:
    dbkey_backend: str = "local"  # keychain|systemd|local
    dbkey_keychain_account: str = "default"
    dbkey_local_path: str = "dbkey.enc"  # relative to state_dir by default


@dataclass
class Settings:
    state_dir: Path
    db_path: Path
    settings_path: Path

    general: GeneralConfig
    mail: MailConfig
    calendar: CalendarConfig
    summary: SummaryConfig
    scheduler: SchedulerConfig
    oauth: OAuthClientConfig
    security: SecurityConfig
    runtime: RuntimeFlags
    routing: RoutingConfig

    @staticmethod
    def default_state_dir() -> Path:
        p = os.environ.get("MAILHUB_STATE_DIR")
        if p:
            return Path(os.path.expandvars(p)).expanduser()
        # fallback
        return Path.home() / ".openclaw" / "state" / "mailhub"

    @classmethod
    def load(cls) -> "Settings":
        state_dir = cls.default_state_dir()
        settings_path = state_dir / "settings.json"
        db_path = state_dir / "mailhub.sqlite"

        general = GeneralConfig()
        mail = MailConfig()
        calendar = CalendarConfig()
        summary = SummaryConfig()
        scheduler = SchedulerConfig()
        oauth = OAuthClientConfig()
        security = SecurityConfig()
        runtime = RuntimeFlags()
        routing = RoutingConfig()
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            g = data.get("general", {})
            if isinstance(g, dict):
                general = GeneralConfig(
                    **{**asdict(general), **_filter_dataclass_kwargs(GeneralConfig, g)}
                )
            m = data.get("mail", {})
            if isinstance(m, dict):
                mf = mail.fetch
                mb = mail.billing
                f = m.get("fetch", {})
                b = m.get("billing", {})
                if isinstance(f, dict):
                    mf = MailFetchConfig(
                        **{**asdict(mf), **_filter_dataclass_kwargs(MailFetchConfig, f)}
                    )
                if isinstance(b, dict):
                    mb = MailBillingConfig(
                        **{**asdict(mb), **_filter_dataclass_kwargs(MailBillingConfig, b)}
                    )
                mail = MailConfig(
                    **{
                        **asdict(mail),
                        **_filter_dataclass_kwargs(MailConfig, m, exclude=("fetch", "billing")),
                    },
                    fetch=mf,
                    billing=mb,
                )
            c = data.get("calendar", {})
            if isinstance(c, dict):
                cr = calendar.reminder
                r = c.get("reminder", {})
                if isinstance(r, dict):
                    cr = CalendarReminderConfig(
                        **{**asdict(cr), **_filter_dataclass_kwargs(CalendarReminderConfig, r)}
                    )
                calendar = CalendarConfig(
                    **{
                        **asdict(calendar),
                        **_filter_dataclass_kwargs(CalendarConfig, c, exclude=("reminder",)),
                    },
                    reminder=cr,
                )
            sm = data.get("summary", {})
            if isinstance(sm, dict):
                summary = SummaryConfig(
                    **{**asdict(summary), **_filter_dataclass_kwargs(SummaryConfig, sm)}
                )
            sc = data.get("scheduler", {})
            if isinstance(sc, dict):
                scheduler = SchedulerConfig(
                    **{**asdict(scheduler), **_filter_dataclass_kwargs(SchedulerConfig, sc)}
                )
            o = data.get("oauth", {})
            oauth = OAuthClientConfig(
                **{**asdict(oauth), **_filter_dataclass_kwargs(OAuthClientConfig, o)}
            )
            sec = data.get("security", {})
            security = SecurityConfig(
                **{**asdict(security), **_filter_dataclass_kwargs(SecurityConfig, sec)}
            )
            r = data.get("runtime", {})
            runtime = RuntimeFlags(
                **{**asdict(runtime), **_filter_dataclass_kwargs(RuntimeFlags, r)}
            )
            rt = data.get("routing", {})
            routing = RoutingConfig(
                **{**asdict(routing), **_filter_dataclass_kwargs(RoutingConfig, rt)}
            )

        return cls(
            state_dir=state_dir,
            db_path=db_path,
            settings_path=settings_path,
            general=general,
            mail=mail,
            calendar=calendar,
            summary=summary,
            scheduler=scheduler,
            oauth=oauth,
            security=security,
            runtime=runtime,
            routing=routing,
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _restrict_private_path(self.state_dir, is_dir=True)
        self._ensure_standalone_models_files()

    def save(self) -> None:
        self.ensure_dirs()
        payload: Dict[str, Any] = {
            "general": asdict(self.general),
            "mail": asdict(self.mail),
            "calendar": asdict(self.calendar),
            "summary": asdict(self.summary),
            "scheduler": asdict(self.scheduler),
            "oauth": asdict(self.oauth),
            "security": asdict(self.security),
            "runtime": asdict(self.runtime),
            "routing": asdict(self.routing),
        }
        self.settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _restrict_private_path(self.settings_path, is_dir=False)

    def disclosure_text(self) -> str:
        return self.general.disclosure_line.replace(
            "<AgentName>", self.general.agent_display_name
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state_dir": str(self.state_dir),
            "db_path": str(self.db_path),
            "settings_path": str(self.settings_path),
            "general": asdict(self.general),
            "mail": asdict(self.mail),
            "calendar": asdict(self.calendar),
            "summary": asdict(self.summary),
            "scheduler": asdict(self.scheduler),
            "oauth": asdict(self.oauth),
            "security": asdict(self.security),
            "runtime": asdict(self.runtime),
            "routing": asdict(self.routing),
        }

    def effective_dbkey_backend(self) -> str:
        v = (self.security.dbkey_backend or "local").strip().lower()
        if v in ("keychain", "systemd", "local"):
            return v
        return "local"

    def effective_dbkey_keychain_account(self) -> str:
        v = (self.security.dbkey_keychain_account or "default").strip()
        return v or "default"

    def effective_dbkey_local_path(self) -> Path:
        raw = (self.security.dbkey_local_path or "dbkey.enc").strip()
        p = Path(os.path.expandvars(raw)).expanduser()
        if p.is_absolute():
            return p
        return self.state_dir / p

    def resolve_setting_key(self, key: str) -> str:
        return resolve_setting_key(key)

    def get_setting_value(self, key: str) -> Any:
        return _get_path_value(self, self.resolve_setting_key(key))

    def set_setting_value(self, key: str, value: Any) -> str:
        path = self.resolve_setting_key(key)
        cur = _get_path_value(self, path)
        if isinstance(cur, bool):
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on"):
                    value = True
                elif v in ("0", "false", "no", "off"):
                    value = False
                else:
                    raise ValueError(f"Invalid boolean value: {value}")
            else:
                value = bool(value)
        elif isinstance(cur, int):
            try:
                value = int(value)
            except Exception as exc:
                raise ValueError(f"Invalid integer value: {value}") from exc
        else:
            value = str(value)
        _set_path_value(self, path, value)
        return path

    def effective_mode(self) -> str:
        v = (
            (os.environ.get("MAILHUB_MODE") or self.routing.mode or "openclaw")
            .strip()
            .lower()
        )
        if v not in ("openclaw", "standalone"):
            return "openclaw"
        return v

    def effective_openclaw_json_path(self) -> str:
        return (
            os.environ.get("MAILHUB_OPENCLAW_JSON_PATH")
            or self.routing.openclaw_json_path
            or "~/.openclaw/openclaw.json"
        ).strip()

    def effective_standalone_agent_enabled(self) -> bool:
        raw = os.environ.get("MAILHUB_STANDALONE_AGENT_ENABLED", "")
        if raw.strip():
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(self.routing.standalone_agent_enabled)

    def effective_standalone_models_path(self) -> str:
        configured = (self.routing.standalone_models_path or "").strip()
        default_path = str(self.state_dir / "standalone.models.json")
        return (
            os.environ.get("MAILHUB_STANDALONE_MODELS_PATH")
            or configured
            or default_path
        ).strip()

    def effective_standalone_models_template_path(self) -> str:
        # Template is intentionally fixed under skill template/ for discoverability/editability.
        skill_dir = (os.environ.get("MAILHUB_SKILL_DIR") or "").strip()
        if skill_dir:
            return str(
                Path(os.path.expandvars(skill_dir)).expanduser()
                / "template"
                / "standalone.models.template.json"
            )
        return str(
            Path(__file__).resolve().parents[3] / "template" / "standalone.models.template.json"
        )

    def effective_settings_template_path(self) -> str:
        skill_dir = (os.environ.get("MAILHUB_SKILL_DIR") or "").strip()
        if skill_dir:
            return str(
                Path(os.path.expandvars(skill_dir)).expanduser()
                / "template"
                / "settings.template.json"
            )
        return str(
            Path(__file__).resolve().parents[3] / "template" / "settings.template.json"
        )

    def load_standalone_models(self) -> Dict[str, Any]:
        p = Path(
            os.path.expandvars(self.effective_standalone_models_path())
        ).expanduser()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _ensure_standalone_models_files(self) -> None:
        p = Path(
            os.path.expandvars(self.effective_standalone_models_path())
        ).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        _restrict_private_path(p.parent, is_dir=True)
        if not p.exists():
            p.write_text("{}", encoding="utf-8")
        _restrict_private_path(p, is_dir=False)

    def _dotenv_value(self, key: str) -> str:
        # Priority: explicit env file -> cwd .env -> skill dir .env (if launcher exports) -> ""
        candidates: list[Path] = []
        env_file = (os.environ.get("MAILHUB_ENV_FILE") or "").strip()
        if env_file:
            candidates.append(Path(os.path.expandvars(env_file)).expanduser())
        candidates.append(Path.cwd() / ".env")
        skill_dir = (os.environ.get("MAILHUB_SKILL_DIR") or "").strip()
        if skill_dir:
            candidates.append(Path(os.path.expandvars(skill_dir)).expanduser() / ".env")

        for p in candidates:
            if not p.exists() or not p.is_file():
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    k, v = raw.split("=", 1)
                    if k.strip() != key:
                        continue
                    val = v.strip()
                    if len(val) >= 2 and (
                        (val[0] == '"' and val[-1] == '"')
                        or (val[0] == "'" and val[-1] == "'")
                    ):
                        val = val[1:-1]
                    return val.strip()
            except Exception:
                continue
        return ""

    def effective_google_client_id(self) -> str:
        return (
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
            or self._dotenv_value("GOOGLE_OAUTH_CLIENT_ID")
            or self.oauth.google_client_id
            or ""
        ).strip()

    def effective_google_client_secret(self) -> str:
        return (
            os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
            or self._dotenv_value("GOOGLE_OAUTH_CLIENT_SECRET")
            or self.oauth.google_client_secret
            or ""
        ).strip()

    def effective_ms_client_id(self) -> str:
        return (
            os.environ.get("MS_OAUTH_CLIENT_ID")
            or self._dotenv_value("MS_OAUTH_CLIENT_ID")
            or self.oauth.ms_client_id
            or ""
        ).strip()

    def skill_root(self) -> Path:
        """
        Resolve MailHub skill root directory.
        Priority:
        1) MAILHUB_SKILL_DIR (set by launcher/setup)
        2) Source-relative fallback
        """
        skill_dir = (os.environ.get("MAILHUB_SKILL_DIR") or "").strip()
        if skill_dir:
            return Path(os.path.expandvars(skill_dir)).expanduser()
        return Path(__file__).resolve().parents[3]

    def resolve_skill_path(self, relative_path: str) -> Path:
        return self.skill_root() / relative_path


def _filter_dataclass_kwargs(
    dc: type[Any], data: Dict[str, Any], *, exclude: Iterable[str] = ()
) -> Dict[str, Any]:
    allowed = {f.name for f in fields(dc)}
    excluded = set(exclude)
    return {k: v for k, v in data.items() if k in allowed and k not in excluded}


STRUCTURED_SETTINGS_ROOTS = {
    "general",
    "mail",
    "calendar",
    "summary",
    "scheduler",
    "oauth",
    "security",
    "runtime",
    "routing",
}


def resolve_setting_key(key: str) -> str:
    raw = (key or "").strip()
    if not raw:
        raise ValueError("settings key is empty")

    if "." not in raw:
        raise ValueError(
            f"Invalid key: {raw}. Use dotted key like mail.poll_since or calendar.reminder.enabled."
        )

    ns, _ = raw.split(".", 1)
    if ns in STRUCTURED_SETTINGS_ROOTS:
        return raw
    raise ValueError(f"Unknown settings namespace: {ns}")


def _get_path_value(root_obj: Any, dotted: str) -> Any:
    obj = root_obj
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _set_path_value(root_obj: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    obj = root_obj
    for part in parts[:-1]:
        if isinstance(obj, dict):
            obj = obj[part]
        else:
            obj = getattr(obj, part)
    last = parts[-1]
    if isinstance(obj, dict):
        obj[last] = value
    else:
        setattr(obj, last, value)


def _restrict_private_path(path: Path, *, is_dir: bool) -> None:
    if os.name == "nt":
        return
    try:
        if not path.exists():
            return
        os.chmod(path, 0o700 if is_dir else 0o600)
    except Exception:
        pass
