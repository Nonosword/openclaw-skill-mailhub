from __future__ import annotations

from datetime import datetime, timezone, timedelta
from dateutil import parser


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(since: str) -> datetime:
    """
    since: "15m" | "2h" | "7d" | ISO8601
    """
    since = since.strip()
    if since.endswith("m"):
        return datetime.now(timezone.utc) - timedelta(minutes=int(since[:-1]))
    if since.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=int(since[:-1]))
    if since.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(since[:-1]))
    dt = parser.isoparse(since)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def yyyy_mm_dd_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def today_yyyy_mm_dd_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")