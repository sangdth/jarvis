"""Cron and human wording for Brain's timers.

A timer is either a one-shot `followup` (a deadline the owner or an agent sets) or a repeating
`schedule` (a five-field cron expression in a named time zone). Everything here is pure: the database
stores the result, and `Reconciler.tick()` only asks "what is due now?".

Day/month/weekday semantics follow cron: when both day-of-month and day-of-week are restricted, a day
matches either one. Times are computed in the timer's own zone so a morning check stays at 08:00 after a
DST change, and a missed slot is skipped rather than replayed in a burst.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_DAY_NAMES = {
    "sunday": 0,
    "sun": 0,
    "monday": 1,
    "mon": 1,
    "tuesday": 2,
    "tue": 2,
    "tues": 2,
    "wednesday": 3,
    "wed": 3,
    "thursday": 4,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "friday": 5,
    "fri": 5,
    "saturday": 6,
    "sat": 6,
}
_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}
_DURATION_PART_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)?", re.IGNORECASE)
_HHMM_RE = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?$", re.IGNORECASE)
_EVERY_RE = re.compile(
    r"^\s*every\s+(?P<when>day|weekday|weekend|"
    r"sunday|monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sun|mon|tue|tues|wed|thu|thur|thurs|fri|sat)"
    r"\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s+(?P<body>.*))?$",
    re.IGNORECASE | re.DOTALL,
)


def local_timezone_name() -> str:
    """The Mac's IANA zone (`America/Los_Angeles`), not an abbreviation like `PST`."""
    try:
        target = Path("/etc/localtime").resolve()
        if "zoneinfo/" in str(target):
            return str(target).split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def resolve_timezone(name: str | None) -> tuple[str, ZoneInfo]:
    """A validated `(name, ZoneInfo)`; an unknown name is a caller error, never a silent UTC fallback."""
    candidate = (name or "").strip() or local_timezone_name()
    try:
        return candidate, ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone: {candidate!r}") from exc


def _parse_field(text: str, low: int, high: int) -> set[int]:
    raw = text.strip()
    if not raw:
        raise ValueError("empty cron field")
    values: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        step = 1
        if "/" in piece:
            piece, raw_step = piece.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if piece in ("*", "?"):
            start, end = low, high
        elif "-" in piece:
            raw_start, raw_end = piece.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(piece)
        if start > end or start < low or end > high:
            raise ValueError(f"cron value out of range: {part!r}")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    def day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        cron_weekday = (moment.weekday() + 1) % 7  # cron: Sunday is 0; Python: Monday is 0
        day_ok = moment.day in self.days
        weekday_ok = cron_weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def matches(self, moment: datetime) -> bool:
        return (
            self.day_matches(moment)
            and moment.hour in self.hours
            and moment.minute in self.minutes
        )


def parse_cron(expression: str) -> CronSpec:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("a cron expression needs five fields: minute hour day month weekday")
    minute, hour, day, month, weekday = (
        _parse_field(fields[i], *_FIELD_RANGES[i]) for i in range(5)
    )
    if 7 in weekday:
        weekday.discard(7)
        weekday.add(0)
    if not (minute and hour and day and month and weekday):
        raise ValueError("a cron field cannot be empty")
    return CronSpec(
        minutes=frozenset(minute),
        hours=frozenset(hour),
        days=frozenset(day),
        months=frozenset(month),
        weekdays=frozenset(weekday),
        day_restricted=fields[2].strip() not in ("*", "?"),
        weekday_restricted=fields[4].strip() not in ("*", "?"),
    )


def next_occurrence(
    expression: str,
    timezone_name: str | None = None,
    *,
    after: datetime | None = None,
) -> datetime:
    """The first matching minute strictly after `after`, in the timer's zone."""
    _, zone = resolve_timezone(timezone_name)
    spec = parse_cron(expression)
    moment = (after or datetime.now(timezone.utc)).astimezone(zone)
    moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + timedelta(days=366 * 4)  # four years covers Feb 29 and leap-year-only schedules

    while moment < limit:
        if moment.month not in spec.months:
            if moment.month == 12:
                moment = moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                moment = moment.replace(month=moment.month + 1, day=1, hour=0, minute=0)
            continue
        if not spec.day_matches(moment):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if moment.hour not in spec.hours:
            moment = (moment + timedelta(hours=1)).replace(minute=0)
            continue
        if moment.minute not in spec.minutes:
            moment += timedelta(minutes=1)
            continue
        return moment
    raise ValueError(f"cron expression {expression!r} has no occurrence in the next year")


def parse_duration(text: str) -> timedelta:
    """`30`, `30m`, `2h30m`, `1 day`; a bare number means minutes."""
    raw = text.strip()
    if not raw:
        raise ValueError("a duration is required")
    total = 0.0
    consumed = 0
    for match in _DURATION_PART_RE.finditer(raw):
        if match.start() != consumed and raw[consumed : match.start()].strip(" ,"):
            raise ValueError(f"could not read duration {text!r}")
        consumed = match.end()
        value = float(match.group("value"))
        unit = (match.group("unit") or "m").lower()
        if unit not in _DURATION_UNITS:
            raise ValueError(f"unknown duration unit {unit!r}")
        total += value * _DURATION_UNITS[unit]
    if consumed == 0 or raw[consumed:].strip(" ,"):
        raise ValueError(f"could not read duration {text!r}")
    if total <= 0:
        raise ValueError("a duration must be positive")
    return timedelta(seconds=total)


def _parse_clock(text: str) -> tuple[int, int]:
    match = _HHMM_RE.match(text.strip())
    if match is None:
        raise ValueError(f"could not read time {text!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {text!r}")
    return hour, minute


def next_at(text: str, timezone_name: str | None = None, *, now: datetime | None = None) -> datetime:
    """An ISO timestamp, or `HH:MM[(am|pm)]` for the next time the clock shows it."""
    name, zone = resolve_timezone(timezone_name)
    raw = text.strip()
    if not raw:
        raise ValueError("a time is required")
    lowered = raw.lower()
    tomorrow = lowered.startswith("tomorrow")
    if tomorrow:
        raw = raw[len("tomorrow") :].strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(zone)
    except ValueError:
        pass
    hour, minute = _parse_clock(raw)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow or candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def describe_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = round(seconds / 3600, 1)
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    days = round(seconds / 86400, 1)
    return f"{days:g} day{'s' if days != 1 else ''}"


def parse_recurring_phrase(text: str) -> tuple[str, str | None, str] | None:
    """`every weekday at 8am check the PRs` -> `(cron, tz, body)`; None when it is not that shape."""
    match = _EVERY_RE.match(text)
    if match is None:
        return None
    when = match.group("when").lower()
    hour, minute = _parse_clock(match.group("time"))
    if when == "day":
        weekday_field = "*"
    elif when == "weekday":
        weekday_field = "1-5"
    elif when == "weekend":
        weekday_field = "0,6"
    else:
        weekday_field = str(_DAY_NAMES[when])
    body = re.sub(
        r"^(?:to|and)\s+", "", (match.group("body") or "").strip(" ."), flags=re.IGNORECASE
    )
    return f"{minute} {hour} * * {weekday_field}", None, body


def timer_description(
    *,
    kind: str,
    cron_expr: str | None,
    timezone_name: str,
    next_run_at: datetime,
) -> str:
    if kind == "schedule" and cron_expr:
        return f"every `{cron_expr}` ({timezone_name}), next {next_run_at.strftime('%Y-%m-%d %H:%M %Z')}"
    return f"once at {next_run_at.strftime('%Y-%m-%d %H:%M %Z')}"


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
