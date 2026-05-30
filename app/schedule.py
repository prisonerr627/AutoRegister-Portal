"""Timetable parsing + clash detection.

The authoritative timing for a section comes from the live API `Routine` string,
e.g.  "Sunday 1:00 PM - 2:30 PM Theory [TBA] & Tuesday 1:00 PM - 2:30 PM Theory [TBA]".
We parse it into day/time slots so we can detect clashes against the user's
currently-registered sections and match day/time alert filters.
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_DAY_IDX = {d.lower(): i for i, d in enumerate(WEEKDAYS)}
_DAY_IDX.update({d.lower()[:3]: i for i, d in enumerate(WEEKDAYS)})


class Slot(TypedDict):
    day: str
    day_idx: int
    start: int  # minutes since midnight
    end: int
    type: str
    room: str


def parse_time_to_minutes(s: str) -> Optional[int]:
    """'1:00 PM' / '11:20 AM' / '9:40' -> minutes since midnight."""
    s = s.strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", s, re.IGNORECASE)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def minutes_to_str(mins: int) -> str:
    h, m = divmod(mins, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


_SEG_RE = re.compile(
    r"(?P<day>Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\s+"
    r"(?P<start>\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(?P<end>\d{1,2}:\d{2}\s*[AP]M)"
    r"(?:\s+(?P<type>[A-Za-z]+))?"
    r"(?:\s*\[(?P<room>[^\]]*)\])?",
    re.IGNORECASE,
)


def parse_routine(routine: str) -> list[Slot]:
    """Parse a live API Routine string into slots. Robust to '&' separators and
    trailing whitespace."""
    slots: list[Slot] = []
    if not routine:
        return slots
    for m in _SEG_RE.finditer(routine):
        start = parse_time_to_minutes(m.group("start"))
        end = parse_time_to_minutes(m.group("end"))
        if start is None or end is None:
            continue
        day = m.group("day").capitalize()
        slots.append(
            Slot(
                day=day,
                day_idx=_DAY_IDX.get(day.lower(), -1),
                start=start,
                end=end,
                type=(m.group("type") or "").capitalize(),
                room=(m.group("room") or "").strip(),
            )
        )
    return slots


def make_slot(day: str, start_str: str, end_str: str, type_: str = "", room: str = "") -> Optional[Slot]:
    start = parse_time_to_minutes(start_str)
    end = parse_time_to_minutes(end_str)
    if start is None or end is None or day.lower() not in _DAY_IDX:
        return None
    return Slot(
        day=day.capitalize(),
        day_idx=_DAY_IDX[day.lower()],
        start=start,
        end=end,
        type=type_.capitalize(),
        room=room.strip(),
    )


def _two_slots_clash(a: Slot, b: Slot) -> bool:
    return a["day_idx"] == b["day_idx"] and a["start"] < b["end"] and b["start"] < a["end"]


def slots_clash(a: list[Slot], b: list[Slot]) -> bool:
    return any(_two_slots_clash(x, y) for x in a for y in b)


def find_clashes(candidate: list[Slot], registered: list[dict]) -> list[dict]:
    """Return the subset of `registered` items whose slots overlap `candidate`.

    Each registered item is {"title": str, "section": str, "slots": [Slot, ...]}.
    """
    out = []
    for reg in registered:
        if slots_clash(candidate, reg.get("slots", [])):
            out.append(reg)
    return out


def schedule_gap(candidate: list[Slot], registered_slots: list[Slot], day_penalty: int = 600) -> int:
    """How tightly `candidate` packs against the already-registered classes — lower is
    tighter. For each candidate slot, add the idle minutes to the NEAREST registered
    class on the same day; a candidate slot on a day with no registered class adds
    `day_penalty` (an extra day on campus is worse than any within-day gap). Used to
    pick the best 'any section' to auto-join so the timetable stays compact."""
    total = 0
    for cs in candidate:
        same_day = [rs for rs in registered_slots if rs["day_idx"] == cs["day_idx"]]
        if not same_day:
            total += day_penalty
            continue
        best = None
        for rs in same_day:
            if rs["start"] >= cs["end"]:
                gap = rs["start"] - cs["end"]
            elif cs["start"] >= rs["end"]:
                gap = cs["start"] - rs["end"]
            else:
                gap = 0  # overlap — non-clashing candidates won't reach here
            best = gap if best is None else min(best, gap)
        total += best
    return total


def matches_daytime_filter(
    slots: list[Slot],
    days: Optional[list[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> bool:
    """A section matches the day/time filter when EVERY one of its slots falls on
    an allowed day (if `days` given) and within the [start, end] window (if given).
    Empty filter components are treated as 'no constraint'."""
    if not slots:
        return False
    allowed_idx = {_DAY_IDX[d.lower()] for d in days if d.lower() in _DAY_IDX} if days else None
    win_start = parse_time_to_minutes(start) if start else None
    win_end = parse_time_to_minutes(end) if end else None
    for s in slots:
        if allowed_idx is not None and s["day_idx"] not in allowed_idx:
            return False
        if win_start is not None and s["start"] < win_start:
            return False
        if win_end is not None and s["end"] > win_end:
            return False
    return True


def routine_summary(routine: str) -> str:
    """Human-readable one-liner for notifications."""
    slots = parse_routine(routine)
    if not slots:
        return routine.strip()
    return " & ".join(
        f"{s['day'][:3]} {minutes_to_str(s['start'])}-{minutes_to_str(s['end'])}"
        f"{(' ' + s['type']) if s['type'] else ''}"
        for s in slots
    )
