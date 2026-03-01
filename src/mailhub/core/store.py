from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from pysqlcipher3 import dbapi2 as sqlcipher
except Exception as _sqlcipher_import_error:  # pragma: no cover - runtime env dependent
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except Exception as _sqlcipher_import_error2:  # pragma: no cover - runtime env dependent
        sqlcipher = None
        SQLCIPHER_IMPORT_ERROR = (_sqlcipher_import_error, _sqlcipher_import_error2)
    else:  # pragma: no cover - runtime env dependent
        SQLCIPHER_IMPORT_ERROR = None
else:  # pragma: no cover - runtime env dependent
    SQLCIPHER_IMPORT_ERROR = None

from .dbkey_backend import (
    default_local_dbkey_path,
    read_dbkey,
)


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- google|microsoft|imap|caldav|carddav
  email TEXT,                    -- for mail providers
  meta_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,           -- provider unique id or composite key
  provider_id TEXT NOT NULL,
  thread_id TEXT,
  from_addr TEXT,
  to_addrs TEXT,
  subject TEXT,
  date_utc TEXT,
  snippet TEXT,
  body_text TEXT,
  body_html TEXT,
  has_attachments INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_provider_date ON messages(provider_id, date_utc);

CREATE TABLE IF NOT EXISTS message_tags (
  message_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 1.0,
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(message_id, tag),
  FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS reply_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- pending|sent|skipped
  send_mode TEXT NOT NULL DEFAULT 'manual', -- manual|auto
  short_reason TEXT,
  drafted_subject TEXT,
  drafted_body TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS billing_statements (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  issuer TEXT,
  statement_month TEXT,          -- YYYY-MM
  total_due REAL,
  due_date TEXT,
  currency TEXT,
  extracted_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TEXT NOT NULL
  -- secret:* keys hold provider credentials/tokens inside SQLCipher-encrypted DB
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  filename TEXT,
  content_type TEXT,
  size_bytes INTEGER,
  stored_path TEXT NOT NULL,
  sha256 TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_attach_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS message_analysis (
  message_id TEXT PRIMARY KEY,
  title TEXT,
  summary TEXT,
  tag TEXT,
  suggest_reply INTEGER NOT NULL DEFAULT 0,
  suggestion TEXT,
  source TEXT NOT NULL DEFAULT 'openclaw', -- openclaw|standalone
  updated_at TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_message_analysis_tag ON message_analysis(tag);

CREATE TABLE IF NOT EXISTS calendar_events (
  provider_id TEXT NOT NULL,
  provider_kind TEXT NOT NULL,    -- google|microsoft|caldav
  event_id TEXT NOT NULL,         -- provider event id
  title TEXT,
  start_utc TEXT NOT NULL,        -- ISO8601 UTC
  end_utc TEXT NOT NULL,          -- ISO8601 UTC
  location TEXT,
  status TEXT,
  description TEXT,
  raw_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(provider_id, event_id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start_utc);
CREATE INDEX IF NOT EXISTS idx_calendar_events_provider_start ON calendar_events(provider_id, start_utc);
"""

@dataclass
class DB:
    path: Path
    dbkey: bytes | None = None
    dbkey_backend: str = ""
    dbkey_local_path: Path | None = None
    dbkey_keychain_account: str = ""

    def _restrict_fs_permissions(self) -> None:
        if os.name == "nt":
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        except Exception:
            pass
        for p in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            try:
                if p.exists():
                    os.chmod(p, 0o600)
            except Exception:
                pass

    def _resolve_dbkey(self) -> bytes:
        if self.dbkey is not None:
            return self.dbkey

        from .config import Settings

        s = Settings.load()
        backend = (self.dbkey_backend or s.effective_dbkey_backend()).strip().lower()
        local_path = self.dbkey_local_path or default_local_dbkey_path(
            s.state_dir, s.security.dbkey_local_path
        )
        account = self.dbkey_keychain_account or s.effective_dbkey_keychain_account()
        key = read_dbkey(
            backend=backend,
            state_dir=s.state_dir,
            local_dbkey_path=local_path,
            keychain_account=account,
        )
        if len(key) != 32:
            raise RuntimeError("Invalid dbkey length (must be 32 bytes).")
        self.dbkey = key
        return key

    def connect(self):
        if sqlcipher is None:
            raise RuntimeError(
                "SQLCipher driver unavailable. Install `sqlcipher3-binary` or `pysqlcipher3`. "
                f"import_error={SQLCIPHER_IMPORT_ERROR!r}"
            )
        con = sqlcipher.connect(str(self.path))
        con.row_factory = sqlcipher.Row
        key = self._resolve_dbkey()
        hex_key = key.hex()
        con.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        con.execute("PRAGMA cipher_memory_security = ON;")
        con.execute("PRAGMA foreign_keys = ON;")
        # Force key validation early.
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con

    def init(self) -> None:
        con = self.connect()
        try:
            con.executescript(SCHEMA)
            # Backward-compatible migration for existing DBs.
            try:
                con.execute("ALTER TABLE reply_queue ADD COLUMN send_mode TEXT NOT NULL DEFAULT 'manual'")
            except Exception:
                pass
            con.commit()
        finally:
            con.close()
        self._restrict_fs_permissions()

    def upsert_provider(self, pid: str, kind: str, email: str | None, meta_json: str, created_at: str) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO providers (id, kind, email, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  kind=excluded.kind,
                  email=excluded.email,
                  meta_json=excluded.meta_json
                """,
                (pid, kind, email, meta_json, created_at),
            )
            con.commit()
        finally:
            con.close()

    def list_providers(self) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute("SELECT * FROM providers ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def get_provider(self, pid: str) -> Optional[Dict[str, Any]]:
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def upsert_message(self, msg: Dict[str, Any]) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO messages (
                  id, provider_id, thread_id, from_addr, to_addrs, subject, date_utc,
                  snippet, body_text, body_html, has_attachments, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  snippet=excluded.snippet,
                  body_text=COALESCE(excluded.body_text, messages.body_text),
                  body_html=COALESCE(excluded.body_html, messages.body_html),
                  has_attachments=excluded.has_attachments,
                  raw_json=excluded.raw_json
                """,
                (
                    msg["id"],
                    msg["provider_id"],
                    msg.get("thread_id"),
                    msg.get("from_addr"),
                    msg.get("to_addrs"),
                    msg.get("subject"),
                    msg.get("date_utc"),
                    msg.get("snippet"),
                    msg.get("body_text"),
                    msg.get("body_html"),
                    int(msg.get("has_attachments", 0)),
                    msg.get("raw_json"),
                    msg["created_at"],
                ),
            )
            con.commit()
        finally:
            con.close()

    def set_message_tag(self, message_id: str, tag: str, score: float, reason: str | None, created_at: str) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO message_tags (message_id, tag, score, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(message_id, tag) DO UPDATE SET
                  score=excluded.score,
                  reason=excluded.reason
                """,
                (message_id, tag, float(score), reason, created_at),
            )
            con.commit()
        finally:
            con.close()

    def get_messages_by_date(self, date_prefix_yyyy_mm_dd: str) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT rowid AS mail_id, * FROM messages WHERE date_utc LIKE ? ORDER BY date_utc DESC",
                (f"{date_prefix_yyyy_mm_dd}%",),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        con = self.connect()
        try:
            row = con.execute("SELECT rowid AS mail_id, * FROM messages WHERE id=?", (message_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def resolve_message_id(self, message_ref: str) -> Optional[str]:
        raw = (message_ref or "").strip()
        if not raw:
            return None
        con = self.connect()
        try:
            if raw.isdigit():
                row = con.execute(
                    "SELECT id FROM messages WHERE rowid=? LIMIT 1",
                    (int(raw),),
                ).fetchone()
                if row:
                    return str(row["id"])
            row = con.execute("SELECT id FROM messages WHERE id=? LIMIT 1", (raw,)).fetchone()
            return str(row["id"]) if row else None
        finally:
            con.close()

    def get_tags_for_message(self, message_id: str) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT * FROM message_tags WHERE message_id=? ORDER BY score DESC",
                (message_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def list_tag_counts_for_date(self, date_prefix_yyyy_mm_dd: str) -> List[Tuple[str, int]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT tag, COUNT(*) AS c
                FROM message_tags mt
                JOIN messages m ON m.id=mt.message_id
                WHERE m.date_utc LIKE ?
                GROUP BY tag
                ORDER BY c DESC
                """,
                (f"{date_prefix_yyyy_mm_dd}%",),
            ).fetchall()
            return [(r["tag"], int(r["c"])) for r in rows]
        finally:
            con.close()

    def enqueue_reply(self, message_id: str, status: str, short_reason: str | None, now: str) -> int:
        con = self.connect()
        try:
            existing = con.execute(
                """
                SELECT id FROM reply_queue
                WHERE message_id=? AND status='pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = con.execute(
                """
                INSERT INTO reply_queue (message_id, status, short_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, status, short_reason, now, now),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def list_reply_queue(self, status: str = "pending", limit: int = 50) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT rq.*, m.rowid AS mail_id, m.subject, m.from_addr, m.date_utc, m.provider_id
                FROM reply_queue rq
                JOIN messages m ON m.id=rq.message_id
                WHERE rq.status=?
                ORDER BY rq.created_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def get_reply_queue_item(self, rq_id: int) -> Optional[Dict[str, Any]]:
        con = self.connect()
        try:
            row = con.execute(
                """
                SELECT rq.*, m.rowid AS mail_id, m.subject, m.from_addr, m.date_utc, m.provider_id
                FROM reply_queue rq
                JOIN messages m ON m.id=rq.message_id
                WHERE rq.id=?
                LIMIT 1
                """,
                (rq_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def update_reply_draft(self, rq_id: int, subject: str, body: str, now: str) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                UPDATE reply_queue
                SET drafted_subject=?, drafted_body=?, updated_at=?
                WHERE id=?
                """,
                (subject, body, now, rq_id),
            )
            con.commit()
        finally:
            con.close()

    def mark_reply_status(self, rq_id: int, status: str, now: str, send_mode: str | None = None) -> None:
        con = self.connect()
        try:
            if send_mode:
                con.execute(
                    "UPDATE reply_queue SET status=?, send_mode=?, updated_at=? WHERE id=?",
                    (status, send_mode, now, rq_id),
                )
            else:
                con.execute(
                    "UPDATE reply_queue SET status=?, updated_at=? WHERE id=?",
                    (status, now, rq_id),
                )
            con.commit()
        finally:
            con.close()

    def list_reply_queue_by_message_date(self, date_prefix_yyyy_mm_dd: str, status: str, limit: int = 200) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT rq.*, m.rowid AS mail_id, m.subject, m.from_addr, m.date_utc, m.provider_id
                FROM reply_queue rq
                JOIN messages m ON m.id=rq.message_id
                WHERE rq.status=? AND m.date_utc LIKE ?
                ORDER BY rq.updated_at DESC
                LIMIT ?
                """,
                (status, f"{date_prefix_yyyy_mm_dd}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def reply_status_counts_by_message_date(self, date_prefix_yyyy_mm_dd: str) -> Dict[str, int]:
        con = self.connect()
        out: Dict[str, int] = {"pending": 0, "sent": 0, "skipped": 0, "auto_sent": 0}
        try:
            rows = con.execute(
                """
                SELECT rq.status AS status, rq.send_mode AS send_mode, COUNT(*) AS c
                FROM reply_queue rq
                JOIN messages m ON m.id=rq.message_id
                WHERE m.date_utc LIKE ?
                GROUP BY rq.status, rq.send_mode
                """,
                (f"{date_prefix_yyyy_mm_dd}%",),
            ).fetchall()
            for r in rows:
                status = r["status"]
                c = int(r["c"])
                if status in out:
                    out[status] += c
                if status == "sent" and (r["send_mode"] or "") == "auto":
                    out["auto_sent"] += c
            return out
        finally:
            con.close()

    def upsert_message_analysis(
        self,
        *,
        message_id: str,
        title: str,
        summary: str,
        tag: str,
        suggest_reply: bool,
        suggestion: str,
        source: str,
        updated_at: str,
    ) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO message_analysis
                (message_id, title, summary, tag, suggest_reply, suggestion, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                  title=excluded.title,
                  summary=excluded.summary,
                  tag=excluded.tag,
                  suggest_reply=excluded.suggest_reply,
                  suggestion=excluded.suggestion,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (message_id, title, summary, tag, int(bool(suggest_reply)), suggestion, source, updated_at),
            )
            con.commit()
        finally:
            con.close()

    def list_message_analysis_by_date(self, date_prefix_yyyy_mm_dd: str, limit: int = 200) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT ma.*, m.rowid AS mail_id, m.subject, m.from_addr, m.date_utc
                FROM message_analysis ma
                JOIN messages m ON m.id=ma.message_id
                WHERE m.date_utc LIKE ?
                ORDER BY ma.updated_at DESC
                LIMIT ?
                """,
                (f"{date_prefix_yyyy_mm_dd}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def upsert_statement(self, st: Dict[str, Any]) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO billing_statements (
                  id, message_id, issuer, statement_month, total_due, due_date, currency, extracted_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  issuer=excluded.issuer,
                  statement_month=excluded.statement_month,
                  total_due=excluded.total_due,
                  due_date=excluded.due_date,
                  currency=excluded.currency,
                  extracted_json=excluded.extracted_json
                """,
                (
                    st["id"],
                    st["message_id"],
                    st.get("issuer"),
                    st.get("statement_month"),
                    st.get("total_due"),
                    st.get("due_date"),
                    st.get("currency"),
                    st.get("extracted_json"),
                    st["created_at"],
                ),
            )
            con.commit()
        finally:
            con.close()

    def list_statements_for_month(self, yyyy_mm: str) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT * FROM billing_statements WHERE statement_month=? ORDER BY created_at DESC",
                (yyyy_mm,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def kv_get(self, key: str) -> Optional[str]:
        con = self.connect()
        try:
            row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return str(row["v"]) if row else None
        finally:
            con.close()

    def kv_set(self, key: str, value: str, updated_at: str) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO kv (k, v, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(k) DO UPDATE SET
                  v=excluded.v,
                  updated_at=excluded.updated_at
                """,
                (key, value, updated_at),
            )
            con.commit()
        finally:
            con.close()

    def kv_delete(self, key: str) -> None:
        con = self.connect()
        try:
            con.execute("DELETE FROM kv WHERE k=?", (key,))
            con.commit()
        finally:
            con.close()

    def upsert_calendar_event(
        self,
        *,
        provider_id: str,
        provider_kind: str,
        event_id: str,
        title: str,
        start_utc: str,
        end_utc: str,
        location: str,
        status: str,
        description: str,
        raw_json: str,
        updated_at: str,
    ) -> None:
        con = self.connect()
        try:
            con.execute(
                """
                INSERT INTO calendar_events (
                  provider_id, provider_kind, event_id, title, start_utc, end_utc,
                  location, status, description, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, event_id) DO UPDATE SET
                  title=excluded.title,
                  start_utc=excluded.start_utc,
                  end_utc=excluded.end_utc,
                  location=excluded.location,
                  status=excluded.status,
                  description=excluded.description,
                  raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at
                """,
                (
                    provider_id,
                    provider_kind,
                    event_id,
                    title,
                    start_utc,
                    end_utc,
                    location,
                    status,
                    description,
                    raw_json,
                    updated_at,
                ),
            )
            con.commit()
        finally:
            con.close()

    def delete_calendar_event(self, provider_id: str, event_id: str) -> None:
        con = self.connect()
        try:
            con.execute(
                "DELETE FROM calendar_events WHERE provider_id=? AND event_id=?",
                (provider_id, event_id),
            )
            con.commit()
        finally:
            con.close()

    def list_calendar_events(
        self,
        *,
        start_utc: str,
        end_utc: str,
        provider_id: str = "",
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            if provider_id:
                rows = con.execute(
                    """
                    SELECT * FROM calendar_events
                    WHERE provider_id=?
                      AND end_utc >= ?
                      AND start_utc <= ?
                    ORDER BY start_utc ASC
                    LIMIT ?
                    """,
                    (provider_id, start_utc, end_utc, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT * FROM calendar_events
                    WHERE end_utc >= ?
                      AND start_utc <= ?
                    ORDER BY start_utc ASC
                    LIMIT ?
                    """,
                    (start_utc, end_utc, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def find_calendar_events_by_event_id(self, event_id: str) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT * FROM calendar_events WHERE event_id=? ORDER BY updated_at DESC",
                (event_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def list_messages_in_range(
        self,
        *,
        start_utc: str,
        end_utc: str,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT rowid AS mail_id, * FROM messages
                WHERE date_utc >= ? AND date_utc < ?
                ORDER BY date_utc DESC
                LIMIT ?
                """,
                (start_utc, end_utc, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def list_tag_counts_in_range(self, *, start_utc: str, end_utc: str) -> List[Tuple[str, int]]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT mt.tag AS tag, COUNT(*) AS c
                FROM message_tags mt
                JOIN messages m ON m.id = mt.message_id
                WHERE m.date_utc >= ? AND m.date_utc < ?
                GROUP BY mt.tag
                ORDER BY c DESC
                """,
                (start_utc, end_utc),
            ).fetchall()
            return [(str(r["tag"]), int(r["c"])) for r in rows]
        finally:
            con.close()

    def reply_status_counts_in_range(self, *, start_utc: str, end_utc: str) -> Dict[str, int]:
        con = self.connect()
        out: Dict[str, int] = {"pending": 0, "sent": 0, "skipped": 0, "auto_sent": 0}
        try:
            rows = con.execute(
                """
                SELECT rq.status AS status, rq.send_mode AS send_mode, COUNT(*) AS c
                FROM reply_queue rq
                JOIN messages m ON m.id = rq.message_id
                WHERE m.date_utc >= ? AND m.date_utc < ?
                GROUP BY rq.status, rq.send_mode
                """,
                (start_utc, end_utc),
            ).fetchall()
            for r in rows:
                status = str(r["status"] or "")
                c = int(r["c"] or 0)
                if status in out:
                    out[status] += c
                if status == "sent" and str(r["send_mode"] or "") == "auto":
                    out["auto_sent"] += c
            return out
        finally:
            con.close()
