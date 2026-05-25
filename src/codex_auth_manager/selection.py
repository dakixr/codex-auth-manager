from __future__ import annotations

import time
from dataclasses import dataclass

from .rpc import Snapshot, Window


@dataclass(frozen=True)
class Pace:
    used_percent: float
    expected_used_percent: float
    delta_percent: float
    will_last_to_reset: bool
    eta_seconds: float | None
    resets_in_seconds: float

    @property
    def label(self) -> str:
        delta = round(abs(self.delta_percent))
        if abs(self.delta_percent) <= 2:
            left = "on pace"
        elif self.delta_percent > 0:
            left = f"{delta}% in deficit"
        else:
            left = f"{delta}% in reserve"

        if self.will_last_to_reset:
            return f"{left}, lasts until reset"
        if self.eta_seconds is not None:
            return f"{left}, projected empty in {_duration(self.eta_seconds)}"
        return left


@dataclass(frozen=True)
class AccountScore:
    score: float
    session_used: int | None
    weekly_used: int | None
    session_pace: Pace | None
    weekly_pace: Pace | None
    rejected: bool
    reject_reason: str | None

    @property
    def summary(self) -> str:
        parts = [
            f"5h={_percent(self.session_used)}",
            self.session_pace.label if self.session_pace else "5h pace unavailable",
            f"week={_percent(self.weekly_used)}",
            self.weekly_pace.label if self.weekly_pace else "weekly pace unavailable",
        ]
        if self.rejected and self.reject_reason:
            parts.append(f"rejected={self.reject_reason}")
        else:
            parts.append(f"score={self.score:.1f}")
        return "; ".join(parts)


def score_snapshot(snapshot: Snapshot, *, now: float | None = None) -> AccountScore:
    now = time.time() if now is None else now
    limit = snapshot.default_limit
    session = limit.primary if limit else None
    weekly = limit.secondary if limit else None
    session_used = session.used_percent if session else None
    weekly_used = weekly.used_percent if weekly else None
    session_pace = pace_for_window(session, now=now, default_window_minutes=300)
    weekly_pace = pace_for_window(weekly, now=now, default_window_minutes=10080)
    reject_reason = _reject_reason(session_used, weekly_used, session_pace)

    if reject_reason:
        return AccountScore(
            score=float("inf"),
            session_used=session_used,
            weekly_used=weekly_used,
            session_pace=session_pace,
            weekly_pace=weekly_pace,
            rejected=True,
            reject_reason=reject_reason,
        )

    score = 0.0
    score += float(session_used if session_used is not None else 999)
    score += float(weekly_used if weekly_used is not None else 999) * 2
    score += _pace_pressure(session_pace, deficit_weight=1.5, reserve_weight=0.5)
    score += _pace_pressure(weekly_pace, deficit_weight=3.0, reserve_weight=1.0)
    return AccountScore(
        score=score,
        session_used=session_used,
        weekly_used=weekly_used,
        session_pace=session_pace,
        weekly_pace=weekly_pace,
        rejected=False,
        reject_reason=None,
    )


def pace_for_window(window: Window | None, *, now: float | None = None, default_window_minutes: int) -> Pace | None:
    if window is None or window.used_percent is None or window.resets_at is None:
        return None
    window_minutes = window.duration_mins or default_window_minutes
    if window_minutes <= 0:
        return None
    now = time.time() if now is None else now
    duration = float(window_minutes * 60)
    resets_in = float(window.resets_at) - now
    if resets_in <= 0 or resets_in > duration:
        return None
    elapsed = max(0.0, min(duration, duration - resets_in))
    actual = _clamp(float(window.used_percent), 0.0, 100.0)
    if elapsed == 0 and actual > 0:
        return None
    expected = _clamp((elapsed / duration) * 100.0, 0.0, 100.0)
    delta = actual - expected
    eta_seconds: float | None = None
    will_last = False
    if elapsed > 0 and actual > 0:
        rate = actual / elapsed
        remaining = max(0.0, 100.0 - actual)
        candidate = remaining / rate if rate > 0 else None
        if candidate is not None:
            if candidate >= resets_in:
                will_last = True
            else:
                eta_seconds = candidate
    elif elapsed > 0 and actual == 0:
        will_last = True
    return Pace(
        used_percent=actual,
        expected_used_percent=expected,
        delta_percent=delta,
        will_last_to_reset=will_last,
        eta_seconds=eta_seconds,
        resets_in_seconds=resets_in,
    )


def _reject_reason(session_used: int | None, weekly_used: int | None, session_pace: Pace | None) -> str | None:
    if weekly_used is not None and weekly_used >= 98:
        return "weekly >= 98%"
    if session_used is not None and session_used >= 98:
        if session_pace is None or session_pace.resets_in_seconds > 10 * 60:
            return "5h >= 98%"
    return None


def _pace_pressure(pace: Pace | None, *, deficit_weight: float, reserve_weight: float) -> float:
    if pace is None:
        return 0.0
    if pace.delta_percent > 0:
        return pace.delta_percent * deficit_weight
    return pace.delta_percent * reserve_weight


def _percent(value: int | None) -> str:
    return f"{value}%" if value is not None else "-"


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
