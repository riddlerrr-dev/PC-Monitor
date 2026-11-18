import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, policy

TZ = ZoneInfo("Europe/London")


def settings(**over):
    s = copy.deepcopy(db.DEFAULT_SETTINGS)
    s.update(over)
    return s


def at(y, mo, d, h, mi=0):
    return datetime(2026, mo, d, h, mi, tzinfo=TZ) if y is None else datetime(y, mo, d, h, mi, tzinfo=TZ)


# 2026-11-02 is a Monday
def test_inside_window():
    r = policy.evaluate(at(None, 11, 2, 18), settings(), TZ)
    assert r["allowed"] and r["deadline"] == at(None, 11, 2, 21)


def test_outside_window_next_allowed():
    r = policy.evaluate(at(None, 11, 2, 22), settings(), TZ)
    assert not r["allowed"] and r["next_allowed"] == at(None, 11, 3, 16)


def test_window_past_midnight():
    s = settings()
    s["schedule"]["fri"] = {"mode": "window", "start": "16:00", "end": "01:00"}
    r = policy.evaluate(at(None, 11, 7, 0, 30), s, TZ)  # Saturday 00:30
    assert r["allowed"] and r["deadline"] == at(None, 11, 7, 1)


def test_none_and_all_day():
    s = settings()
    s["schedule"]["mon"] = {"mode": "none", "start": "00:00", "end": "00:00"}
    assert not policy.evaluate(at(None, 11, 2, 18), s, TZ)["allowed"]
    s["schedule"]["mon"] = {"mode": "all", "start": "00:00", "end": "00:00"}
    r = policy.evaluate(at(None, 11, 2, 3), s, TZ)
    assert r["allowed"] and r["deadline"] == at(None, 11, 3, 0)


def test_holiday_overrides_and_expires():
    s = settings(holiday={"enabled": True, "mode": "window", "start": "09:00", "end": "23:00", "until": "2026-11-03"})
    assert policy.evaluate(at(None, 11, 2, 22), s, TZ)["allowed"]       # holiday hours
    assert policy.evaluate(at(None, 11, 3, 10), s, TZ)["allowed"]       # last holiday day
    assert not policy.evaluate(at(None, 11, 4, 10), s, TZ)["allowed"]   # back to normal Wed


def test_block_beats_everything():
    s = settings(block={"active": True, "until": None, "by": "x"})
    r = policy.evaluate(at(None, 11, 2, 18), s, TZ)
    assert not r["allowed"] and r["reason"] == "blocked"


def test_block_until_expires():
    s = settings(block={"active": True, "until": at(None, 11, 2, 17).isoformat(), "by": "x"})
    assert policy.evaluate(at(None, 11, 2, 18), s, TZ)["allowed"]


def test_extension_extends_deadline():
    now = at(None, 11, 2, 20, 50)
    s = settings(extension={"from": now.isoformat(), "until": at(None, 11, 2, 21, 30).isoformat(), "by": "x"})
    r = policy.evaluate(now, s, TZ)
    assert r["allowed"] and r["deadline"] == at(None, 11, 2, 21, 30)
    # also allows the PC on outside hours
    r2 = policy.evaluate(at(None, 11, 2, 21, 10), s, TZ)
    assert r2["allowed"] and r2["source"] == "extension"


def test_dst_change_day():
    # Clocks go back 2026-10-25; schedule still ends at 21:00 local
    r = policy.evaluate(at(None, 10, 25, 20), settings(), TZ)
    assert r["deadline"] == at(None, 10, 25, 21)
