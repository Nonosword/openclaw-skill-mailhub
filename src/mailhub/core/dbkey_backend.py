from __future__ import annotations

import base64
import getpass
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import keyring
except Exception as _keyring_import_error:  # pragma: no cover - runtime env dependent
    keyring = None
    KEYRING_IMPORT_ERROR = _keyring_import_error
else:  # pragma: no cover - runtime env dependent
    KEYRING_IMPORT_ERROR = None


BACKEND_KEYCHAIN = "keychain"
BACKEND_SYSTEMD = "systemd"
BACKEND_LOCAL = "local"
BACKEND_ORDER = [BACKEND_KEYCHAIN, BACKEND_SYSTEMD, BACKEND_LOCAL]
KEYCHAIN_SERVICE = "mailhub.dbkey"
KEYCHAIN_ACCOUNT = "default"


@dataclass
class BackendCheck:
    backend: str
    available: bool
    reason: str
    suggestion: str
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_backend(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in (BACKEND_KEYCHAIN, BACKEND_SYSTEMD, BACKEND_LOCAL):
        return v
    return ""


def default_local_dbkey_path(state_dir: Path, configured_path: str = "") -> Path:
    raw = (configured_path or "").strip()
    if not raw:
        return state_dir / "dbkey.enc"
    p = Path(os.path.expandvars(raw)).expanduser()
    if p.is_absolute():
        return p
    return state_dir / p


def detect_backends(*, state_dir: Path, local_dbkey_path: Path) -> Dict[str, BackendCheck]:
    out: Dict[str, BackendCheck] = {
        BACKEND_KEYCHAIN: _detect_keychain(),
        BACKEND_SYSTEMD: _detect_systemd(),
        BACKEND_LOCAL: _detect_local(state_dir=state_dir, local_dbkey_path=local_dbkey_path),
    }
    return out


def pick_backend(checks: Dict[str, BackendCheck]) -> str:
    for name in BACKEND_ORDER:
        item = checks.get(name)
        if item and item.available:
            return name
    return BACKEND_LOCAL


def generate_dbkey() -> bytes:
    return secrets.token_bytes(32)


def read_dbkey(
    *,
    backend: str,
    state_dir: Path,
    local_dbkey_path: Path,
    keychain_account: str = KEYCHAIN_ACCOUNT,
) -> bytes:
    b = normalize_backend(backend)
    if b == BACKEND_KEYCHAIN:
        return _read_keychain(keychain_account=keychain_account)
    if b == BACKEND_SYSTEMD:
        return _read_systemd_key()
    if b == BACKEND_LOCAL:
        return _read_local_key(local_dbkey_path, state_dir=state_dir)
    raise RuntimeError(f"Unsupported dbkey backend: {backend}")


def write_dbkey(
    *,
    backend: str,
    key: bytes,
    state_dir: Path,
    local_dbkey_path: Path,
    keychain_account: str = KEYCHAIN_ACCOUNT,
) -> None:
    if len(key) != 32:
        raise RuntimeError("dbkey must be exactly 32 bytes")
    b = normalize_backend(backend)
    if b == BACKEND_KEYCHAIN:
        _write_keychain(key, keychain_account=keychain_account)
        return
    if b == BACKEND_SYSTEMD:
        _write_systemd_key(key)
        return
    if b == BACKEND_LOCAL:
        _write_local_key(local_dbkey_path, key, state_dir=state_dir)
        return
    raise RuntimeError(f"Unsupported dbkey backend: {backend}")


def delete_dbkey(
    *,
    backend: str,
    state_dir: Path,
    local_dbkey_path: Path,
    keychain_account: str = KEYCHAIN_ACCOUNT,
) -> None:
    b = normalize_backend(backend)
    if b == BACKEND_KEYCHAIN:
        if keyring is None:
            return
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, keychain_account)
        except Exception:
            pass
        return
    if b == BACKEND_SYSTEMD:
        dbkey_file = (os.environ.get("MAILHUB_DBKEY_FILE") or "").strip()
        if dbkey_file:
            try:
                Path(os.path.expandvars(dbkey_file)).expanduser().unlink(missing_ok=True)
            except Exception:
                pass
        return
    if b == BACKEND_LOCAL:
        try:
            local_dbkey_path.unlink(missing_ok=True)
        except Exception:
            pass
        return


