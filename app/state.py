"""The monitor's state, as typed objects with named transitions.

There are two separate concerns:

* `EnforcementSession`: what we've done to the PC since it was switched on (warnings sent,
  shutdowns scheduled, failures). It resets when the PC turns off or is found to have rebooted,
  and it's saved to the database so a server restart doesn't lose it.
* `ConnectionHealth`: whether we can see and log in to the PC. It's rebuilt from live checks,
  so it isn't saved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

Reason = Literal["outside_hours", "blocked", "manual"]

# How long Windows is given to actually shut down before we try again
SHUTDOWN_GRACE_AFTER_DEADLINE = 45  # after a warned cut-off
SHUTDOWN_GRACE_AFTER_COMMAND = 90  # after an out-of-hours, blocked or manual shutdown
# A Windows timer older than this (at a server restart) is assumed to have fired already
PENDING_STALE_AFTER = 600


@dataclass
class PendingShutdown:
    """A shutdown we've scheduled on the PC's own timer."""

    key: str  # deadline (ISO time) for a warned cut-off, else "not_allowed" or "manual"
    at: float  # when Windows will shut down (Unix time)
    reason: Reason
    set_at: float | None = None  # when we scheduled it (older saved records lack this)

    @property
    def is_manual(self) -> bool:
        return self.key == "manual"

    def to_dict(self) -> dict[str, Any]:
        # "for" is the key name used by earlier versions, so existing databases still load
        return {"for": self.key, "at": self.at, "reason": self.reason, "set_at": self.set_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingShutdown:
        return cls(key=d["for"], at=float(d["at"]), reason=d.get("reason", "outside_hours"), set_at=d.get("set_at"))


@dataclass
class EnforcementSession:
    pending: PendingShutdown | None = None
    hold_until: float = 0.0  # don't send another shutdown before this
    warned: dict[str, set[int]] = field(default_factory=dict)  # deadline -> warning minutes sent
    warned_this_session: bool = False
    attempts: int = 0  # shutdowns sent since the PC was switched on
    block_sent: bool = False  # the block countdown has been given
    fail_streak: int = 0  # failed commands in a row (not saved)

    # ---------- transitions ----------

    def reset(self) -> None:
        """The PC was switched off or restarted: start again from nothing."""
        self.pending = None
        self.hold_until = 0.0
        self.warned = {}
        self.warned_this_session = False
        self.attempts = 0
        self.block_sent = False
        self.fail_streak = 0

    def warning_sent(self, deadline: datetime, minutes_covered: set[int], now: float) -> None:
        self.warned.setdefault(deadline.isoformat(), set()).update(minutes_covered)
        self.warned_this_session = True
        self.pending = PendingShutdown(deadline.isoformat(), deadline.timestamp(), "outside_hours", now)
        # Let Windows' own timer do the shutdown; only step in if it didn't happen
        self.hold_until = deadline.timestamp() + SHUTDOWN_GRACE_AFTER_DEADLINE
        self.fail_streak = 0

    def shutdown_sent(self, reason: Reason, seconds: int, now: float) -> None:
        key = "manual" if reason == "manual" else "not_allowed"
        self.pending = PendingShutdown(key, now + seconds, reason, now)
        self.hold_until = now + seconds + SHUTDOWN_GRACE_AFTER_COMMAND
        self.attempts += 1
        if reason == "blocked":
            self.block_sent = True
        if reason != "manual":
            self.fail_streak = 0

    def pending_cancelled(self) -> None:
        self.pending = None
        self.hold_until = 0.0
        self.fail_streak = 0

    def command_failed(self) -> int:
        """Returns how many commands have now failed in a row."""
        self.fail_streak += 1
        return self.fail_streak

    # ---------- questions ----------

    def warnings_sent_for(self, deadline: datetime) -> set[int]:
        return self.warned.get(deadline.isoformat(), set())

    def countdown_due(self, reason: Reason) -> bool:
        """Should the next shutdown give the usual countdown rather than happen at once?"""
        if reason == "blocked":
            return not self.block_sent  # a new block gets one countdown, even after earlier warnings
        return self.attempts == 0 and not self.warned_this_session

    def should_wait(self, reason: Reason, notice: int, now: float) -> bool:
        """Is a shutdown already on its way that we should leave alone?

        Yes if it was set for this same reason, or if it's a manual one landing within the usual
        countdown. A block arriving while the end-of-day timer runs (or a long manual delay) doesn't wait.
        """
        if now >= self.hold_until:
            return False
        p = self.pending
        if p is None or p.reason == reason:
            return True
        return p.is_manual and p.at <= now + notice

    def pending_outdated_by(self, deadline: datetime) -> bool:
        """A non-manual shutdown is pending, but for some other cut-off (extra time, unblocked...)."""
        return self.pending is not None and not self.pending.is_manual and self.pending.key != deadline.isoformat()

    def predates_boot(self, booted: float) -> bool:
        """Was the pending shutdown set before the PC last started? A restart wipes Windows' timer."""
        return bool(self.pending and self.pending.set_at and booted > self.pending.set_at)

    # ---------- saving ----------

    def to_dict(self, now: float) -> dict[str, Any]:
        cutoff = now - 86400
        warned = {
            k: sorted(v)
            for k, v in self.warned.items()
            if not k[:1].isdigit() or datetime.fromisoformat(k).timestamp() > cutoff
        }
        return {
            "pending": self.pending.to_dict() if self.pending else None,
            "hold_until": self.hold_until,
            "warned": warned,
            "session_warned": self.warned_this_session,
            "attempts": self.attempts,
            "block_sent": self.block_sent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], now: float) -> EnforcementSession:
        s = cls(
            warned={k: set(v) for k, v in (d.get("warned") or {}).items()},
            warned_this_session=bool(d.get("session_warned")),
            attempts=int(d.get("attempts") or 0),
            block_sent=bool(d.get("block_sent")),
        )
        pending = d.get("pending")
        # A Windows timer from before the restart may still be counting down
        if pending and float(pending.get("at", 0)) > now - PENDING_STALE_AFTER:
            s.pending = PendingShutdown.from_dict(pending)
            s.hold_until = float(d.get("hold_until") or 0)
        return s


