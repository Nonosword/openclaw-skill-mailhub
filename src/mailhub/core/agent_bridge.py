from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings


def agent_enabled() -> bool:
    s = Settings.load()
    mode = s.effective_mode()
    if mode != "standalone":
        return False
    return s.effective_standalone_agent_enabled()


def _prompt_text(name: str) -> str:
    s = Settings.load()
    p = s.resolve_skill_path(f"config/prompts/{name}")
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    # Fast path
    if raw.startswith("{") and raw.endswith("}"):
        try:
            out = json.loads(raw)
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    # Best effort: parse the last json object line
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not (ln.startswith("{") and ln.endswith("}")):
            continue
        try:
            out = json.loads(ln)
            if isinstance(out, dict):
                return out
        except Exception:
            continue
    return None


def _build_cmd_from_models(models: Dict[str, Any], *, openclaw_json_path: str) -> List[str]:
    runner = models.get("runner") if isinstance(models, dict) else {}
    runner = runner if isinstance(runner, dict) else {}
    command = str(runner.get("command") or "").strip()
    args_raw = runner.get("args", [])
    args: List[str] = []
    if isinstance(args_raw, list):
        args = [str(x) for x in args_raw]
    elif isinstance(args_raw, str):
        args = shlex.split(args_raw)

    agent = models.get("agent") if isinstance(models, dict) else {}
    agent = agent if isinstance(agent, dict) else {}
    defaults = models.get("defaults") if isinstance(models, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    agent_id = str(agent.get("id") or defaults.get("primary_model") or "").strip()

    if not command:
        return []
    values = {
        "agent_id": agent_id,
        "openclaw_json_path": openclaw_json_path,
    }

    head = shlex.split(command.format(**values))
    out = list(head)
    out.extend([a.format(**values) for a in args])
    return [x for x in out if x.strip()]


def run_agent(task: str, payload: Dict[str, Any], prompt_file: str) -> Optional[Dict[str, Any]]:
    if not agent_enabled():
        return None

    s = Settings.load()
    s.ensure_dirs()
    models = s.load_standalone_models()
    cmd_argv = _build_cmd_from_models(models, openclaw_json_path=s.effective_openclaw_json_path())
    if not cmd_argv:
        # No runner configured in models file.
        return None

    req = {
        "mode": s.effective_mode(),
        "task": task,
        "prompt": _prompt_text(prompt_file),
        "input": payload,
        "openclaw_json_path": s.effective_openclaw_json_path(),
        "standalone_models_path": s.effective_standalone_models_path(),
        "models": models,
    }

    try:
        cp = subprocess.run(
            cmd_argv,
            input=json.dumps(req, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=int(os.environ.get("MAILHUB_AGENT_TIMEOUT", "45")),
            check=False,
        )
    except Exception:
        return None

    if cp.returncode != 0:
        return None
    return _extract_json(cp.stdout)


def classify_email_with_agent(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return run_agent("classify_email", payload, "classify_email.md")


def summarize_bucket_with_agent(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return run_agent("summarize_bucket", payload, "summarize_bucket.md")


def draft_reply_with_agent(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return run_agent("draft_reply", payload, "draft_reply.md")
