"""Simulated evening: warnings, shutdown, turning it back on, blocking, extra time, SSH tampering."""

import asyncio
from datetime import datetime

from tests.fakes import TZ, World, tick


def test_evening():
    start = datetime(2026, 11, 2, 20, 40, tzinfo=TZ)  # Monday, allowed until 21:00
    w = World(start)
    clock, m = w.clock, w.monitor()
    tick(m)
    assert w.cmds == []

    clock.advance(minutes=5, seconds=30)  # 14.5 min left -> 15 min warning
    tick(m)
    assert any("/t 870" in c for c in w.cmds), w.cmds
    n = len(w.cmds)
    tick(m)
    assert len(w.cmds) == n  # not repeated

    clock.advance(minutes=10)  # 4.5 min left -> 5 min warning
    tick(m)
    assert len(w.cmds) == n + 2

    # Deadline passes; Windows' timer should do it, so we hold off briefly
    clock.now = datetime(2026, 11, 2, 21, 0, 20, tzinfo=TZ)
    before = len(w.cmds)
    tick(m)
    assert len(w.cmds) == before
    # He aborted the shutdown: after the hold we force it immediately
    clock.advance(seconds=60)
    tick(m)
    assert "/t 0 " in w.cmds[-1]

    # PC goes off, then he turns it back on at 21:30 -> grace countdown
    w.state["online"] = False
    clock.advance(minutes=1)
    tick(m)
    w.state["online"] = True
    clock.now = datetime(2026, 11, 2, 21, 30, tzinfo=TZ)
    tick(m)
    assert "/t 60 " in w.cmds[-1]
    assert "power_on_outside" in w.types_since(start)
    # Still on after the grace + hold -> immediate
    clock.advance(seconds=160)
    tick(m)
    assert "/t 0 " in w.cmds[-1]


def test_block_and_extension():
    start = datetime(2026, 11, 2, 18, 0, tzinfo=TZ)
    w = World(start)
    clock, m = w.clock, w.monitor()
    w.db.set_setting("block", {"active": True, "until": None, "by": "x"})
    tick(m)
    assert "/t 60 " in w.cmds[-1] and "blocked" in w.cmds[-1]
    # Unblocked during the countdown -> pending shutdown cancelled
    w.db.set_setting("block", {"active": False, "until": None, "by": None})
    clock.advance(seconds=20)
    tick(m)
    assert w.cmds[-1] == "shutdown /a"

    # Warning at 20:46, then extra time given -> cancel and re-warn later
    clock.now = datetime(2026, 11, 2, 20, 46, tzinfo=TZ)
    tick(m)
    assert "/t 840" in w.cmds[-1]
    w.db.set_setting(
        "extension",
        {"from": clock.now.isoformat(), "until": datetime(2026, 11, 2, 21, 30, tzinfo=TZ).isoformat(), "by": "x"},
    )
    tick(m)
    assert "shutdown /a" in w.cmds[-1]
    clock.now = datetime(2026, 11, 2, 21, 16, tzinfo=TZ)
    tick(m)
    assert "/t 840" in w.cmds[-1] and "21:30" in w.cmds[-1]


def test_ssh_tamper_alert():
    start = datetime(2026, 11, 2, 18, 0, tzinfo=TZ)
    w = World(start)
    clock, m = w.clock, w.monitor()
    tick(m)
    w.state["ssh"] = False
    for _ in range(w.config.ssh_fails_before_alert + 2):
        clock.advance(seconds=30)
        tick(m)
    assert w.types_since(start).count("ssh_unreachable") == 1
    w.state["ssh"] = True
    clock.advance(seconds=30)
    tick(m)
    assert "ssh_restored" in w.types_since(start)


# ---------- fixes from the code review ----------