def _detect_keychain() -> BackendCheck:
    if keyring is None:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason=f"Python keyring module unavailable: {KEYRING_IMPORT_ERROR!r}",
            suggestion="Install `keyring` or use local/systemd backend.",
            evidence={"keyring_import": False},
        )
    if sys.platform == "darwin":
        return _detect_keychain_macos()
    if sys.platform.startswith("linux"):
        return _detect_keychain_linux()
    return BackendCheck(
        backend=BACKEND_KEYCHAIN,
        available=False,
        reason="unsupported platform for built-in keychain integration",
        suggestion="Use local backend on this platform.",
        evidence={"platform": sys.platform},
    )


def _detect_keychain_macos() -> BackendCheck:
    ok, _, err = _run_cmd(["security", "list-keychains"])
    if not ok:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason="`security list-keychains` failed",
            suggestion="Check macOS Keychain access and login keychain unlock status.",
            evidence={"stderr": err[:200]},
        )
    probe_ok, probe_reason = _probe_keyring_roundtrip()
    if not probe_ok:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason=f"Keychain write/read probe failed: {probe_reason}",
            suggestion="Allow keychain access prompt and ensure login keychain is writable.",
            evidence={"probe": "keyring_roundtrip"},
        )
    return BackendCheck(
        backend=BACKEND_KEYCHAIN,
        available=True,
        reason="Keychain is reachable and writable",
        suggestion="",
        evidence={"security_list_keychains": True, "probe": "keyring_roundtrip"},
    )


def _detect_keychain_linux() -> BackendCheck:
    bus = (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    if not bus:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason="DBUS_SESSION_BUS_ADDRESS is empty",
            suggestion="Run in a desktop session with DBus/Secret Service, or use local backend.",
            evidence={"dbus_session": False},
        )
    if not shutil.which("gdbus"):
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason="`gdbus` command not found",
            suggestion="Install gdbus (usually in glib2 package) or use local backend.",
            evidence={"dbus_session": True, "gdbus": False},
        )

    ping_ok, _, ping_err = _run_cmd(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.secrets",
            "--object-path",
            "/org/freedesktop/secrets",
            "--method",
            "org.freedesktop.DBus.Peer.Ping",
        ]
    )
    if not ping_ok:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason="Secret Service ping failed (org.freedesktop.secrets)",
            suggestion="Headless/container sessions often lack Secret Service; use local backend or enable a desktop keyring daemon.",
            evidence={"dbus_session": True, "ping_ok": False, "stderr": ping_err[:200]},
        )

    probe_ok, probe_reason = _probe_keyring_roundtrip()
    if probe_ok:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=True,
            reason="Secret Service is reachable and keyring probe succeeded",
            suggestion="",
            evidence={"dbus_session": True, "ping_ok": True, "probe": "keyring_roundtrip"},
        )

    if not shutil.which("secret-tool"):
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=False,
            reason=f"Secret Service reachable but write/read probe failed: {probe_reason}",
            suggestion=(
                "Install libsecret-tools (`sudo apt-get install -y libsecret-tools`) "
                "or fix Python keyring backend permissions."
            ),
            evidence={"dbus_session": True, "ping_ok": True, "secret_tool": False},
        )

    st_ok, st_reason = _probe_secret_tool_roundtrip()
    if st_ok:
        return BackendCheck(
            backend=BACKEND_KEYCHAIN,
            available=True,
            reason="Secret Service reachable and secret-tool probe succeeded",
            suggestion="",
            evidence={"dbus_session": True, "ping_ok": True, "probe": "secret-tool"},
        )
    return BackendCheck(
        backend=BACKEND_KEYCHAIN,
        available=False,
        reason=f"Secret Service probe failed: {st_reason}",
        suggestion="Check keyring daemon unlock state and Secret Service permissions.",
        evidence={"dbus_session": True, "ping_ok": True, "secret_tool": True},
    )