@dataclass
class ConnectionHealth:
    online: bool | None = None  # None until the first check
    port_open: bool = False
    arp_ok: bool = False
    ssh_ok: bool = False  # port open and logged in, as of the latest check
    login_ok: bool | None = None  # result of the last real SSH login; None = check again
    login_detail: str = ""
    login_checked_at: float = 0.0
    unreachable_streak: int = 0  # checks in a row where it was on but SSH wasn't usable
    alerted: bool = False

    def needs_login_check(self, port_open: bool, now: float, interval: int) -> bool:
        """An open port doesn't prove we can log in, so log in for real: when we don't know yet,
        when the last one failed, when the PC or its SSH port has just come back, or every `interval`."""
        return port_open and (
            self.login_ok is not True
            or not self.online
            or not self.port_open
            or now - self.login_checked_at >= interval
        )

    def observe(self, online: bool, port_open: bool, arp_ok: bool) -> bool:
        """Record this check's results. Returns ssh_ok. Call after any login check this round,
        but before anything that resets the session, so this check can still act on it."""
        self.online, self.port_open, self.arp_ok = online, port_open, arp_ok
        self.ssh_ok = port_open and bool(self.login_ok)
        return self.ssh_ok

    def login_checked(self, ok: bool, detail: str, now: float) -> None:
        self.login_ok, self.login_detail, self.login_checked_at = ok, detail, now

    def login_suspect(self) -> None:
        """A command failed, so the cached "login works" may be wrong. Check again next time."""
        self.login_ok = None

    def new_session(self) -> None:
        self.unreachable_streak = 0
        self.alerted = False
        self.login_ok = None

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)
