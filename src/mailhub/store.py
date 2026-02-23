from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


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
"""

@dataclass
class DB:
    path: Path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        con = self.connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

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
                "SELECT * FROM messages WHERE date_utc LIKE ? ORDER BY date_utc DESC",
                (f"{date_prefix_yyyy_mm_dd}%",),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return dict(row) if row else None
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
                SELECT rq.*, m.subject, m.from_addr, m.date_utc
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

    def mark_reply_status(self, rq_id: int, status: str, now: str) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE reply_queue SET status=?, updated_at=? WHERE id=?",
                (status, now, rq_id),
            )
            con.commit()
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