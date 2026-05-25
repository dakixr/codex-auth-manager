from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .storage import read_auth, write_auth


class RpcError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    used_percent: int | None
    resets_at: int | None
    duration_mins: int | None


@dataclass(frozen=True)
class RateLimit:
    limit_id: str | None
    limit_name: str | None
    primary: Window | None
    secondary: Window | None
    plan: str | None


@dataclass(frozen=True)
class Snapshot:
    email: str | None
    plan: str | None
    auth_method: str | None
    default_limit: RateLimit | None
    limits: dict[str, RateLimit]
    updated_auth: dict[str, Any]


def refresh_auth(auth_data: dict[str, Any]) -> dict[str, Any]:
    return _with_temp_codex(auth_data, _refresh)


def query_quota(auth_data: dict[str, Any]) -> Snapshot:
    return _with_temp_codex(auth_data, _quota)


def _refresh(ws: Any, auth_path: Path) -> dict[str, Any]:
    _send(ws, 1, "account/read", {"refreshToken": True})
    _raise_for_error(_recv(ws, 1))
    return read_auth(auth_path)


def _quota(ws: Any, auth_path: Path) -> Snapshot:
    _send(ws, 1, "account/read", {"refreshToken": True})
    account_resp = _recv(ws, 1)
    _raise_for_error(account_resp)
    _send(ws, 2, "getAuthStatus", {})
    auth_resp = _recv(ws, 2)
    _raise_for_error(auth_resp)
    _send(ws, 3, "account/rateLimits/read", {})
    rate_resp = _recv(ws, 3)
    _raise_for_error(rate_resp)

    account = (account_resp.get("result") or {}).get("account") or {}
    auth = auth_resp.get("result") or {}
    rates = rate_resp.get("result") or {}
    default_limit = _parse_limit(rates.get("rateLimits"))
    by_id_raw = rates.get("rateLimitsByLimitId")
    limits: dict[str, RateLimit] = {}
    if isinstance(by_id_raw, dict):
        for key, value in by_id_raw.items():
            parsed = _parse_limit(value)
            if parsed:
                limits[key] = parsed
    if default_limit is None and "codex" in limits:
        default_limit = limits["codex"]
    return Snapshot(
        email=_str(account.get("email")),
        plan=_str(account.get("planType")) or (default_limit.plan if default_limit else None),
        auth_method=_str(auth.get("authMethod")),
        default_limit=default_limit,
        limits=limits,
        updated_auth=read_auth(auth_path),
    )


def _with_temp_codex(auth_data: dict[str, Any], operation: Callable[[Any, Path], Any]) -> Any:
    codex = shutil.which("codex")
    if not codex:
        raise RpcError("codex is not on PATH")
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise RpcError("websockets is required") from exc

    with tempfile.TemporaryDirectory(prefix="cxauth-") as tmp:
        root = Path(tmp)
        home = root / ".codex"
        auth_path = home / "auth.json"
        write_auth(auth_path, auth_data)
        port = _free_port()
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        proc = subprocess.Popen(
            [
                codex,
                "app-server",
                "-c",
                'cli_auth_credentials_store="file"',
                "--listen",
                f"ws://127.0.0.1:{port}",
            ],
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        result: Any = None
        try:
            _wait(port, 30)
            with connect(f"ws://127.0.0.1:{port}", open_timeout=10, close_timeout=1) as ws:
                _send(ws, 0, "initialize", {"clientInfo": {"name": "cxauth", "version": "0.1.0"}})
                _raise_for_error(_recv(ws, 0))
                _notify(ws, "initialized", {})
                result = operation(ws, auth_path)
        except Exception as exc:
            stderr = _stderr(proc)
            message = f"{exc}\n{stderr}".strip()
            raise RpcError(_clean_rpc_error(message)) from exc
        finally:
            _terminate(proc)
        stderr = _stderr(proc)
        token_error = _token_error(stderr)
        if token_error:
            raise RpcError(token_error)
        return result


def _send(ws: Any, request_id: int, method: str, params: dict[str, Any]) -> None:
    ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))


def _notify(ws: Any, method: str, params: dict[str, Any]) -> None:
    ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))


def _recv(ws: Any, request_id: int) -> dict[str, Any]:
    while True:
        payload = json.loads(ws.recv())
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return payload


def _raise_for_error(resp: dict[str, Any]) -> None:
    if "error" in resp:
        raise RpcError(f"JSON-RPC error: {resp['error']}")


def _parse_limit(raw: Any) -> RateLimit | None:
    if not isinstance(raw, dict):
        return None
    return RateLimit(
        limit_id=_str(raw.get("limitId")),
        limit_name=_str(raw.get("limitName")),
        primary=_parse_window(raw.get("primary")),
        secondary=_parse_window(raw.get("secondary")),
        plan=_str(raw.get("planType")),
    )


def _parse_window(raw: Any) -> Window | None:
    if not isinstance(raw, dict):
        return None
    return Window(
        used_percent=raw.get("usedPercent") if isinstance(raw.get("usedPercent"), int) else None,
        resets_at=raw.get("resetsAt") if isinstance(raw.get("resetsAt"), int) else None,
        duration_mins=raw.get("windowDurationMins") if isinstance(raw.get("windowDurationMins"), int) else None,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for codex app-server on {port}")


def _stderr(proc: subprocess.Popen[str]) -> str:
    if proc.stderr is None or proc.poll() is None:
        return ""
    try:
        return proc.stderr.read().strip()
    except Exception:
        return ""


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean_rpc_error(message: str) -> str:
    token_error = _token_error(message)
    if token_error:
        return token_error
    return message


def _token_error(message: str) -> str | None:
    for code in ("token_revoked", "token_invalidated", "token_expired", "refresh_token_reused"):
        if code in message:
            return code
    return None
