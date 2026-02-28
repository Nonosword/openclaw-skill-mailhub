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
    config_confirmed: bool = False
    config_confirmed_at: str = ""


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

    @staticmethod
    def default_state_dir() -> Path:
        p = os.environ.get("MAILHUB_STATE_DIR")
        if p:
            return Path(p).expanduser()
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
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            t = data.get("toggles", {})
            toggles = FeatureToggles(**{**asdict(toggles), **_filter_dataclass_kwargs(FeatureToggles, t)})
            o = data.get("oauth", {})
            oauth = OAuthClientConfig(**{**asdict(oauth), **_filter_dataclass_kwargs(OAuthClientConfig, o)})
            r = data.get("runtime", {})
            runtime = RuntimeFlags(**{**asdict(runtime), **_filter_dataclass_kwargs(RuntimeFlags, r)})

        return cls(
            state_dir=state_dir,
            db_path=db_path,
            settings_path=settings_path,
            secrets_path=secrets_path,
            toggles=toggles,
            oauth=oauth,
            runtime=runtime,
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.ensure_dirs()
        payload: Dict[str, Any] = {
            "toggles": asdict(self.toggles),
            "oauth": asdict(self.oauth),
            "runtime": asdict(self.runtime),
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
        }


def _filter_dataclass_kwargs(dc: type[Any], data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {f.name for f in fields(dc)}
    return {k: v for k, v in data.items() if k in allowed}
