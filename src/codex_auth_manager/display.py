from __future__ import annotations

import time

from .rpc import Snapshot, Window
from .selection import AccountScore, score_snapshot

BOLD = "\033[1m"
RESET = "\033[0m"


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


def _window_line(label: str, window: Window | None, *, now: float) -> str:
    if window is None or window.used_percent is None:
        return f"  {label:<5} -"
    used = max(0, min(100, int(window.used_percent)))
    remaining = 100 - used
    reset = _reset_text(window.resets_at, now=now)
    return f"  {label:<5} {_bar(used)} {used:>3}% used, {remaining:>3}% left · {reset}"


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
