"""Background loop: checks the PC, records history, sends warnings and shutdowns."""
import asyncio
import json
import logging
import time
from datetime import date, datetime

from . import config, db, notify, pc, policy

log = logging.getLogger("pcmonitor.monitor")


def _hm(dt: datetime) -> str:
    return dt.astimezone(config.TZ).strftime("%H:%M")


def _mins(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute" + ("" if m == 1 else "s")


class Monitor:
    def __init__(self) -> None:
        self.online: bool | None = None
        self.ssh_ok = False
        self.arp_ok = False
        self.ssh_fail_streak = 0
        self.ssh_alerted = False
        self.warned: dict[str, set[int]] = {}   # deadline -> warning thresholds already sent
        self.session_warned = False             # any warning sent since it was switched on
        self.attempts = 0                       # shutdowns sent since it was switched on
        self.hold_until = 0.0                   # don't send another shutdown before this
        self.win_pending: dict | None = None    # shutdown we've scheduled on the PC
        self.fail_streak = 0                    # failed shutdown/warning/cancel commands in a row
        self.block_sent = False                 # block countdown already given this session
        self.port_ok = False
        self.auth_ok: bool | None = None        # result of the last real SSH login
        self.auth_detail = ""
        self.auth_checked = 0.0
        self.status: dict = {}
        self.lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._saved = ""
        self._load_runtime()

    # ---------- persisted state (survives a server restart) ----------

    def _load_runtime(self) -> None:
        rt = db.get_settings().get("runtime") or {}
        pending = rt.get("pending")
        # A Windows timer from before the restart may still be counting down
        if pending and pending.get("at", 0) > time.time() - 600:
            self.win_pending = pending
            self.hold_until = float(rt.get("hold_until") or 0)
        self.warned = {k: set(v) for k, v in (rt.get("warned") or {}).items()}
        self.session_warned = bool(rt.get("session_warned"))
        self.attempts = int(rt.get("attempts") or 0)
        self.block_sent = bool(rt.get("block_sent"))

    def _save_runtime(self) -> None:
        cutoff = datetime.now(config.TZ).timestamp() - 86400
        self.warned = {k: v for k, v in self.warned.items()
                       if not k[:1].isdigit() or datetime.fromisoformat(k).timestamp() > cutoff}
        rt = {"pending": self.win_pending, "hold_until": self.hold_until,
              "warned": {k: sorted(v) for k, v in self.warned.items()},
              "session_warned": self.session_warned, "attempts": self.attempts,
              "block_sent": self.block_sent}
        blob = json.dumps(rt, sort_keys=True)
        if blob != self._saved:
            db.set_setting("runtime", rt)
            self._saved = blob

    # ---------- helpers ----------

    def poke(self) -> None:
        """Re-check straight away (after settings change)."""
        self._wake.set()

    async def event(self, settings: dict, type_: str, message: str, user: str | None = None) -> None:
        db.add_event(type_, message, user)
        log.info("event %s: %s", type_, message)
        title = f"{config.PC_NAME}: {db.EVENT_TYPES.get(type_, type_)}"
        await asyncio.to_thread(notify.send, settings, type_, title, message)

    async def _failed(self, settings: dict, message: str) -> None:
        """Record a failed command. Only the first failure in a row (and every 10th after)
        is logged and notified, so a flaky connection doesn't flood your phone."""
        self.fail_streak += 1
        self.auth_ok = None
        if self.fail_streak == 1 or self.fail_streak % 10 == 0:
            extra = f" (failed {self.fail_streak} times in a row)" if self.fail_streak > 1 else ""
            await self.event(settings, "shutdown_failed", message + extra)

    async def _cancel_pending(self, settings: dict, message: str, keep_manual: bool = False) -> bool:
        """Cancel the shutdown we scheduled on the PC. Returns True once nothing is pending.
        If it fails, the pending shutdown is kept on record and retried next check."""
        if not self.win_pending or (keep_manual and self.win_pending["for"] == "manual"):
            return True
        ok, out = await asyncio.to_thread(pc.run, "shutdown /a")
        if not ok:
            await self._failed(settings, f"Couldn't cancel the pending shutdown, will keep trying: {out}")
            return False
        self.fail_streak = 0
        self.win_pending = None
        self.hold_until = 0.0
        db.add_event("settings_changed", message)
        return True

    async def _check_reboot(self) -> None:
        """A restart wipes any shutdown timer on Windows. If the PC has booted since we set the
        one on record (e.g. while this server was offline), forget it and start fresh."""
        p = self.win_pending
        if not (p and p.get("set_at")):
            return
        booted = await asyncio.to_thread(pc.boot_time)
        if booted and booted > p["set_at"]:
            self._reset_session()
            self.auth_ok = True  # we just logged in successfully
            db.add_event("power_on", "PC was restarted since the last shutdown was scheduled; scheduling again")

    def _reset_session(self) -> None:
        self.warned.clear()
        self.session_warned = False
        self.attempts = 0
        self.hold_until = 0.0
        self.win_pending = None
        self.fail_streak = 0
        self.block_sent = False
        self.ssh_fail_streak = 0
        self.ssh_alerted = False
        self.auth_ok = None

    # ---------- main loop ----------

    async def run(self) -> None:
        log.info("monitor running: host=%s poll=%ss dry_run=%s", config.PC_HOST, config.POLL_SECONDS, config.DRY_RUN)
        while True:
            try:
                async with self.lock:
                    await self.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=config.POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _expire(self, now: datetime, settings: dict) -> dict:
        changed = False
        b = settings["block"]
        until = policy.parse_dt(b.get("until"), config.TZ)
        if b.get("active") and until and now >= until:
            settings["block"] = {"active": False, "until": None, "by": None}
            db.set_setting("block", settings["block"])
            db.add_event("block_off", "Block ended (time reached)")
            changed = True
        ext = settings["extension"]
        ext_until = policy.parse_dt(ext.get("until"), config.TZ)
        if ext_until and now >= ext_until:
            settings["extension"] = {"from": None, "until": None, "by": None}
            db.set_setting("extension", settings["extension"])
            changed = True
        h = settings["holiday"]
        if h.get("enabled") and h.get("until") and now.date() > date.fromisoformat(h["until"]):
            h["enabled"] = False
            db.set_setting("holiday", h)
            db.add_event("holiday_off", "Holiday mode ended (end date passed)")
            changed = True
        return db.get_settings() if changed else settings

    async def tick(self) -> None:
        now = datetime.now(config.TZ)
        settings = await self._expire(now, db.get_settings())

        port_ok, arp_ok = await asyncio.to_thread(pc.check)
        online = port_ok or arp_ok

        # An open port doesn't prove we can log in, so do a real login now and then:
        # when we don't know yet, when the last one failed, or every AUTH_CHECK_SECONDS
        # when we don't know yet, when the last one failed, when the PC or its SSH port has just
        # come back, or every AUTH_CHECK_SECONDS
        if port_ok and (self.auth_ok is not True or not self.online or not self.port_ok
                        or time.time() - self.auth_checked >= config.AUTH_CHECK_SECONDS):
            self.auth_ok, self.auth_detail = await asyncio.to_thread(pc.verify)
            self.auth_checked = time.time()
            if self.auth_ok:
                await self._check_reboot()
        ssh_ok = port_ok and bool(self.auth_ok)
        self.port_ok = port_ok

        db.add_sample(int(now.timestamp()), online, ssh_ok)
        result = policy.evaluate(now, settings, config.TZ)

        # --- power transitions ---
        prev = self.online
        if prev is None:
            db.add_event("monitor_started", f"Monitoring started. PC is {'on' if online else 'off'}")
        elif online and not prev:
            self._reset_session()
            if result["allowed"]:
                await self.event(settings, "power_on", "PC turned on")
            else:
                why = "while blocked" if result["reason"] == "blocked" else "outside allowed hours"
                await self.event(settings, "power_on_outside", f"PC turned on {why} ({now.strftime('%H:%M')})")
        elif prev and not online:
            await self.event(settings, "power_off", "PC turned off")
            self._reset_session()
        self.online, self.ssh_ok, self.arp_ok = online, ssh_ok, arp_ok

        # --- tamper detection: on the network but SSH not answering ---
        if online and not ssh_ok:
            self.ssh_fail_streak += 1
            if self.ssh_fail_streak >= config.SSH_FAILS_BEFORE_ALERT and not self.ssh_alerted:
                self.ssh_alerted = True
                if port_ok:
                    msg = ("SSH is answering but logging in fails, so the PC can't be shut down. "
                           f"The key may have been removed or the account changed. ({self.auth_detail})")
                else:
                    msg = ("PC is on the network but SSH isn't answering, so it can't be shut down. "
                           "SSH may have been stopped or blocked.")
                await self.event(settings, "ssh_unreachable", msg)
        elif ssh_ok:
            if self.ssh_alerted:
                db.add_event("ssh_restored", "SSH is responding again")
            self.ssh_fail_streak = 0
            self.ssh_alerted = False

        if online and ssh_ok:
            if not settings.get("enforcement_enabled", True):
                await self._cancel_pending(settings, "Enforcement paused, pending shutdown cancelled",
                                           keep_manual=True)
            elif result["allowed"]:
                await self._allowed(now, settings, result)
            else:
                await self._not_allowed(now, settings, result)

        self._save_runtime()
        self._update_status(now, settings, result)

    # ---------- enforcement ----------

    async def _allowed(self, now: datetime, settings: dict, result: dict) -> None:
        deadline: datetime = result["deadline"]
        key = deadline.isoformat()

        # A shutdown is pending on the PC but the rules changed (extra time, unblocked...)
        if self.win_pending and self.win_pending["for"] not in (key, "manual"):
            if not await self._cancel_pending(settings, f"Pending shutdown cancelled, now allowed until {_hm(deadline)}"):
                return

        if self.win_pending and self.win_pending["for"] == "manual":
            return  # a manual shutdown is already on its way

        seconds_left = (deadline - now).total_seconds()
        minutes_left = seconds_left / 60
        thresholds = sorted({int(w) for w in settings.get("warnings") or [] if int(w) > 0}, reverse=True)
        sent = self.warned.setdefault(key, set())
        due = [w for w in thresholds if minutes_left <= w and w not in sent]
        if not due:
            return
        msg = f"This PC will shut down in {_mins(seconds_left)} (at {_hm(deadline)}). Save your work now."
        ok, out = await asyncio.to_thread(pc.schedule_shutdown, int(seconds_left), msg)
        if ok:
            sent.update(w for w in thresholds if minutes_left <= w)  # only once it's really been sent
            self.fail_streak = 0
            self.session_warned = True
            self.win_pending = {"for": key, "at": deadline.timestamp(), "reason": "outside_hours",
                                "set_at": time.time()}
            # Let Windows' own timer do the shutdown; only step in if it didn't happen
            self.hold_until = deadline.timestamp() + 45
            await self.event(settings, "warning", f"{_mins(seconds_left)} warning sent (shutdown at {_hm(deadline)})")
        else:
            await self._failed(settings, f"Couldn't send warning, will retry: {out}")

    async def _not_allowed(self, now: datetime, settings: dict, result: dict) -> None:
        if result["reason"] == "blocked":
            first = not self.block_sent  # a new block gets one countdown, even after earlier warnings
        else:
            first = self.attempts == 0 and not self.session_warned
        secs = int(settings.get("grace_seconds", 60)) if first else 0

        # Wait for the shutdown already on its way if it was set for this same reason, or if it's a
        # manual one landing within the usual countdown. A block arriving while the normal
        # end-of-day timer is counting down (or a long manual delay) acts straight away.
        p = self.win_pending
        if time.time() < self.hold_until:
            if not p or p.get("reason", "outside_hours") == result["reason"]:
                return
            notice = max(secs, int(settings.get("grace_seconds", 60)))
            if p.get("reason") == "manual" and p["at"] <= time.time() + notice:
                return
        if result["reason"] == "blocked":
            why = "This PC is blocked right now."
        else:
            why = "It's outside the allowed hours."
        msg = f"{why} Shutting down in {secs} seconds. Save your work." if secs else f"{why} Shutting down now."
        ok, out = await asyncio.to_thread(pc.schedule_shutdown, secs, msg)
        if ok:
            # Only a shutdown that was really sent counts, and only then do we wait for it.
            # After a failure the next check (POLL_SECONDS later) tries again.
            self.attempts += 1
            self.fail_streak = 0
            self.hold_until = time.time() + secs + 90
            self.win_pending = {"for": "not_allowed", "at": time.time() + secs, "reason": result["reason"],
                                "set_at": time.time()}
            if result["reason"] == "blocked":
                self.block_sent = True
            label = "blocked" if result["reason"] == "blocked" else "outside allowed hours"
            when = f"in {secs}s" if secs else "immediately"
            extra = f" (attempt {self.attempts})" if self.attempts > 1 else ""
            await self.event(settings, "shutdown_sent", f"Shutdown sent {when}, {label}{extra}")
        else:
            await self._failed(settings, f"Shutdown command failed, retrying: {out}")

    async def shutdown_now(self, user: str, seconds: int) -> tuple[bool, str]:
        async with self.lock:
            settings = db.get_settings()
            msg = (f"This PC is being shut down in {seconds} seconds. Save your work." if seconds
                   else "This PC is being shut down now.")
            ok, out = await asyncio.to_thread(pc.schedule_shutdown, seconds, msg)
            if ok:
                self.win_pending = {"for": "manual", "at": time.time() + seconds, "reason": "manual",
                                    "set_at": time.time()}
                self.hold_until = time.time() + seconds + 90
                self.attempts += 1
                db.add_event("shutdown_sent", f"Manual shutdown{f' in {seconds}s' if seconds else ''}", user)
                self._save_runtime()
            else:
                await self.event(settings, "shutdown_failed", f"Manual shutdown failed: {out}", user)
            return ok, out

    # ---------- status for the UI ----------

    def _update_status(self, now: datetime, settings: dict, result: dict) -> None:
        def iso(dt):
            return dt.isoformat() if dt else None

        problems = []
        if not config.PC_HOST:
            problems.append("PC_HOST isn't set, so nothing is being monitored.")
        if self.online and not self.ssh_ok:
            if self.port_ok:
                problems.append("SSH is answering but logging in fails, so the PC can't be shut down remotely. "
                                f"({self.auth_detail})")
            else:
                problems.append("The PC is on but SSH isn't answering, so it can't be shut down remotely.")
        enforcing = settings.get("enforcement_enabled", True)
        if not enforcing:
            problems.append("Enforcement is paused: the PC is only being monitored.")
            if self.win_pending and self.win_pending["for"] != "manual":
                problems.append("A shutdown scheduled before enforcement was paused is still pending on the PC. "
                                "It'll be cancelled as soon as SSH answers.")
        if not config.DRY_RUN and not config.SSH_TRUST_FIRST_USE and not pc.host_key_known():
            problems.append("The PC's SSH host key hasn't been saved yet, so no commands can be sent. "
                            "See \"Save the PC's host key\" in the README.")
        self.status = {
            "pc_name": config.PC_NAME,
            "checked_at": now.isoformat(),
            "online": bool(self.online),
            "ssh_ok": self.ssh_ok,
            "ssh_port_open": self.port_ok,
            "arp_ok": self.arp_ok,
            "arp_enabled": config.CHECK_ARP,
            "allowed": result["allowed"],
            "reason": result["reason"],
            "deadline": iso(result["deadline"]),
            "next_allowed": iso(result["next_allowed"]),
            "summary": policy.describe(now, result, settings, config.TZ),
            "pending_shutdown_at": (datetime.fromtimestamp(self.win_pending["at"], config.TZ).isoformat()
                                    if self.win_pending and self.online else None),
            "enforcement_enabled": settings.get("enforcement_enabled", True),
            "dry_run": config.DRY_RUN,
            "problems": problems,
        }


monitor = Monitor()
