"""Works out whether the PC is allowed on right now, and until when."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import DAYS

DAY_NAMES = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def holiday_applies(d: date, holiday: dict) -> bool:
    if not holiday.get("enabled"):
        return False
    until = holiday.get("until")
    return not until or d <= date.fromisoformat(until)


def rule_for_date(d: date, settings: dict) -> tuple[dict, str]:
    """Return (rule, source) where source is 'holiday' or the day key."""
    if holiday_applies(d, settings["holiday"]):
        return settings["holiday"], "holiday"
    key = DAYS[d.weekday()]
    return settings["schedule"][key], key


def day_interval(d: date, rule: dict, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    mode = rule.get("mode", "window")
    if mode == "none":
        return None
    if mode == "all":
        start = end = time(0, 0)
    else:
        start, end = parse_hm(rule["start"]), parse_hm(rule["end"])
    s = datetime.combine(d, start, tzinfo=tz)
    e = datetime.combine(d, end, tzinfo=tz)
    if e <= s:  # runs past midnight (or "all" = full 24h)
        e = datetime.combine(d + timedelta(days=1), end, tzinfo=tz)
    return s, e


def allowed_intervals(now: datetime, settings: dict, tz: ZoneInfo, days_ahead: int = 8):
    """Merged list of (start, end, sources) intervals around `now`."""
    raw = []
    today = now.astimezone(tz).date()
    for off in range(-1, days_ahead + 1):
        d = today + timedelta(days=off)
        rule, source = rule_for_date(d, settings)
        iv = day_interval(d, rule, tz)
        if iv:
            raw.append((iv[0], iv[1], {source}))
    ext = settings.get("extension") or {}
    ext_until = parse_dt(ext.get("until"), tz)
    if ext_until and ext_until > now:
        ext_from = parse_dt(ext.get("from"), tz) or now
        raw.append((min(ext_from, now), ext_until, {"extension"}))
    raw.sort(key=lambda x: x[0])
    merged: list[list] = []
    for s, e, src in raw:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] |= src
        else:
            merged.append([s, e, set(src)])
    return [(s, e, src) for s, e, src in merged]


def block_active(now: datetime, settings: dict, tz: ZoneInfo) -> bool:
    b = settings.get("block") or {}
    if not b.get("active"):
        return False
    until = parse_dt(b.get("until"), tz)
    return until is None or now < until


def evaluate(now: datetime, settings: dict, tz: ZoneInfo) -> dict:
    """Decide whether the PC may be on at `now`.

    Returns: allowed, reason, deadline (end of allowed time), next_allowed, source.
    """
    if block_active(now, settings, tz):
        until = parse_dt(settings["block"].get("until"), tz)
        return {"allowed": False, "reason": "blocked", "deadline": None,
                "next_allowed": until, "source": "block"}

    intervals = allowed_intervals(now, settings, tz)
    for s, e, src in intervals:
        if s <= now < e:
            ext_until = parse_dt((settings.get("extension") or {}).get("until"), tz)
            if "extension" in src and ext_until and e == ext_until:
                source = "extension"   # the extra time is what sets the end
            else:
                source = "holiday" if "holiday" in src else "schedule"
            return {"allowed": True, "reason": source, "deadline": e,
                    "next_allowed": None, "source": source}
    nxt = next((s for s, _, _ in intervals if s > now), None)
    return {"allowed": False, "reason": "outside_hours", "deadline": None,
            "next_allowed": nxt, "source": "schedule"}


def describe(now: datetime, result: dict, settings: dict, tz: ZoneInfo) -> str:
    def fmt(dt: datetime) -> str:
        dt = dt.astimezone(tz)
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        if dt.date() == now.date() + timedelta(days=1):
            return "tomorrow " + dt.strftime("%H:%M")
        return dt.strftime("%a %H:%M")

    if result["reason"] == "blocked":
        return f"Blocked until {fmt(result['next_allowed'])}" if result["next_allowed"] else "Blocked"
    if result["allowed"]:
        if result["deadline"] - now > timedelta(days=6):
            return "Allowed (no end time in the next week)"
        label = {"extension": " (extra time)", "holiday": " (holiday mode)"}.get(result["source"], "")
        return f"Allowed until {fmt(result['deadline'])}{label}"
    if result["next_allowed"]:
        return f"Outside allowed hours. Next allowed {fmt(result['next_allowed'])}"
    return "Not allowed this week"
