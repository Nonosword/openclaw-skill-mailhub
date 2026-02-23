from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


SERVICE_NAME = "mailhub"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return kdf.derive(passphrase.encode("utf-8"))


@dataclass
class SecretStore:
    secrets_path: Path
    keyring_prefix: str = SERVICE_NAME

    def _keyring_key(self, name: str) -> str:
        return f"{self.keyring_prefix}:{name}"

    def set(self, name: str, value: str) -> None:
        # First try keyring
        try:
            keyring.set_password(SERVICE_NAME, self._keyring_key(name), value)
            return
        except Exception:
            # fallback
            self._set_encrypted_file(name, value)

    def get(self, name: str) -> Optional[str]:
        try:
            v = keyring.get_password(SERVICE_NAME, self._keyring_key(name))
            if v:
                return v
        except Exception:
            pass
        return self._get_encrypted_file(name)

    def delete(self, name: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, self._keyring_key(name))
        except Exception:
            pass
        self._delete_encrypted_file(name)

    # ---------- encrypted file fallback ----------
    def _load_blob(self) -> Dict[str, Any]:
        if not self.secrets_path.exists():
            return {"v": 1, "salt": None, "nonce": None, "ct": None}
        return json.loads(self.secrets_path.read_text(encoding="utf-8"))

    def _save_blob(self, blob: Dict[str, Any]) -> None:
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets_path.write_text(json.dumps(blob, indent=2), encoding="utf-8")

    def _get_passphrase(self) -> str:
        # Prefer env var
        p = os.environ.get("MAILHUB_SECRET_PASSPHRASE")
        if p:
            return p
        # Interactive prompt (local only)
        import getpass
        return getpass.getpass("Enter passphrase to unlock MailHub secrets: ")

    def _decrypt_map(self) -> Dict[str, str]:
        blob = self._load_blob()
        if not blob.get("ct"):
            return {}
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        ct = base64.b64decode(blob["ct"])
        key = _derive_key(self._get_passphrase(), salt)
        aes = AESGCM(key)
        pt = aes.decrypt(nonce, ct, None)
        return json.loads(pt.decode("utf-8"))

    def _encrypt_map(self, m: Dict[str, str]) -> None:
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = _derive_key(self._get_passphrase(), salt)
        aes = AESGCM(key)
        pt = json.dumps(m).encode("utf-8")
        ct = aes.encrypt(nonce, pt, None)
        blob = {
            "v": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
        self._save_blob(blob)

    def _set_encrypted_file(self, name: str, value: str) -> None:
        m = self._decrypt_map()
        m[name] = value
        self._encrypt_map(m)

    def _get_encrypted_file(self, name: str) -> Optional[str]:
        try:
            m = self._decrypt_map()
            return m.get(name)
        except Exception:
            return None

    def _delete_encrypted_file(self, name: str) -> None:
        try:
            m = self._decrypt_map()
            if name in m:
                del m[name]
                self._encrypt_map(m)
        except Exception:
            pass