from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from .auth import AuthSummary, format_epoch, summarize
from .display import quota_block
from .engine import fetch_quota, fetch_quotas
from .rpc import RpcError, Snapshot
from .selection import score_snapshot
from .storage import (
    StorageError,
    account_path,
    account_summary,
    active_auth_path,
    ensure_dirs,
    identify_active,
    list_accounts,
    read_auth,
    remove_account,
    switch_account,
    write_auth,
)

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
app = typer.Typer(help="Manage Codex CLI OAuth accounts", no_args_is_help=True)


def main(argv: list[str] | None = None) -> int:
    app(args=argv, prog_name="cxauth")
    return 0


@app.command("list")
def list_command() -> None:
    """List saved accounts without touching live auth."""
    with _handle_errors():
        cmd_list()


@app.command("current")
def current_command() -> None:
    """Show the active Codex auth identity."""
    with _handle_errors():
        cmd_current()


@app.command("switch")
def switch_command(name: Annotated[str, typer.Argument(help="Saved account name")]) -> None:
    """Switch ~/.codex/auth.json to a saved account."""
    with _handle_errors():
        cmd_switch(name)


@app.command("login")
def login_command(
    name: Annotated[str, typer.Argument(help="Saved account name")],
    browser: Annotated[bool, typer.Option(help="Use the localhost browser login flow")] = False,
) -> None:
    """Login with Codex in an isolated CODEX_HOME and save the result."""
    with _handle_errors():
        raise typer.Exit(cmd_login(name, device_auth=not browser))


@app.command("quota")
def quota_command(
    name: Annotated[str | None, typer.Argument(help="Saved account name. Omit to check all accounts.")] = None,
) -> None:
    """Print live quota for one account, or all accounts when NAME is omitted."""
    with _handle_errors():
        raise typer.Exit(cmd_quota(name))


@app.command("use-best")
def use_best_command(accounts: Annotated[list[str] | None, typer.Argument(help="Optional account names")] = None) -> None:
    """Switch to the best account using pace-aware quota scoring."""
    with _handle_errors():
        cmd_use_best(accounts or [])


@app.command("remove")
def remove_command(name: Annotated[str, typer.Argument(help="Saved account name")]) -> None:
    """Remove a saved account."""
    with _handle_errors():
        cmd_remove(name)


@app.command("export")
def export_command(
    output: Annotated[str | None, typer.Option("-o", "--output", help="Write exported auth JSON to this file")] = None,
) -> None:
    """Export every saved auth JSON. Prints credentials to stdout without -o."""
    with _handle_errors():
        cmd_export(output)


@app.command("import")
def import_command(path: Annotated[Path, typer.Argument(help="JSON file created by cxauth export")]) -> None:
    """Import saved auth JSON accounts from an export file."""
    with _handle_errors():
        cmd_import(path)


@contextmanager
def _handle_errors():
    try:
        ensure_dirs()
        yield
    except (StorageError, RpcError) as exc:
        print(_color(f"Error: {exc}", RED), file=sys.stderr)
        raise typer.Exit(1) from exc


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


def cmd_quota(name: str | None) -> int:
    active = identify_active()
    if name is not None:
        _print_quota(name, fetch_quota(name, sync_active=True), active=active)
        return 0

    successes = 0
    skipped = 0
    for result in fetch_quotas([account.name for account in list_accounts()]):
        if result.error is not None or result.snapshot is None:
            exc = result.error or StorageError("missing quota snapshot")
            print(f"{result.name}: skipped ({exc})")
            skipped += 1
            continue
        if active == result.name:
            switch_account(result.name)
        if successes:
            print("")
        _print_quota(result.name, result.snapshot, active=active)
        successes += 1
    if skipped:
        print(f"Skipped {skipped} account(s)")
    if successes == 0:
        raise StorageError("No usable account found")
    return 0


def cmd_use_best(names: list[str]) -> int:
    candidates = names or [account.name for account in list_accounts()]
    best_name = ""
    best_score = float("inf")
    best_snapshot: Snapshot | None = None
    skipped = 0
    for result in fetch_quotas(candidates):
        if result.error is not None or result.snapshot is None:
            exc = result.error or StorageError("missing quota snapshot")
            print(f"{result.name}: skipped ({exc})")
            skipped += 1
            continue

        account_score = score_snapshot(result.snapshot)
        print(f"{result.name}: {account_score.summary}")
        if account_score.rejected:
            skipped += 1
            continue
        if account_score.score < best_score:
            best_name = result.name
            best_score = account_score.score
            best_snapshot = result.snapshot
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


def cmd_export(output: str | None) -> int:
    payload = {
        account.name: read_auth(account.auth_path)
        for account in list_accounts()
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
        return 0

    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
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
    print(path)
    return 0


def cmd_import(path: Path) -> int:
    source = path.expanduser()
    try:
        raw = json.loads(source.read_text())
    except FileNotFoundError as exc:
        raise StorageError(f"Missing import file: {source}") from exc
    except json.JSONDecodeError as exc:
        raise StorageError(f"Invalid JSON in {source}: {exc}") from exc

    if not isinstance(raw, dict):
        raise StorageError("Import file must contain a JSON object keyed by account name")

    imported = 0
    overwritten = 0
    for name, data in raw.items():
        if not isinstance(name, str):
            raise StorageError("Import account names must be strings")
        if not isinstance(data, dict):
            raise StorageError(f"Import entry '{name}' must be an auth JSON object")
        target = account_path(name)
        existed = target.exists()
        write_auth(target, data)
        imported += 1
        if existed:
            overwritten += 1
            print(_color(f"Warning: overwrote '{name}'", YELLOW))

    print(_color(f"Imported {imported} account(s)", GREEN))
    if overwritten:
        print(f"Overwrote {overwritten} account(s)")
    return 0


def _print_quota(name: str, snapshot: Snapshot, *, active: str | None = None) -> None:
    marker = " *" if name == active else ""
    print(quota_block(f"{name}{marker}", snapshot))


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
