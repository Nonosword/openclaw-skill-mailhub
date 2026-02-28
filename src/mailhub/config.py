from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Any, Dict

DEFAULT_DISCLOSURE = "— Sent by <AgentName> via MailHub"


@dataclass
class FeatureToggles:
    agent_display_name: str = "MailHub"
    disclosure_line: str = DEFAULT_DISCLOSURE

    mail_alerts_mode: str = "off"  # off|all|suggested
    scheduled_analysis: str = "off"  # off|daily|weekly
    scheduled_time_local: str = "09:00"  # HH:MM
    auto_reply: str = "off"  # off|on
    calendar_management: str = "off"  # off|on
    calendar_days_window: int = 3
    bill_analysis: str = "off"  # off|on

    # Additional knobs
    suggest_max_items: int = 10
    reply_needed_max_items: int = 20
    poll_since: str = "15m"
    auto_reply_send: str = "off"  # off|on

    # Scheduler knobs used by "mailhub jobs run" when triggered periodically.
    scheduler_tz: str = "UTC"
    digest_weekdays: str = "mon,tue,wed,thu,fri"
    digest_times_local: str = "09:00"
    billing_days_of_month: str = "1"
    billing_times_local: str = "10:00"


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
    standalone_agent_cmd: str = ""


@dataclass
class OAuthClientConfig:
    google_client_id: str = ""
    google_client_secret: str = ""
    ms_client_id: str = ""


@dataclass
class Settings:
    state_dir: Path
    db_path: Path
    settings_path: Path
    secrets_path: Path

    toggles: FeatureToggles
    oauth: OAuthClientConfig
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
        secrets_path = state_dir / "secrets.enc"
        db_path = state_dir / "mailhub.sqlite"

        toggles = FeatureToggles()
        oauth = OAuthClientConfig()
        runtime = RuntimeFlags()
        routing = RoutingConfig()
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            t = data.get("toggles", {})
            toggles = FeatureToggles(**{**asdict(toggles), **_filter_dataclass_kwargs(FeatureToggles, t)})
            o = data.get("oauth", {})
            oauth = OAuthClientConfig(**{**asdict(oauth), **_filter_dataclass_kwargs(OAuthClientConfig, o)})
            r = data.get("runtime", {})
            runtime = RuntimeFlags(**{**asdict(runtime), **_filter_dataclass_kwargs(RuntimeFlags, r)})
            rt = data.get("routing", {})
            routing = RoutingConfig(**{**asdict(routing), **_filter_dataclass_kwargs(RoutingConfig, rt)})

        return cls(
            state_dir=state_dir,
            db_path=db_path,
            settings_path=settings_path,
            secrets_path=secrets_path,
            toggles=toggles,
            oauth=oauth,
            runtime=runtime,
            routing=routing,
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.ensure_dirs()
        payload: Dict[str, Any] = {
            "toggles": asdict(self.toggles),
            "oauth": asdict(self.oauth),
            "runtime": asdict(self.runtime),
            "routing": asdict(self.routing),
        }
        self.settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def disclosure_text(self) -> str:
        return self.toggles.disclosure_line.replace("<AgentName>", self.toggles.agent_display_name)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state_dir": str(self.state_dir),
            "db_path": str(self.db_path),
            "settings_path": str(self.settings_path),
            "toggles": asdict(self.toggles),
            "oauth": asdict(self.oauth),
            "runtime": asdict(self.runtime),
            "routing": asdict(self.routing),
        }

    def effective_mode(self) -> str:
        v = (os.environ.get("MAILHUB_MODE") or self.routing.mode or "openclaw").strip().lower()
        if v not in ("openclaw", "standalone"):
            return "openclaw"
        return v

    def effective_openclaw_json_path(self) -> str:
        return (
            os.environ.get("MAILHUB_OPENCLAW_JSON_PATH")
            or self.routing.openclaw_json_path
            or "~/.openclaw/openclaw.json"
        ).strip()

    def effective_standalone_agent_cmd(self) -> str:
        return (
            os.environ.get("MAILHUB_STANDALONE_AGENT_CMD")
            or self.routing.standalone_agent_cmd
            or os.environ.get("MAILHUB_OPENCLAW_AGENT_CMD")
            or ""
        ).strip()

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
                    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                        val = val[1:-1]
                    return val.strip()
            except Exception:
                continue
        return ""

    def effective_google_client_id(self) -> str:
        return (
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
            or self.oauth.google_client_id
            or self._dotenv_value("GOOGLE_OAUTH_CLIENT_ID")
            or ""
        ).strip()

    def effective_google_client_secret(self) -> str:
        return (
            os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
            or self.oauth.google_client_secret
            or self._dotenv_value("GOOGLE_OAUTH_CLIENT_SECRET")
            or ""
        ).strip()

    def effective_ms_client_id(self) -> str:
        return (
            os.environ.get("MS_OAUTH_CLIENT_ID")
            or self.oauth.ms_client_id
            or self._dotenv_value("MS_OAUTH_CLIENT_ID")
            or ""
        ).strip()


def _filter_dataclass_kwargs(dc: type[Any], data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {f.name for f in fields(dc)}
    return {k: v for k, v in data.items() if k in allowed}
