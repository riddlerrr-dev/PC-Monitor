"""Test doubles: a fake clock, an in-memory database and a fake PC, wired into a real Monitor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import DEFAULT_SETTINGS, Database
from app.monitor import Monitor
from app.pc import WindowsPC

TZ = ZoneInfo("Europe/London")


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakePC(WindowsPC):
    """The real WindowsPC (so the real shutdown commands are built), with the network replaced.
    Every command that would be sent over SSH is recorded in `world.cmds`."""

    def __init__(self, world: World) -> None:
        super().__init__(world.config)
        self.w = world

    def check(self) -> tuple[bool, bool]:
        s = self.w.state
        return s["online"] and s["ssh"], s["online"]

    def run(self, *commands: str) -> tuple[bool, str]:
        if self.w.state["fail"]:
            return False, "SSHException: connection reset"
        self.w.cmds.extend(commands)
        return True, "ok"

    def verify(self) -> tuple[bool, str]:
        ok = self.w.state["auth"]
        return ok, "ok" if ok else "AuthenticationException"

    def boot_time(self) -> float | None:
        return self.w.boot()

    def host_key_known(self) -> bool:
        return True


class World:
    """One simulated PC and server. `monitor()` starts (or restarts) the server's monitor."""

    def __init__(self, start: datetime, data_dir: Path = Path("/nonexistent")) -> None:
        self.config = Config(data_dir=data_dir, pc_host="192.0.2.10", pc_ssh_user="test")
        self.clock = Clock(start)
        self.db = Database(":memory:")
        self.state: dict[str, bool] = {"online": True, "ssh": True, "auth": True, "fail": False}
        self.cmds: list[str] = []
        self.boot: Callable[[], float | None] = lambda: None
        self.pc = FakePC(self)
        for k in ("block", "extension", "holiday", "schedule", "warnings", "grace_seconds", "enforcement_enabled"):
            self.db.set_setting(k, DEFAULT_SETTINGS[k])

    def monitor(self) -> Monitor:
        return Monitor(self.config, self.db, self.pc, notifier=lambda *a: (True, ""), clock=self.clock)

    def types_since(self, ts: datetime) -> list[str]:
        return [e["type"] for e in self.db.events_between(int(ts.timestamp()), 2**40)]

    def events_since(self, ts: datetime) -> list[dict[str, Any]]:
        return self.db.events_between(int(ts.timestamp()), 2**40)


def tick(m: Monitor) -> None:
    asyncio.run(m.tick())
