from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain import schedule


def test_cron_weekday_and_timezone() -> None:
    # Friday 2026-11-20 18:00 UTC is Friday 10:00 in Los Angeles; the next weekday 08:00 is Monday.
    after = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
    nxt = schedule.next_occurrence("0 8 * * 1-5", "America/Los_Angeles", after=after)
    assert nxt.isoformat() == "2026-11-23T08:00:00-08:00"
    assert nxt.strftime("%A") == "Monday"


def test_cron_day_of_month_or_weekday_when_both_restricted() -> None:
    after = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    nxt = schedule.next_occurrence("0 9 1 * 1", "UTC", after=after)
    # 2026-11-23 is a Monday; day-of-month 1 is a different branch, and cron matches either.
    assert nxt.isoformat() == "2026-11-23T09:00:00+00:00"


def test_invalid_cron_and_timezone_are_errors() -> None:
    with pytest.raises(ValueError):
        schedule.parse_cron("0 8 * *")
    with pytest.raises(ValueError):
        schedule.parse_cron("61 8 * * *")
    with pytest.raises(ValueError):
        schedule.next_occurrence("0 8 * * *", "Mars/Olympus")


def test_leap_day_schedule_is_found() -> None:
    after = datetime(2026, 11, 22, tzinfo=timezone.utc)
    nxt = schedule.next_occurrence("0 0 29 2 *", "UTC", after=after)
    assert nxt.isoformat() == "2028-02-29T00:00:00+00:00"


def test_duration_words() -> None:
    assert schedule.parse_duration("30") == timedelta(minutes=30)
    assert schedule.parse_duration("2h30m") == timedelta(hours=2, minutes=30)
    assert schedule.parse_duration("1 day") == timedelta(days=1)
    assert schedule.describe_duration(timedelta(minutes=90)) == "1.5 hours"
    with pytest.raises(ValueError):
        schedule.parse_duration("soon")


def test_next_at_rolls_to_tomorrow() -> None:
    now = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
    nxt = schedule.next_at("08:00", "UTC", now=now)
    assert nxt.isoformat() == "2026-11-23T08:00:00+00:00"
    assert schedule.next_at("2026-11-24T09:30:00Z", "UTC", now=now).isoformat().startswith(
        "2026-11-24T09:30:00"
    )


def test_recurring_phrase() -> None:
    assert schedule.parse_recurring_phrase("every weekday at 8am check the PRs") == (
        "0 8 * * 1-5",
        None,
        "check the PRs",
    )
    assert schedule.parse_recurring_phrase("every day at 17:30") == ("30 17 * * *", None, "")
    assert schedule.parse_recurring_phrase("remind me to stretch") is None
