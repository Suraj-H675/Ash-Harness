"""Validated one-shot, interval, and timezone-aware cron calculation."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from ash.automation.models import ScheduleSpec


MIN_INTERVAL_SECONDS = 10.0
MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60.0
_DURATION_PART = re.compile(r"(?P<number>\d+(?:\.\d+)?)(?P<unit>[smhdw])")
_DURATION_MULTIPLIERS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str, *, label: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty ISO 8601 timestamp")
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_duration(value: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("interval must be a duration such as 30m, 2h, or 1d")
    normalized = value.strip().casefold().replace(" ", "")
    position = 0
    seconds = 0.0
    for match in _DURATION_PART.finditer(normalized):
        if match.start() != position:
            raise ValueError("interval must be a duration such as 30m, 2h, or 1d")
        seconds += (
            float(match.group("number")) * _DURATION_MULTIPLIERS[match.group("unit")]
        )
        position = match.end()
    if position != len(normalized) or position == 0:
        raise ValueError("interval must be a duration such as 30m, 2h, or 1d")
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"interval must be between {int(MIN_INTERVAL_SECONDS)} seconds and 366 days"
        )
    return seconds


def normalize_timezone(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be an IANA time zone name")
    name = value.strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc
    return name


def normalize_cron(value: str, timezone_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError("cron expression must be a string")
    expression = " ".join(value.strip().split())
    fields = expression.split(" ")
    if len(fields) != 5:
        raise ValueError("cron expression must contain exactly five fields")
    day_of_week = fields[4]
    if any(character.isdigit() for character in day_of_week):
        raise ValueError(
            "cron day-of-week must use names (mon-sun), not numbers, to avoid "
            "cross-engine weekday ambiguity"
        )
    try:
        valid = croniter.is_valid(expression, strict=True)
    except (CroniterError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc
    if not valid:
        raise ValueError("invalid cron expression")
    return expression


def build_schedule(
    *,
    at: str | None = None,
    every: str | None = None,
    cron: str | None = None,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> ScheduleSpec:
    supplied = [value is not None for value in (at, every, cron)]
    if sum(supplied) != 1:
        raise ValueError("choose exactly one of at, every, or cron")
    current = (now or utc_now()).astimezone(timezone.utc)
    tz_name = normalize_timezone(timezone_name)
    if at is not None:
        run_at = parse_datetime(at, label="at")
        if run_at <= current:
            raise ValueError("at must be in the future")
        return ScheduleSpec("at", run_at.isoformat(), "UTC")
    if every is not None:
        seconds = parse_duration(every)
        return ScheduleSpec("every", _format_seconds(seconds), "UTC", current)
    assert cron is not None
    return ScheduleSpec("cron", normalize_cron(cron, tz_name), tz_name)


def first_fire_time(spec: ScheduleSpec, *, now: datetime | None = None) -> datetime:
    current = (now or utc_now()).astimezone(timezone.utc)
    if spec.kind == "at":
        return parse_datetime(spec.value, label="at")
    if spec.kind == "every":
        anchor = (spec.anchor_at or current).astimezone(timezone.utc)
        return _next_interval(anchor, float(spec.value), current)
    return _next_cron(spec, current)


def next_fire_time(
    spec: ScheduleSpec,
    *,
    previous: datetime,
    now: datetime | None = None,
) -> datetime | None:
    current = (now or utc_now()).astimezone(timezone.utc)
    prior = previous.astimezone(timezone.utc)
    if spec.kind == "at":
        return None
    if spec.kind == "every":
        anchor = (spec.anchor_at or prior).astimezone(timezone.utc)
        return _next_interval(anchor, float(spec.value), current)
    return _next_cron(spec, max(prior, current))


def render_schedule(spec: ScheduleSpec) -> str:
    if spec.kind == "at":
        return f"at {spec.value}"
    if spec.kind == "every":
        return f"every {_render_duration(float(spec.value))}"
    return f"cron {spec.value} ({spec.timezone})"


def _next_interval(anchor: datetime, seconds: float, now: datetime) -> datetime:
    elapsed = max(0.0, (now - anchor).total_seconds())
    steps = math.floor(elapsed / seconds) + 1
    return anchor + timedelta(seconds=steps * seconds)


def _next_cron(spec: ScheduleSpec, after: datetime) -> datetime:
    schedule_timezone = ZoneInfo(spec.timezone)
    base = after.astimezone(schedule_timezone)
    try:
        candidate = croniter(spec.value, base, ret_type=datetime).get_next(datetime)
    except (CroniterError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"cron schedule has no future fire time: {exc}") from exc
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        candidate = candidate.replace(tzinfo=schedule_timezone)
    result = candidate.astimezone(timezone.utc)
    if result <= after.astimezone(timezone.utc):
        raise ValueError("cron schedule did not advance monotonically")
    return result


def _format_seconds(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".6f").rstrip("0")


def _render_duration(seconds: float) -> str:
    for suffix, scale in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        quotient = seconds / scale
        if quotient.is_integer():
            return f"{int(quotient)}{suffix}"
    return f"{_format_seconds(seconds)}s"
