from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict


_CONFIGURED = False
_SENSITIVE_KEYWORDS = ("token", "password", "secret", "dbkey", "credential", "authorization")


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = (os.environ.get("MAILHUB_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger("mailhub")
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.handlers.clear()
    logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    clean = (name or "").strip()
    if not clean:
        return logging.getLogger("mailhub")
    if clean.startswith("mailhub."):
        return logging.getLogger(clean)
    if clean == "mailhub":
        return logging.getLogger(clean)
    if clean.startswith("__main__"):
        return logging.getLogger("mailhub.cli")
    return logging.getLogger(f"mailhub.{clean}")


def log_event(logger: logging.Logger, event: str, level: str = "info", **fields: Any) -> None:
    parts = [f"event={_format_value(event)}"]
    for key in sorted(fields.keys()):
        parts.append(f"{key}={_format_field(key, fields[key])}")
    line = " ".join(parts)
    fn = getattr(logger, level, logger.info)
    fn(line)


def _format_field(key: str, value: Any) -> str:
    lower = key.lower()
    if any(word in lower for word in _SENSITIVE_KEYWORDS):
        return "<redacted>"
    return _format_value(value)


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        cleaned = " ".join(value.strip().split())
        if len(cleaned) > 240:
            cleaned = cleaned[:240] + "...(truncated)"
        return json.dumps(cleaned, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return _format_json_like(list(value))
    if isinstance(value, dict):
        return _format_json_like(value)
    return _format_value(str(value))


def _format_json_like(value: Dict[str, Any] | list[Any]) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    if len(text) > 240:
        text = text[:240] + "...(truncated)"
    return json.dumps(text, ensure_ascii=False)