def test_pausing_enforcement_cancels_pending_shutdown():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    assert "/t 840" in w.cmds[-1] and m.session.pending
    # Paused while SSH is briefly down: the pending shutdown is remembered, not forgotten
    w.db.set_setting("enforcement_enabled", False)
    w.state["fail"] = True
    clock.advance(seconds=30)
    tick(m)
    assert m.session.pending and any("still pending" in p for p in m.status["problems"])
    # SSH back: cancelled
    w.state["fail"] = False
    clock.advance(seconds=30)
    tick(m)
    assert w.cmds[-1] == "shutdown /a" and m.session.pending is None
    # Nothing else is sent while paused, even after the deadline
    n = len(w.cmds)
    clock.now = datetime(2026, 11, 2, 21, 30, tzinfo=TZ)
    tick(m)
    assert len(w.cmds) == n


def test_restart_remembers_pending_shutdown():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    assert "/t 840" in w.cmds[-1]
    # Server restarts: a brand new Monitor picks the pending shutdown up from the database
    m2 = w.monitor()
    assert m2.session.pending and m2.session.warned
    w.db.set_setting(
        "extension",
        {"from": clock.now.isoformat(), "until": datetime(2026, 11, 2, 22, 0, tzinfo=TZ).isoformat(), "by": "x"},
    )
    clock.advance(seconds=30)
    tick(m2)
    assert w.cmds[-1] == "shutdown /a"


def test_restart_does_not_repeat_warnings():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    n = len(w.cmds)
    m2 = w.monitor()  # restart
    clock.advance(seconds=30)
    tick(m2)
    assert len(w.cmds) == n  # 15-minute warning not sent twice
    clock.now = datetime(2026, 11, 2, 20, 55, 30, tzinfo=TZ)
    tick(m2)
    assert "/t 270" in w.cmds[-1]  # but the 5-minute one still comes


