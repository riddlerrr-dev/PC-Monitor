"""Background loop: checks the PC, records history, sends warnings and shutdowns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from . import notify, policy
from .config import Config
from .db import EVENT_TYPES, Database
from .pc import PCClient
from .policy import Decision
from .state import ConnectionHealth, EnforcementSession, Reason

log = logging.getLogger("pcmonitor.monitor")

Clock = Callable[[], datetime]
Settings = dict[str, Any]


def _mins(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute" + ("" if m == 1 else "s")


class Monitor:
    def __init__(
        self,
        config: Config,
        db: Database,
        pc: PCClient,
        *,
        notifier: notify.Notifier = notify.send,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.pc = pc
        self.notifier = notifier
        self.clock: Clock = clock or (lambda: datetime.now(config.tz))
        self.health = ConnectionHealth()
        self.session = EnforcementSession.from_dict(db.get_settings().get("runtime") or {}, self._ts())
        self.status: dict[str, Any] = {}
        self.lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._saved = ""

    # ---------- helpers ----------

    def _ts(self) -> float:
        return self.clock().timestamp()

    def _hm(self, dt: datetime) -> str:
        return dt.astimezone(self.config.tz).strftime("%H:%M")

    def poke(self) -> None:
        """Re-check straight away (after settings change)."""
        self._wake.set()

    def _save_session(self) -> None:
        data = self.session.to_dict(self._ts())
        blob = json.dumps(data, sort_keys=True)
        if blob != self._saved:
            self.db.set_setting("runtime", data)
            self._saved = blob

    def _new_session(self) -> None:
        self.session.reset()
        self.health.new_session()

    async def event(self, settings: Settings, type_: str, message: str, user: str | None = None) -> None:
        """Log an event and send a phone notification if that event type is switched on."""
        self.db.add_event(type_, message, user)
        log.info("event %s: %s", type_, message)
        title = f"{self.config.pc_name}: {EVENT_TYPES.get(type_, type_)}"
        await asyncio.to_thread(self.notifier, settings, type_, title, message)

    async def _failed(self, settings: Settings, message: str) -> None:
        """Record a failed command. Only the first failure in a row (and every 10th after)
        is logged and notified, so a flaky connection doesn't flood your phone."""
        streak = self.session.command_failed()
        self.health.login_suspect()
        if streak == 1 or streak % 10 == 0:
            extra = f" (failed {streak} times in a row)" if streak > 1 else ""
            await self.event(settings, "shutdown_failed", message + extra)

    async def _cancel_pending(self, settings: Settings, message: str, keep_manual: bool = False) -> bool:
        """Cancel the shutdown we scheduled on the PC. Returns True once nothing is pending.
        If it fails, the pending shutdown is kept on record and retried next check."""
        p = self.session.pending
        if p is None or (keep_manual and p.is_manual):
            return True
        ok, out = await asyncio.to_thread(self.pc.run, "shutdown /a")
        if not ok:
            await self._failed(settings, f"Couldn't cancel the pending shutdown, will keep trying: {out}")
            return False
        self.session.pending_cancelled()
        self.db.add_event("settings_changed", message)
        return True

    async def _check_reboot(self) -> None:
        """If the PC has booted since we set the shutdown on record (e.g. while this server was
        offline), Windows' timer is gone: forget it and start fresh."""
        if not (self.session.pending and self.session.pending.set_at):
            return
        booted = await asyncio.to_thread(self.pc.boot_time)
        if booted and self.session.predates_boot(booted):
            self._new_session()
            self.health.login_ok = True  # we just logged in successfully
            self.db.add_event("power_on", "PC was restarted since the last shutdown was scheduled; scheduling again")

    # ---------- main loop ----------

    async def run(self) -> None:
        c = self.config
        log.info("monitor running: host=%s poll=%ss dry_run=%s", c.pc_host, c.poll_seconds, c.dry_run)
        while True:
            try:
                async with self.lock:
                    await self.tick()
            except Exception:
                log.exception("tick failed")  # never let the loop die
            self._wake.clear()
            with contextlib.suppress(TimeoutError):  # woken early by poke(), or the poll interval passed
                await asyncio.wait_for(self._wake.wait(), timeout=c.poll_seconds)

    def _expire(self, now: datetime, settings: Settings) -> Settings:
        """Switch off a block, extra time or holiday mode whose end has passed."""
        tz, changed = self.config.tz, False
        b = settings["block"]
        until = policy.parse_dt(b.get("until"), tz)
        if b.get("active") and until and now >= until:
            self.db.set_setting("block", {"active": False, "until": None, "by": None})
            self.db.add_event("block_off", "Block ended (time reached)")
            changed = True
        ext_until = policy.parse_dt(settings["extension"].get("until"), tz)
        if ext_until and now >= ext_until:
            self.db.set_setting("extension", {"from": None, "until": None, "by": None})
            changed = True
        h = settings["holiday"]
        if h.get("enabled") and h.get("until") and now.date() > date.fromisoformat(h["until"]):
            self.db.set_setting("holiday", h | {"enabled": False})
            self.db.add_event("holiday_off", "Holiday mode ended (end date passed)")
            changed = True
        return self.db.get_settings() if changed else settings

    async def tick(self) -> None:
        now = self.clock()
        settings = self._expire(now, self.db.get_settings())
        health = self.health

        port_open, arp_ok = await asyncio.to_thread(self.pc.check)
        online = port_open or arp_ok
        if health.needs_login_check(port_open, self._ts(), self.config.auth_check_seconds):
            ok, detail = await asyncio.to_thread(self.pc.verify)
            health.login_checked(ok, detail, self._ts())
            if ok:
                await self._check_reboot()

        prev_online = health.online
        ssh_ok = health.observe(online, port_open, arp_ok)
        self.db.add_sample(int(now.timestamp()), online, ssh_ok)
        result = policy.evaluate(now, settings, self.config.tz)

        await self._power_transition(prev_online, online, now, settings, result)
        await self._track_ssh_problems(online, ssh_ok, port_open, settings)

        if online and ssh_ok:
            if not settings.get("enforcement_enabled", True):
                await self._cancel_pending(settings, "Enforcement paused, pending shutdown cancelled", keep_manual=True)
            elif result["allowed"]:
                await self._allowed(now, settings, result)
            else:
                await self._not_allowed(settings, result)

        self._save_session()
        self._update_status(now, settings, result)

    async def _power_transition(
        self, prev: bool | None, online: bool, now: datetime, settings: Settings, result: Decision
    ) -> None:
        if prev is None:
            self.db.add_event("monitor_started", f"Monitoring started. PC is {'on' if online else 'off'}")
        elif online and not prev:
            self._new_session()
            if result["allowed"]:
                await self.event(settings, "power_on", "PC turned on")
            else:
                why = "while blocked" if result["reason"] == "blocked" else "outside allowed hours"
                await self.event(settings, "power_on_outside", f"PC turned on {why} ({now.strftime('%H:%M')})")
        elif prev and not online:
            await self.event(settings, "power_off", "PC turned off")
            self._new_session()

    async def _track_ssh_problems(self, online: bool, ssh_ok: bool, port_open: bool, settings: Settings) -> None:
        """Tamper detection: the PC is on the network but SSH isn't usable."""
        h = self.health
        if online and not ssh_ok:
            h.unreachable_streak += 1
            if h.unreachable_streak >= self.config.ssh_fails_before_alert and not h.alerted:
                h.alerted = True
                if port_open:
                    msg = (
                        "SSH is answering but logging in fails, so the PC can't be shut down. "
                        f"The key may have been removed or the account changed. ({h.login_detail})"
                    )
                else:
                    msg = (
                        "PC is on the network but SSH isn't answering, so it can't be shut down. "
                        "SSH may have been stopped or blocked."
                    )
                await self.event(settings, "ssh_unreachable", msg)
        elif ssh_ok:
            if h.alerted:
                self.db.add_event("ssh_restored", "SSH is responding again")
            h.unreachable_streak = 0
            h.alerted = False

    # ---------- enforcement ----------

    async def _allowed(self, now: datetime, settings: Settings, result: Decision) -> None:
        deadline = result["deadline"]
        assert deadline is not None  # always set when allowed
        s = self.session

        # The rules changed (extra time, unblocked...): cancel the old shutdown first
        cancelled_msg = f"Pending shutdown cancelled, now allowed until {self._hm(deadline)}"
        if s.pending_outdated_by(deadline) and not await self._cancel_pending(settings, cancelled_msg):
            return
        if s.pending and s.pending.is_manual:
            return  # a manual shutdown is already on its way

        seconds_left = (deadline - now).total_seconds()
        minutes_left = seconds_left / 60
        thresholds = {int(w) for w in settings.get("warnings") or [] if int(w) > 0}
        covered = {w for w in thresholds if minutes_left <= w}
        if not covered - s.warnings_sent_for(deadline):
            return
        msg = f"This PC will shut down in {_mins(seconds_left)} (at {self._hm(deadline)}). Save your work now."
        ok, out = await asyncio.to_thread(self.pc.schedule_shutdown, int(seconds_left), msg)
        if ok:
            s.warning_sent(deadline, covered, self._ts())  # marked sent only once it really went
            sent = f"{_mins(seconds_left)} warning sent (shutdown at {self._hm(deadline)})"
            await self.event(settings, "warning", sent)
        else:
            await self._failed(settings, f"Couldn't send warning, will retry: {out}")

    async def _not_allowed(self, settings: Settings, result: Decision) -> None:
        s = self.session
        reason: Reason = "blocked" if result["reason"] == "blocked" else "outside_hours"
        grace = int(settings.get("grace_seconds", 60))
        secs = grace if s.countdown_due(reason) else 0
        if s.should_wait(reason, notice=max(secs, grace), now=self._ts()):
            return

        why = "This PC is blocked right now." if reason == "blocked" else "It's outside the allowed hours."
        msg = f"{why} Shutting down in {secs} seconds. Save your work." if secs else f"{why} Shutting down now."
        ok, out = await asyncio.to_thread(self.pc.schedule_shutdown, secs, msg)
        if ok:
            # Only a shutdown that was really sent counts. After a failure the next check retries.
            s.shutdown_sent(reason, secs, self._ts())
            label = "blocked" if reason == "blocked" else "outside allowed hours"
            when = f"in {secs}s" if secs else "immediately"
            extra = f" (attempt {s.attempts})" if s.attempts > 1 else ""
            await self.event(settings, "shutdown_sent", f"Shutdown sent {when}, {label}{extra}")
        else:
            await self._failed(settings, f"Shutdown command failed, retrying: {out}")

    async def shutdown_now(self, user: str, seconds: int) -> tuple[bool, str]:
        async with self.lock:
            settings = self.db.get_settings()
            msg = (
                f"This PC is being shut down in {seconds} seconds. Save your work."
                if seconds
                else "This PC is being shut down now."
            )
            ok, out = await asyncio.to_thread(self.pc.schedule_shutdown, seconds, msg)
            if ok:
                self.session.shutdown_sent("manual", seconds, self._ts())
                self.db.add_event("shutdown_sent", f"Manual shutdown{f' in {seconds}s' if seconds else ''}", user)
                self._save_session()
            else:
                await self.event(settings, "shutdown_failed", f"Manual shutdown failed: {out}", user)
            return ok, out

    # ---------- status for the UI ----------

    def _problems(self, settings: Settings) -> list[str]:
        c, h, p = self.config, self.health, self.session.pending
        problems = []
        if not c.pc_host:
            problems.append("PC_HOST isn't set, so nothing is being monitored.")
        if h.online and not h.ssh_ok:
            if h.port_open:
                problems.append(
                    f"SSH is answering but logging in fails, so the PC can't be shut down remotely. ({h.login_detail})"
                )
            else:
                problems.append("The PC is on but SSH isn't answering, so it can't be shut down remotely.")
        if not settings.get("enforcement_enabled", True):
            problems.append("Enforcement is paused: the PC is only being monitored.")
            if p and not p.is_manual:
                problems.append(
                    "A shutdown scheduled before enforcement was paused is still pending on the PC. "
                    "It'll be cancelled as soon as SSH answers."
                )
        if not c.dry_run and not c.ssh_trust_first_use and not self.pc.host_key_known():
            problems.append(
                "The PC's SSH host key hasn't been saved yet, so no commands can be sent. "
                'See "Save the PC\'s host key" in the README.'
            )
        return problems

    def _update_status(self, now: datetime, settings: Settings, result: Decision) -> None:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        c, h, p = self.config, self.health, self.session.pending
        self.status = {
            "pc_name": c.pc_name,
            "checked_at": now.isoformat(),
            "online": bool(h.online),
            "ssh_ok": h.ssh_ok,
            "ssh_port_open": h.port_open,
            "arp_ok": h.arp_ok,
            "arp_enabled": c.check_arp,
            "allowed": result["allowed"],
            "reason": result["reason"],
            "deadline": iso(result["deadline"]),
            "next_allowed": iso(result["next_allowed"]),
            "summary": policy.describe(now, result, settings, c.tz),
            "pending_shutdown_at": datetime.fromtimestamp(p.at, c.tz).isoformat() if p and h.online else None,
            "enforcement_enabled": settings.get("enforcement_enabled", True),
            "dry_run": c.dry_run,
            "problems": self._problems(settings),
        }