def _detect_systemd() -> BackendCheck:
    path, source = _systemd_dbkey_file()
    if path and path.exists() and path.is_file() and os.access(path, os.R_OK):
        try:
            key = _load_key_material(path.read_bytes())
            if len(key) != 32:
                raise ValueError("invalid key length")
        except Exception as exc:
            return BackendCheck(
                backend=BACKEND_SYSTEMD,
                available=False,
                reason=f"systemd credential file unreadable/invalid: {exc}",
                suggestion="Provide a valid 32-byte dbkey file via LoadCredential or MAILHUB_DBKEY_FILE.",
                evidence={"source": source, "path": str(path)},
            )
        return BackendCheck(
            backend=BACKEND_SYSTEMD,
            available=True,
            reason="systemd credential file is readable",
            suggestion="",
            evidence={"source": source, "path": str(path)},
        )

    if shutil.which("systemctl"):
        ok, out, err = _run_cmd(["systemctl", "is-system-running"])
        state = out.strip() if ok else err.strip()
        return BackendCheck(
            backend=BACKEND_SYSTEMD,
            available=False,
            reason="systemd detected but no injected dbkey credential file available",
            suggestion=(
                "Run as a systemd service with LoadCredential=dbkey:/path/to/dbkey "
                "or Environment=MAILHUB_DBKEY_FILE=%d/dbkey."
            ),
            evidence={"systemctl": True, "is_system_running": state},
        )

    return BackendCheck(
        backend=BACKEND_SYSTEMD,
        available=False,
        reason="systemd not detected",
        suggestion="Use keychain or local backend.",
        evidence={"systemctl": False},
    )


def _detect_local(*, state_dir: Path, local_dbkey_path: Path) -> BackendCheck:
    try:
        _ensure_private_dir(state_dir)
        local_dbkey_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_dir(local_dbkey_path.parent)
    except Exception as exc:
        return BackendCheck(
            backend=BACKEND_LOCAL,
            available=False,
            reason=f"cannot prepare local path: {exc}",
            suggestion="Ensure state directory is writable.",
            evidence={"state_dir": str(state_dir), "local_dbkey_path": str(local_dbkey_path)},
        )
    return BackendCheck(
        backend=BACKEND_LOCAL,
        available=True,
        reason="local dbkey file path is writable",
        suggestion="",
        evidence={"state_dir": str(state_dir), "local_dbkey_path": str(local_dbkey_path)},
    )


def _systemd_dbkey_file() -> Tuple[Path | None, str]:
    env_file = (os.environ.get("MAILHUB_DBKEY_FILE") or "").strip()
    if env_file:
        return Path(os.path.expandvars(env_file)).expanduser(), "MAILHUB_DBKEY_FILE"
    cred_dir = (os.environ.get("CREDENTIALS_DIRECTORY") or "").strip()
    if cred_dir:
        return (Path(cred_dir) / "dbkey"), "CREDENTIALS_DIRECTORY"
    return None, ""


def _read_systemd_key() -> bytes:
    path, source = _systemd_dbkey_file()
    if not path:
        raise RuntimeError(
            "systemd dbkey is not configured. Set CREDENTIALS_DIRECTORY with dbkey, or MAILHUB_DBKEY_FILE."
        )
    if not path.exists() or not path.is_file() or not os.access(path, os.R_OK):
        raise RuntimeError(f"systemd dbkey file is not readable: {path} ({source})")
    return _load_key_material(path.read_bytes())


def _write_systemd_key(key: bytes) -> None:
    dbkey_file = (os.environ.get("MAILHUB_DBKEY_FILE") or "").strip()
    if not dbkey_file:
        raise RuntimeError(
            "Cannot write systemd credential automatically without MAILHUB_DBKEY_FILE. "
            "Use systemd LoadCredential injection, or set MAILHUB_DBKEY_FILE to a writable file."
        )
    p = Path(os.path.expandvars(dbkey_file)).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    _ensure_private_dir(p.parent)
    encoded = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    p.write_text(f"{encoded}\n", encoding="utf-8")
    _ensure_private_file(p)


def _read_local_key(path: Path, *, state_dir: Path) -> bytes:
    if not path.exists():
        raise RuntimeError(f"local dbkey file not found: {path}")
    key = _load_key_material(path.read_bytes())
    _ensure_private_dir(state_dir)
    _ensure_private_file(path)
    return key