def test_failed_shutdown_retries_on_next_check():
    w = World(datetime(2026, 11, 2, 22, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    w.state["fail"] = True
    tick(m)
    assert w.cmds == [] and m.session.attempts == 0
    w.state["fail"] = False
    clock.advance(seconds=30)  # one poll later, not 90+ seconds
    tick(m)
    assert "/t 60 " in w.cmds[-1]
    # Only one failure event was logged for the streak
    assert w.types_since(datetime(2026, 11, 2, 22, 0, tzinfo=TZ)).count("shutdown_failed") == 1


def test_failed_warning_is_retried():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    w.state["fail"] = True
    tick(m)
    w.state["fail"] = False
    clock.advance(seconds=30)
    tick(m)
    assert any("/t 81" in c for c in w.cmds), w.cmds


def test_ssh_login_failure_detected():
    start = datetime(2026, 11, 2, 22, 0, tzinfo=TZ)
    w = World(start)
    clock, m = w.clock, w.monitor()
    w.state["auth"] = False  # port open, but the key no longer works
    for _ in range(w.config.ssh_fails_before_alert + 1):
        tick(m)
        clock.advance(seconds=30)
    assert w.cmds == []
    assert m.status["online"] and not m.status["ssh_ok"] and m.status["ssh_port_open"]
    evs = [e for e in w.db.events_between(int(start.timestamp()), 2**40) if e["type"] == "ssh_unreachable"]
    assert len(evs) == 1 and "logging in fails" in evs[0]["message"]


# ---------- second review ----------


def test_block_during_warned_session_takes_effect_now():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    assert "/t 840" in w.cmds[-1]  # 21:00 timer set
    clock.advance(minutes=1)  # 20:47: blocked
    w.db.set_setting("block", {"active": True, "until": None, "by": "x"})
    tick(m)
    assert "blocked" in w.cmds[-1] and "/t 60 " in w.cmds[-1], w.cmds[-1]  # replaces the 21:00 timer
    # Still on after the block countdown -> immediate, not left for the old 21:00 timer
    clock.advance(seconds=160)
    tick(m)
    assert "/t 0 " in w.cmds[-1]


def test_auth_rechecked_when_ssh_port_comes_back():
    w = World(datetime(2026, 11, 2, 18, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    assert m.status["ssh_ok"]
    w.state["ssh"] = False  # port closes, ARP still sees the PC
    clock.advance(seconds=30)
    tick(m)
    w.state["ssh"] = True  # port reopens...
    w.state["auth"] = False  # ...but the login no longer works
    clock.advance(seconds=30)
    tick(m)
    assert not m.status["ssh_ok"] and m.status["ssh_port_open"]


def test_restart_after_pc_rebooted_while_server_offline():
    w = World(datetime(2026, 11, 2, 20, 46, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    boot = {"t": datetime(2026, 11, 2, 16, 0, tzinfo=TZ).timestamp()}
    w.boot = lambda: boot["t"]
    tick(m)
    assert m.session.pending  # 21:00 timer recorded
    # Server goes down; meanwhile the PC is restarted at 20:50, which wipes the Windows timer
    boot["t"] = datetime(2026, 11, 2, 20, 50, tzinfo=TZ).timestamp()
    clock.now = datetime(2026, 11, 2, 20, 52, tzinfo=TZ)
    m2 = w.monitor()
    tick(m2)
    # The stale record is dropped and the shutdown is scheduled again (with the 15-min warning)
    assert "/t 480" in w.cmds[-1], w.cmds[-1]
    assert m2.session.pending.set_at >= clock.now.timestamp()


# ---------- third review ----------


def test_manual_shutdown_outside_hours_is_not_overridden():
    w = World(datetime(2026, 11, 2, 22, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    w.db.set_setting("enforcement_enabled", False)
    tick(m)  # PC seen, nothing sent while paused
    asyncio.run(m.shutdown_now("mum", 60))
    assert "/t 60 " in w.cmds[-1]
    w.db.set_setting("enforcement_enabled", True)  # enforcement on, PC outside hours
    n = len(w.cmds)
    for _ in range(4):  # during the manual countdown + hold
        clock.advance(seconds=30)
        tick(m)
    assert len(w.cmds) == n, w.cmds[n:]  # manual 60s countdown left alone
    clock.advance(seconds=40)  # past 60s + 90s hold, still on
    tick(m)
    assert "/t 0 " in w.cmds[-1]


def test_manual_shutdown_during_block_is_not_overridden():
    w = World(datetime(2026, 11, 2, 18, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    asyncio.run(m.shutdown_now("dad", 60))
    w.db.set_setting("block", {"active": True, "until": None, "by": "dad"})
    n = len(w.cmds)
    clock.advance(seconds=30)
    tick(m)
    assert len(w.cmds) == n  # block waits for the manual shutdown


def test_long_manual_delay_does_not_hold_up_a_block():
    w = World(datetime(2026, 11, 2, 18, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    tick(m)
    asyncio.run(m.shutdown_now("dad", 3600))  # "shut down in an hour"
    w.db.set_setting("block", {"active": True, "until": None, "by": "dad"})
    clock.advance(seconds=30)
    tick(m)
    assert "blocked" in w.cmds[-1] and "/t 60 " in w.cmds[-1]  # block's own countdown wins


def test_block_countdown_cancelled_on_windows_is_resent():
    w = World(datetime(2026, 11, 2, 18, 0, tzinfo=TZ))
    clock, m = w.clock, w.monitor()
    w.db.set_setting("block", {"active": True, "until": None, "by": "x"})
    tick(m)
    assert "/t 60 " in w.cmds[-1]
    # He runs "shutdown /a" on the PC; we can't see that, so the PC simply stays on
    n = len(w.cmds)
    clock.advance(seconds=120)
    tick(m)
    assert len(w.cmds) == n  # still inside the 60s + 90s window
    clock.advance(seconds=31)
    tick(m)
    assert "/t 0 " in w.cmds[-1]  # at most ~2.5 minutes after the block
