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
    return os.environ.get("MAILHUB_USE_OPENCLAW_AGENT", "1").strip().lower() in ("1", "true", "yes", "on")


def _prompt_text(name: str) -> str:
    p = Path("config/prompts") / name
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


def run_agent(task: str, payload: Dict[str, Any], prompt_file: str) -> Optional[Dict[str, Any]]:
    if not agent_enabled():
        return None

    s = Settings.load()
    cmd = s.effective_standalone_agent_cmd()
    if not cmd:
        # Keep explicit and safe: no implicit shell invocation.
        return None

    req = {
        "mode": s.effective_mode(),
        "task": task,
        "prompt": _prompt_text(prompt_file),
        "input": payload,
        "openclaw_json_path": s.effective_openclaw_json_path(),
    }

    try:
        cp = subprocess.run(
            shlex.split(cmd),
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
