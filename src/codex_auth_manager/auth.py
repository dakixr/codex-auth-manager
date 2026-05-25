from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PROFILE_KEY = "https://api.openai.com/profile"
AUTH_KEY = "https://api.openai.com/auth"


@dataclass(frozen=True)
class AuthSummary:
    email: str | None
    plan: str | None
    account_id: str | None
    auth_mode: str | None
    access_exp: int | None
    id_exp: int | None
    access_expired: bool | None
    id_expired: bool | None
    refresh_present: bool


def load_auth_json(raw: str, source: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source} must contain a JSON object")
    validate_auth(data, source)
    return data


def validate_auth(data: dict[str, Any], source: str = "auth.json") -> None:
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError(f"{source} is missing tokens object")
    for key in ("access_token", "id_token", "refresh_token"):
        if not isinstance(tokens.get(key), str) or not tokens.get(key):
            raise ValueError(f"{source} is missing tokens.{key}")


def summarize(data: dict[str, Any]) -> AuthSummary:
    tokens = _dict_or_empty(data.get("tokens"))
    access = _jwt_payload(tokens.get("access_token"))
    ident = _jwt_payload(tokens.get("id_token"))
    profile = _dict_or_empty(access.get(PROFILE_KEY))
    auth = _dict_or_empty(access.get(AUTH_KEY))
    access_exp = _int_or_none(access.get("exp"))
    id_exp = _int_or_none(ident.get("exp"))
    now = int(time.time())
    return AuthSummary(
        email=_first_str(ident.get("email"), profile.get("email")),
        plan=_first_str(auth.get("chatgpt_plan_type")),
        account_id=_first_str(tokens.get("account_id")),
        auth_mode=_first_str(data.get("auth_mode")),
        access_exp=access_exp,
        id_exp=id_exp,
        access_expired=(access_exp <= now) if access_exp is not None else None,
        id_expired=(id_exp <= now) if id_exp is not None else None,
        refresh_present=bool(tokens.get("refresh_token")),
    )


def same_account(left: AuthSummary, right: AuthSummary) -> bool:
    if left.account_id and right.account_id:
        return left.account_id == right.account_id
    if left.email and right.email:
        return left.email == right.email
    return False


def format_epoch(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _jwt_payload(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
