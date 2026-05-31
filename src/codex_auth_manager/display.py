from __future__ import annotations

import time
from dataclasses import dataclass

from .rpc import Snapshot, Window
from .selection import AccountScore, pace_for_window, score_snapshot

BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass(frozen=True)
class BlendedWindow:
    used_percent: int
    remaining_percent: int
    resets_at: int | None
    duration_mins: int | None


def quota_block(name: str, snapshot: Snapshot, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    score = score_snapshot(snapshot, now=now)
    lines = [
        f"{BOLD}{name}{RESET}",
        f"  email  {snapshot.email or '-'}",
        f"  plan   {snapshot.plan or '-'}",
    ]
    limit = snapshot.default_limit
    if limit is None:
        lines.append("  quota  -")
    else:
        lines.append(_window_line("5h", limit.primary, now=now))
        lines.append(_window_line("week", limit.secondary, now=now))
    lines.extend(_pace_lines(score))
    return "\n".join(lines)


def blended_quota_block(snapshots: list[Snapshot], *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    primary = _blended_window(snapshots, "primary")
    secondary = _blended_window(snapshots, "secondary")
    lines = [
        f"{BOLD}blended total{RESET}",
        "  email  -",
        "  plan   synthetic merged remaining",
    ]
    if primary is None and secondary is None:
        lines.append("  quota  -")
    else:
        lines.append(_blended_window_line("5h", primary, now=now))
        lines.append(_blended_window_line("week", secondary, now=now))
    lines.extend(_blended_pace_lines(primary, secondary, now=now))
    return "\n".join(lines)


def _window_line(label: str, window: Window | None, *, now: float) -> str:
    if window is None or window.used_percent is None:
        return f"  {label:<5} -"
    used = max(0, min(100, int(window.used_percent)))
    remaining = 100 - used
    reset = _reset_text(window.resets_at, now=now)
    return f"  {label:<5} {_bar(used)} {used:>3}% used, {remaining:>3}% left · {reset}"


def _blended_window_line(label: str, window: BlendedWindow | None, *, now: float) -> str:
    if window is None:
        return f"  {label:<5} -"
    reset = _reset_text(window.resets_at, now=now)
    return f"  {label:<5} {_bar(window.used_percent)} {window.used_percent:>3}% used, {window.remaining_percent:>3}% left · {reset}"


def _blended_window(snapshots: list[Snapshot], kind: str) -> BlendedWindow | None:
    windows = [_limit_window(snapshot, kind) for snapshot in snapshots]
    windows = [window for window in windows if window is not None and window.used_percent is not None]
    if not windows:
        return None
    total_used = sum(max(0, min(100, int(window.used_percent))) for window in windows)
    used_percent = max(0, min(100, int(round(total_used / len(windows)))))
    resets_at = min((window.resets_at for window in windows if window.resets_at is not None), default=None)
    duration_mins = min((window.duration_mins for window in windows if window.duration_mins is not None), default=None)
    return BlendedWindow(
        used_percent=used_percent,
        remaining_percent=100 - used_percent,
        resets_at=resets_at,
        duration_mins=duration_mins,
    )


def _blended_pace_lines(primary: BlendedWindow | None, secondary: BlendedWindow | None, *, now: float) -> list[str]:
    primary_pace = _blended_pace(primary, now=now, default_window_minutes=300)
    secondary_pace = _blended_pace(secondary, now=now, default_window_minutes=10080)
    lines = []
    if primary_pace:
        lines.append(f"  pace  5h:   {primary_pace.label}")
    else:
        lines.append("  pace  5h:   unavailable")
    if secondary_pace:
        lines.append(f"        week: {secondary_pace.label}")
    else:
        lines.append("        week: unavailable")
    return lines


def _blended_pace(window: BlendedWindow | None, *, now: float, default_window_minutes: int):
    if window is None:
        return None
    return pace_for_window(
        Window(used_percent=window.used_percent, resets_at=window.resets_at, duration_mins=window.duration_mins),
        now=now,
        default_window_minutes=default_window_minutes,
    )


def _limit_window(snapshot: Snapshot, kind: str) -> Window | None:
    limit = snapshot.default_limit
    if limit is None:
        return None
    return limit.primary if kind == "primary" else limit.secondary


def _pace_lines(score: AccountScore) -> list[str]:
    lines = []
    if score.session_pace:
        lines.append(f"  pace  5h:   {score.session_pace.label}")
    else:
        lines.append("  pace  5h:   unavailable")
    if score.weekly_pace:
        lines.append(f"        week: {score.weekly_pace.label}")
    else:
        lines.append("        week: unavailable")
    if score.rejected and score.reject_reason:
        lines.append(f"  pick   rejected ({score.reject_reason})")
    else:
        lines.append(f"  pick   score {score.score:.1f}")
    return lines


def _bar(used: int, *, width: int = 20) -> str:
    filled = round((used / 100) * width)
    filled = max(0, min(width, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _reset_text(resets_at: int | None, *, now: float) -> str:
    if resets_at is None:
        return "reset unknown"
    delta = float(resets_at) - now
    if delta <= 0:
        return "reset due"
    return f"resets in {_duration(delta)}"


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "now" if seconds <= 1 else f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = round(minutes / 60)
    if hours < 48:
        return f"{hours}h"
    days = round(hours / 24)
    return f"{days}d"
