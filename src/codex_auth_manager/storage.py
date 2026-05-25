from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import AuthSummary, load_auth_json, same_account, summarize, validate_auth

VAULT_ENV = "CXAUTH_VAULT"
CODEX_HOME_ENV = "CXAUTH_CODEX_HOME"
DEFAULT_VAULT = Path.home() / ".codex-auth"
DEFAULT_CODEX_HOME = Path.home() / ".codex"


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    auth_path: Path


def vault_dir() -> Path:
    return Path(os.environ.get(VAULT_ENV, str(DEFAULT_VAULT))).expanduser()


def accounts_dir() -> Path:
    return vault_dir() / "accounts"


def codex_home() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV, str(DEFAULT_CODEX_HOME))).expanduser()


def active_auth_path() -> Path:
    return codex_home() / "auth.json"


def ensure_dirs() -> None:
    _secure_dir(vault_dir())
    _secure_dir(accounts_dir())


def account_path(name: str) -> Path:
    return accounts_dir() / _normalize_name(name) / "auth.json"


def list_accounts() -> list[Account]:
    ensure_dirs()
    accounts: list[Account] = []
    for path in sorted(accounts_dir().glob("*/auth.json")):
        accounts.append(Account(path.parent.name, path))
    return accounts


def get_account(name: str) -> Account:
    path = account_path(name)
    if not path.exists():
        raise StorageError(f"No account named '{name}'")
    return Account(path.parent.name, path)


def read_auth(path: Path) -> dict[str, Any]:
    try:
        return load_auth_json(path.read_text(), str(path))
    except FileNotFoundError as exc:
        raise StorageError(f"Missing auth file: {path}") from exc
    except ValueError as exc:
        raise StorageError(str(exc)) from exc


def write_auth(path: Path, data: dict[str, Any]) -> None:
    validate_auth(data, str(path))
    _secure_dir(path.parent)
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(rendered)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def switch_account(name: str) -> Path:
    account = get_account(name)
    data = read_auth(account.auth_path)
    path = active_auth_path()
    write_auth(path, data)
    return path


def remove_account(name: str) -> Path:
    account = get_account(name)
    active = identify_active()
    if active == account.name:
        raise StorageError(f"Cannot remove active account '{name}'")
    shutil.rmtree(account.auth_path.parent)
    return account.auth_path.parent


def identify_active() -> str | None:
    active_path = active_auth_path()
    if not active_path.exists():
        return None
    try:
        active_data = read_auth(active_path)
    except StorageError:
        return None
    active_summary = summarize(active_data)
    active_hash = _file_bytes(active_path)
    accounts = list_accounts()
    for account in accounts:
        if _file_bytes(account.auth_path) == active_hash:
            return account.name
    for account in accounts:
        try:
            info = summarize(read_auth(account.auth_path))
        except StorageError:
            continue
        if same_account(active_summary, info):
            return account.name
    return None


def sync_saved_and_active(name: str, data: dict[str, Any]) -> list[Path]:
    account = get_account(name)
    updated = [account.auth_path]
    write_auth(account.auth_path, data)
    if _active_matches(data):
        write_auth(active_auth_path(), data)
        updated.append(active_auth_path())
    return updated


def account_summary(account: Account) -> AuthSummary:
    return summarize(read_auth(account.auth_path))


def _active_matches(data: dict[str, Any]) -> bool:
    path = active_auth_path()
    if not path.exists():
        return False
    try:
        active = summarize(read_auth(path))
    except StorageError:
        return False
    return same_account(active, summarize(data))


def _normalize_name(name: str) -> str:
    normalized = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise StorageError("Account name must match [A-Za-z0-9._-]+")
    return normalized


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
