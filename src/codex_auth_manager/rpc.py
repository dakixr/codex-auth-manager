from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .auth import summarize


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
    rate_limit_reset_credits: int | None = None
    rate_limit_reset_credit_expirations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsumeResetResult:
    code: str
    windows_reset: int
    updated_auth: dict[str, Any]


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CONSUME_RESET_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_ERRORS = {
    "unauthorized",
    "forbidden",
    "token_expired",
    "refresh_token_expired",
    "token_invalidated",
    "token_revoked",
}


def refresh_auth(auth_data: dict[str, Any]) -> dict[str, Any]:
    return _refresh_auth(auth_data)


def query_quota(auth_data: dict[str, Any]) -> Snapshot:
    data = dict(auth_data)
    summary = summarize(data)
    if summary.access_expired:
        data = _refresh_auth(data)

    try:
        usage = _fetch_usage(data)
    except RpcError as exc:
        if str(exc) not in TOKEN_ERRORS:
            raise
        data = _refresh_auth(data)
        usage = _fetch_usage(data)

    try:
        reset_credits = _fetch_reset_credits(data)
    except RpcError:
        reset_credits = None

    return _snapshot_from_usage(usage, data, reset_credits=reset_credits)


def consume_rate_limit_reset_credit(auth_data: dict[str, Any], redeem_request_id: str) -> ConsumeResetResult:
    if not redeem_request_id:
        raise RpcError("redeem request id must not be empty")
    data = dict(auth_data)
    summary = summarize(data)
    if summary.access_expired:
        data = _refresh_auth(data)

    try:
        response = _consume_reset(data, redeem_request_id)
    except RpcError as exc:
        if str(exc) not in TOKEN_ERRORS:
            raise
        data = _refresh_auth(data)
        response = _consume_reset(data, redeem_request_id)

    code = _str(response.get("code"))
    if code not in {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}:
        raise RpcError("Invalid response from Codex reset API")
    windows_reset = response.get("windows_reset")
    return ConsumeResetResult(
        code=code,
        windows_reset=windows_reset if isinstance(windows_reset, int) else 0,
        updated_auth=data,
    )


def _fetch_usage(auth_data: dict[str, Any]) -> dict[str, Any]:
    raw = _request_json("GET", USAGE_URL, headers=_auth_headers(auth_data))
    if not isinstance(raw, dict):
        raise RpcError("Invalid response from Codex usage API")
    return raw


def _fetch_reset_credits(auth_data: dict[str, Any]) -> dict[str, Any]:
    raw = _request_json(
        "GET",
        RESET_CREDITS_URL,
        headers={**_auth_headers(auth_data), "OpenAI-Beta": "codex-1", "originator": "Codex Desktop"},
    )
    if not isinstance(raw, dict):
        raise RpcError("Invalid response from Codex reset credits API")
    return raw


def _consume_reset(auth_data: dict[str, Any], redeem_request_id: str) -> dict[str, Any]:
    raw = _request_json(
        "POST",
        CONSUME_RESET_URL,
        headers={**_auth_headers(auth_data), "Content-Type": "application/json"},
        body={"redeem_request_id": redeem_request_id},
    )
    if not isinstance(raw, dict):
        raise RpcError("Invalid response from Codex reset API")
    return raw


def _auth_headers(auth_data: dict[str, Any]) -> dict[str, str]:
    tokens = _tokens(auth_data)
    access_token = _str(tokens.get("access_token"))
    if not access_token:
        raise RpcError("missing access token")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "cxauth",
    }
    account_id = _str(tokens.get("account_id"))
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _refresh_auth(auth_data: dict[str, Any]) -> dict[str, Any]:
    tokens = _tokens(auth_data)
    refresh_token = _str(tokens.get("refresh_token"))
    if not refresh_token:
        raise RpcError("missing refresh token")

    body = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }
    raw = _request_json(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "cxauth"},
        body=body,
    )
    if not isinstance(raw, dict):
        raise RpcError("Invalid refresh response")

    updated = dict(auth_data)
    updated_tokens = dict(tokens)
    for key in ("access_token", "refresh_token", "id_token"):
        value = _str(raw.get(key))
        if value:
            updated_tokens[key] = value
    updated["tokens"] = updated_tokens
    return updated


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> Any:
    encoded = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        message = _http_error_message(exc)
        if exc.code == 401:
            raise RpcError(_clean_rpc_error(message) if _token_error(message) else "unauthorized") from exc
        if exc.code == 403:
            raise RpcError(_clean_rpc_error(message) if _token_error(message) else "forbidden") from exc
        raise RpcError(f"{method} {url} failed: {exc.code}; body={message}") from exc
    except urllib.error.URLError as exc:
        raise RpcError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RpcError(f"Invalid JSON from {url}: {exc}") from exc


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode().strip()
    except Exception:
        return str(exc)


