from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .store import DB
from ..shared.time import utc_now_iso


SECRET_PREFIX = "secret:"


@dataclass
class SecretStore:
    """
    Secret values are stored inside SQLCipher-encrypted SQLite `kv` table.
    """

    db_path: Path

    def _key(self, name: str) -> str:
        return f"{SECRET_PREFIX}{name}"

    def set(self, name: str, value: str) -> None:
        db = DB(self.db_path)
        db.init()
        db.kv_set(self._key(name), value, utc_now_iso())

    def get(self, name: str) -> Optional[str]:
        db = DB(self.db_path)
        db.init()
        return db.kv_get(self._key(name))

    def delete(self, name: str) -> None:
        db = DB(self.db_path)
        db.init()
        db.kv_delete(self._key(name))