def _write_local_key(path: Path, key: bytes, *, state_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_private_dir(state_dir)
    _ensure_private_dir(path.parent)
    encoded = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    path.write_text(f"{encoded}\n", encoding="utf-8")
    _ensure_private_file(path)


def _read_keychain(*, keychain_account: str) -> bytes:
    if keyring is None:
        raise RuntimeError("keyring module is unavailable")
    raw = keyring.get_password(KEYCHAIN_SERVICE, keychain_account)
    if not raw:
        raise RuntimeError("dbkey not found in keychain")
    return _load_key_material(raw.encode("utf-8"))


def _write_keychain(key: bytes, *, keychain_account: str) -> None:
    if keyring is None:
        raise RuntimeError("keyring module is unavailable")
    encoded = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    keyring.set_password(KEYCHAIN_SERVICE, keychain_account, encoded)


def _probe_keyring_roundtrip() -> Tuple[bool, str]:
    if keyring is None:
        return False, "keyring module is unavailable"
    probe_account = f"probe.{getpass.getuser()}.{os.getpid()}"
    probe_value = base64.urlsafe_b64encode(secrets.token_bytes(18)).decode("ascii")
    try:
        keyring.set_password(KEYCHAIN_SERVICE, probe_account, probe_value)
        got = keyring.get_password(KEYCHAIN_SERVICE, probe_account)
        if got != probe_value:
            return False, "keyring read value mismatch"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, probe_account)
        except Exception:
            pass


def _probe_secret_tool_roundtrip() -> Tuple[bool, str]:
    if not shutil.which("secret-tool"):
        return False, "secret-tool not found"
    probe_name = f"mailhub-probe-{os.getpid()}-{secrets.token_hex(4)}"
    probe_value = base64.urlsafe_b64encode(secrets.token_bytes(12)).decode("ascii")
    ok, _, err = _run_cmd(
        ["secret-tool", "store", "--label=mailhub-dbkey-probe", "service", "mailhub", "probe", probe_name],
        input_text=probe_value,
    )
    if not ok:
        return False, err[:200]
    try:
        ok2, out2, err2 = _run_cmd(["secret-tool", "lookup", "service", "mailhub", "probe", probe_name])
        if not ok2:
            return False, err2[:200]
        got = out2.strip()
        if got != probe_value:
            return False, "secret-tool read value mismatch"
        return True, ""
    finally:
        _run_cmd(["secret-tool", "clear", "service", "mailhub", "probe", probe_name])


def _load_key_material(raw_bytes: bytes) -> bytes:
    raw = raw_bytes.strip()
    if not raw:
        raise RuntimeError("dbkey payload is empty")
    if len(raw) == 32:
        return bytes(raw)
    text = raw.decode("utf-8", errors="ignore").strip()
    if text.startswith("base64:"):
        text = text[7:].strip()
    if len(text) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        out = bytes.fromhex(text)
        if len(out) == 32:
            return out
    for candidate in (text, text + "=" * (-len(text) % 4)):
        try:
            out = base64.urlsafe_b64decode(candidate.encode("ascii"))
            if len(out) == 32:
                return out
        except Exception:
            pass
        try:
            out = base64.b64decode(candidate.encode("ascii"))
            if len(out) == 32:
                return out
        except Exception:
            pass
    if len(text.encode("utf-8")) == 32:
        return text.encode("utf-8")
    raise RuntimeError("dbkey must decode to exactly 32 bytes")


def _run_cmd(args: list[str], input_text: str = "") -> Tuple[bool, str, str]:
    try:
        cp = subprocess.run(
            args,
            input=input_text if input_text else None,
            text=True,
            capture_output=True,
            check=False,
        )
        return cp.returncode == 0, (cp.stdout or ""), (cp.stderr or "")
    except Exception as exc:
        return False, "", str(exc)


def _ensure_private_dir(path: Path) -> None:
    if os.name == "nt":
        return
    os.umask(0o077)
    try:
        if path.exists():
            os.chmod(path, 0o700)
    except Exception:
        pass


def _ensure_private_file(path: Path) -> None:
    if os.name == "nt":
        return
    os.umask(0o077)
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except Exception:
        pass