def _snapshot_from_usage(
    usage: dict[str, Any],
    auth_data: dict[str, Any],
    *,
    reset_credits: dict[str, Any] | None = None,
) -> Snapshot:
    summary = summarize(auth_data)
    plan = _str(usage.get("plan_type")) or summary.plan
    default_limit = _parse_limit(usage.get("rate_limit"), limit_id="codex", plan=plan)
    limits: dict[str, RateLimit] = {}
    if default_limit is not None:
        limits["codex"] = default_limit

    extras = usage.get("additional_rate_limits")
    if isinstance(extras, list):
        for idx, item in enumerate(extras):
            if not isinstance(item, dict):
                continue
            limit = _parse_limit(
                item.get("rate_limit"),
                limit_id=_str(item.get("metered_feature")) or f"additional-{idx + 1}",
                limit_name=_str(item.get("limit_name")),
                plan=plan,
            )
            if limit and limit.limit_id:
                limits[limit.limit_id] = limit

    reset_credit_count = _reset_credits(reset_credits) if reset_credits is not None else None
    if reset_credit_count is None:
        reset_credit_count = _reset_credits(usage)

    return Snapshot(
        email=summary.email,
        plan=plan,
        auth_method=summary.auth_mode,
        default_limit=default_limit,
        limits=limits,
        updated_auth=auth_data,
        rate_limit_reset_credits=reset_credit_count,
        rate_limit_reset_credit_expirations=_reset_credit_expirations(reset_credits),
    )


def _reset_credits(usage: dict[str, Any]) -> int | None:
    raw = usage.get("rate_limit_reset_credits")
    if not isinstance(raw, dict):
        raw = usage
    available = raw.get("available_count")
    return available if isinstance(available, int) else None


def _reset_credit_expirations(reset_credits: dict[str, Any] | None) -> tuple[str, ...]:
    if reset_credits is None:
        return ()
    raw_credits = reset_credits.get("credits")
    if not isinstance(raw_credits, list):
        return ()
    expirations: list[str] = []
    for credit in raw_credits:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        expires_at = _str(credit.get("expires_at"))
        if expires_at:
            expirations.append(expires_at)
    return tuple(sorted(expirations))


def _parse_limit(
    raw: Any,
    *,
    limit_id: str | None,
    plan: str | None,
    limit_name: str | None = None,
) -> RateLimit | None:
    if not isinstance(raw, dict):
        return None
    primary = _parse_window(raw.get("primary_window"))
    secondary = _parse_window(raw.get("secondary_window"))
    primary, secondary = _normalize_windows(primary, secondary)
    if primary is None and secondary is None:
        return None
    return RateLimit(
        limit_id=limit_id,
        limit_name=limit_name,
        primary=primary,
        secondary=secondary,
        plan=plan,
    )


def _parse_window(raw: Any) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    reset = raw.get("reset_at")
    seconds = raw.get("limit_window_seconds")
    if not isinstance(used, int) or not isinstance(reset, int) or not isinstance(seconds, int):
        return None
    return Window(
        used_percent=used,
        resets_at=reset,
        duration_mins=seconds // 60,
    )


def _normalize_windows(primary: Window | None, secondary: Window | None) -> tuple[Window | None, Window | None]:
    if primary is not None and secondary is not None:
        primary_role = _window_role(primary)
        secondary_role = _window_role(secondary)
        if primary_role == "weekly" and secondary_role in {"session", "unknown"}:
            return secondary, primary
        return primary, secondary
    if primary is not None:
        if _window_role(primary) == "weekly":
            return None, primary
        return primary, None
    if secondary is not None:
        if _window_role(secondary) == "weekly":
            return None, secondary
        return secondary, None
    return None, None


def _window_role(window: Window) -> str:
    if window.duration_mins == 300:
        return "session"
    if window.duration_mins == 10080:
        return "weekly"
    return "unknown"


def _tokens(auth_data: dict[str, Any]) -> dict[str, Any]:
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        raise RpcError("missing tokens")
    return tokens


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean_rpc_error(message: str) -> str:
    token_error = _token_error(message)
    if token_error:
        return token_error
    return message


def _token_error(message: str) -> str | None:
    for code in ("token_revoked", "token_invalidated", "token_expired", "refresh_token_expired", "refresh_token_reused"):
        if code in message:
            return code
    if "invalid_grant" in message:
        return "token_invalidated"
    return None
