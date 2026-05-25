from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .auth import AuthSummary, format_epoch, summarize
from .rpc import RpcError, Snapshot, query_quota, refresh_auth
from .storage import (
    StorageError,
    account_path,
    account_summary,
    active_auth_path,
    ensure_dirs,
    get_account,
    identify_active,
    list_accounts,
    read_auth,
    remove_account,
    switch_account,
    sync_saved_and_active,
    write_auth,
)

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        ensure_dirs()
        if args.command == "list":
            return cmd_list()
        if args.command == "current":
            return cmd_current()
        if args.command == "switch":
            return cmd_switch(args.name)
        if args.command == "login":
            return cmd_login(args.name, device_auth=not args.browser)
        if args.command == "quota":
            return cmd_quota(args.name)
        if args.command == "use-best":
            return cmd_use_best(args.accounts)
        if args.command == "remove":
            return cmd_remove(args.name)
    except (StorageError, RpcError) as exc:
        print(_color(f"Error: {exc}", RED), file=sys.stderr)
        return 1
    parser.print_help()
    return 1


def cmd_list() -> int:
    active = identify_active()
    rows = []
    for account in list_accounts():
        try:
            info = account_summary(account)
            rows.append([
                "*" if account.name == active else " ",
                account.name,
                info.email or "-",
                info.plan or "-",
                _expiry_cell(info.access_expired, info.access_exp),
                _token_status(info),
            ])
        except Exception as exc:
            rows.append([" ", account.name, "-", "-", "-", _color(str(exc), RED)])
    _table([" ", "name", "email", "plan", "access_exp", "status"], rows)
    return 0


def cmd_current() -> int:
    data = read_auth(active_auth_path())
    info = summarize(data)
    print(f"{BOLD}Current Codex account{RESET}")
    print(f"name:  {identify_active() or _color('untracked', YELLOW)}")
    print(f"path:  {active_auth_path()}")
    print(f"email: {info.email or '-'}")
    print(f"plan:  {info.plan or '-'}")
    print(f"access token: {_token_text(info.access_expired, info.access_exp)}")
    print(f"id token:     {_token_text(info.id_expired, info.id_exp)}")
    print(f"refresh:      {'present' if info.refresh_present else 'missing'}")
    return 0


def cmd_switch(name: str) -> int:
    path = switch_account(name)
    print(_color(f"Using {name}", GREEN))
    print(path)
    return 0


def cmd_login(name: str, *, device_auth: bool = True) -> int:
    command = ["codex", "login"]
    if device_auth:
        command.append("--device-auth")
    with tempfile.TemporaryDirectory(prefix="cxauth-login-") as tmp:
        login_home = Path(tmp) / ".codex"
        login_home.mkdir(mode=0o700)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(login_home)
        result = subprocess.run(command, check=False, env=env)
        if result.returncode != 0:
            return result.returncode
        data = read_auth(login_home / "auth.json")
    existed = account_path(name).exists()
    write_auth(account_path(name), data)
    if existed:
        print(_color(f"Warning: overwrote '{name}'", YELLOW))
    print(_color(f"Saved {name}", GREEN))
    print(account_path(name))
    return 0


def _refresh_account(name: str) -> None:
    account = get_account(name)
    data = read_auth(account.auth_path)
    refreshed = refresh_auth(data)
    sync_saved_and_active(name, refreshed)


def cmd_quota(name: str) -> int:
    snapshot = _quota_for(name)
    print(f"{BOLD}Quota for {name}{RESET}")
    print(f"email: {snapshot.email or '-'}")
    print(f"plan:  {snapshot.plan or '-'}")
    print(f"quota: {_quota_text(snapshot)}")
    return 0


def cmd_use_best(names: list[str]) -> int:
    candidates = names or [account.name for account in list_accounts()]
    best_name = ""
    best_score = 10**9
    best_snapshot: Snapshot | None = None
    skipped = 0
    for name in candidates:
        try:
            snapshot = _quota_for(name)
            primary = snapshot.default_limit.primary if snapshot.default_limit else None
            secondary = snapshot.default_limit.secondary if snapshot.default_limit else None
            p5h = primary.used_percent if primary and isinstance(primary.used_percent, int) else 999
            pwk = secondary.used_percent if secondary and isinstance(secondary.used_percent, int) else 999
            score = p5h * 1000 + pwk
            print(f"{name}: 5h={p5h}% used, week={pwk}% used")
            if score < best_score:
                best_name = name
                best_score = score
                best_snapshot = snapshot
        except Exception as exc:
            print(f"{name}: skipped ({exc})")
            skipped += 1
    if not best_name or best_snapshot is None:
        raise StorageError("No usable account found")
    switch_account(best_name)
    print(_color(f"Now using {best_name}: {_quota_text(best_snapshot)}", GREEN))
    if skipped:
        print(f"Skipped {skipped} account(s)")
    return 0


def cmd_remove(name: str) -> int:
    path = remove_account(name)
    print(_color(f"Removed {name}", GREEN))
    print(path)
    return 0


def _quota_for(name: str) -> Snapshot:
    account = get_account(name)
    data = read_auth(account.auth_path)
    snapshot = query_quota(data)
    sync_saved_and_active(name, snapshot.updated_auth)
    return snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cxauth", description="Manage Codex CLI OAuth accounts")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list")
    sub.add_parser("current")
    switch = sub.add_parser("switch")
    switch.add_argument("name")
    login = sub.add_parser("login")
    login.add_argument("name")
    login.add_argument("--browser", action="store_true", help="use the localhost browser login flow")
    quota = sub.add_parser("quota")
    quota.add_argument("name")
    use_best = sub.add_parser("use-best")
    use_best.add_argument("accounts", nargs="*")
    remove = sub.add_parser("remove", aliases=["rm"])
    remove.add_argument("name")
    return parser


def _quota_text(snapshot: Snapshot) -> str:
    limit = snapshot.default_limit
    if not limit:
        return "-"
    return f"5h={_window(limit.primary)}, week={_window(limit.secondary)}"


def _window(window: object | None) -> str:
    used = getattr(window, "used_percent", None)
    resets = getattr(window, "resets_at", None)
    if not isinstance(used, int):
        return "-"
    suffix = ""
    if isinstance(resets, int):
        suffix = f" resets {datetime.fromtimestamp(resets).strftime('%m-%d %H:%M')}"
    return f"{used}%{suffix}"


def _token_status(info: AuthSummary) -> str:
    if info.access_expired:
        return _color("expired", RED)
    return _color("ok", GREEN)


def _expiry_cell(expired: bool | None, epoch: int | None) -> str:
    if epoch is None:
        return "-"
    label = datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
    return _color(label, RED if expired else GREEN)


def _token_text(expired: bool | None, epoch: int | None) -> str:
    if expired is None:
        return format_epoch(epoch)
    return f"{_color('expired' if expired else 'ok', RED if expired else GREEN)} ({format_epoch(epoch)})"


def _table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    plain = [[_strip(cell) for cell in row] for row in rows]
    for row in plain:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    print("  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row, plain_row in zip(rows, plain):
        print("  ".join(row[idx].ljust(widths[idx] + len(row[idx]) - len(plain_row[idx])) for idx in range(len(row))))


def _strip(value: str) -> str:
    out = []
    escaped = False
    for char in value:
        if char == "\033":
            escaped = True
            continue
        if escaped:
            if char == "m":
                escaped = False
            continue
        out.append(char)
    return "".join(out)


def _color(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"
