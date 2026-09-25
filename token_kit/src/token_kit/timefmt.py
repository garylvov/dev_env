"""One way to write a time a PERSON reads, and one place to change it.

    human(dt)        -> "Mon 21 Sep 2026, 10:42pm"
    human_short(dt)  -> "10:42pm"
    human_day(dt)    -> "Mon 21 Sep 2026"

TWO AUDIENCES, TWO FORMATS, AND THEY ARE NOT INTERCHANGEABLE.
A machine log (events.tsv, a registry row, a .jsonl record) is PARSED and
SORTED, so it stays ISO 8601 with an offset and nothing here touches it. Every
surface a person reads gets these helpers instead, because "2026-09-21T22:42:11
-0400" is not an answer to "when did that happen?".

No leading zero on the hour, lowercase am/pm, and midnight and noon are 12,
not 0: `%I` and `%p` are locale-dependent and platform-dependent (`%-I` is not
portable), so the pieces are assembled here rather than handed to strftime.
The weekday and month names come from a fixed table for the same reason: a
machine with a non-English locale must not change what a handoff file says.
"""

from __future__ import annotations

from datetime import datetime

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _coerce(dt: datetime | float | int | None) -> datetime:
    """A datetime, a POSIX timestamp, or None for now. Naive stays naive."""
    if dt is None:
        return datetime.now().astimezone()
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt).astimezone()
    return dt


def human_short(dt: datetime | float | int | None = None) -> str:
    """The clock alone: "10:42pm", "12:05am", "9:07am", "12:30pm"."""
    d = _coerce(dt)
    hour = d.hour % 12 or 12
    half = "am" if d.hour < 12 else "pm"
    return f"{hour}:{d.minute:02d}{half}"


def human_day(dt: datetime | float | int | None = None) -> str:
    """The date alone: "Mon 21 Sep 2026"."""
    d = _coerce(dt)
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def human(dt: datetime | float | int | None = None) -> str:
    """Date and clock: "Mon 21 Sep 2026, 10:42pm"."""
    d = _coerce(dt)
    return f"{human_day(d)}, {human_short(d)}"


def iso(dt: datetime | float | int | None = None) -> str:
    """The MACHINE spelling, kept here so both formats live in one file and a
    change to one is made next to the other. Seconds, with the offset."""
    return _coerce(dt).strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_iso(text: str) -> datetime | None:
    """An ISO stamp back to a datetime, or None. Used when a HUMAN column is
    rendered from a row that was written for machines."""
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
