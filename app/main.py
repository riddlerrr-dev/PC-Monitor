"""Web app: login, API and static UI.

Run with `uvicorn --factory app.main:create_app`. The factory builds the config, database, PC
client and monitor and hands them to the routes through FastAPI dependencies, so tests can
build an app with their own (see tests/test_api.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import bcrypt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import notify, policy
from .config import Config
from .db import EVENT_TYPES, Database
from .monitor import Monitor
from .pc import PCClient, WindowsPC

log = logging.getLogger("pcmonitor")
STATIC = Path(__file__).parent / "static"
HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MASKED = "••••"


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except ValueError:
        return False


class LoginLimiter:
    """At most `limit` failed logins per address in `window` seconds."""

    def __init__(self, limit: int = 8, window: int = 600) -> None:
        self.limit, self.window = limit, window
        self._failures: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        recent = [t for t in self._failures[key] if t > time.time() - self.window]
        self._failures[key] = recent
        return len(recent) >= self.limit

    def failed(self, key: str) -> None:
        self._failures[key].append(time.time())

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)


@dataclass
class Services:
    """Everything the routes need, built once per app."""

    config: Config
    db: Database
    pc: PCClient
    monitor: Monitor
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    def now(self) -> datetime:
        return self.monitor.clock()


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    is_admin: bool


# ---------- dependencies ----------


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(services)]


def current_user(request: Request, svc: Svc) -> CurrentUser:
    uid = request.session.get("uid")
    row = svc.db.get_user(uid) if uid else None
    if not row:
        request.session.clear()
        raise HTTPException(401, "Not logged in")
    return CurrentUser(row["id"], row["username"], bool(row["is_admin"]))


User = Annotated[CurrentUser, Depends(current_user)]


def admin_user(user: User) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Admins only")
    return user


Admin = Annotated[CurrentUser, Depends(admin_user)]
router = APIRouter(prefix="/api")


# ---------- auth ----------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewPasswordIn(BaseModel):
    current: str = ""
    new: str = Field(min_length=8, max_length=256)


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)
    is_admin: bool = False


class ResetIn(BaseModel):
    password: str = Field(min_length=8, max_length=256)


@router.post("/login")
def login(body: LoginIn, request: Request, svc: Svc) -> dict[str, bool]:
    ip = request.client.host if request.client else "?"
    if svc.limiter.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in 10 minutes.")
    row = svc.db.get_user_by_name(body.username.strip())
    if not row or not check_pw(body.password, row["password_hash"]):
        svc.limiter.failed(ip)
        raise HTTPException(401, "Wrong username or password")
    svc.limiter.succeeded(ip)
    request.session.clear()
    request.session["uid"] = row["id"]
    return {"ok": True}


@router.post("/setup")
def first_run_setup(body: UserIn, request: Request, svc: Svc) -> dict[str, bool]:
    """Create the first admin account. Only works while there are no users at all."""
    if svc.db.user_count() > 0:
        raise HTTPException(403, "Already set up")
    request.session["uid"] = svc.db.create_user(body.username, hash_pw(body.password), True)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(request: Request, svc: Svc) -> dict[str, Any]:
    tz = svc.config.tz_name
    try:
        u = current_user(request, svc)
    except HTTPException:
        return {"user": None, "needs_setup": svc.db.user_count() == 0, "timezone": tz}
    return {"user": {"id": u.id, "username": u.username, "is_admin": u.is_admin}, "needs_setup": False, "timezone": tz}


@router.post("/me/password")
def change_password(body: NewPasswordIn, user: User, svc: Svc) -> dict[str, bool]:
    row = svc.db.get_user(user.id)
    if not row or not check_pw(body.current, row["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    svc.db.set_password(user.id, hash_pw(body.new))
    return {"ok": True}


@router.get("/users")
def users(_: Admin, svc: Svc) -> list[dict[str, Any]]:
    return svc.db.list_users()


@router.post("/users")
def add_user(body: UserIn, _: Admin, svc: Svc) -> dict[str, bool]:
    if svc.db.get_user_by_name(body.username):
        raise HTTPException(400, "That username is taken")
    svc.db.create_user(body.username, hash_pw(body.password), body.is_admin)
    return {"ok": True}


@router.post("/users/{user_id}/password")
def reset_password(user_id: int, body: ResetIn, _: Admin, svc: Svc) -> dict[str, bool]:
    if not svc.db.get_user(user_id):
        raise HTTPException(404, "No such user")
    svc.db.set_password(user_id, hash_pw(body.password))
    return {"ok": True}


@router.delete("/users/{user_id}")
def remove_user(user_id: int, user: Admin, svc: Svc) -> dict[str, bool]:
    if user_id == user.id:
        raise HTTPException(400, "You can't delete your own account")
    if not svc.db.get_user(user_id):
        raise HTTPException(404, "No such user")
    svc.db.delete_user(user_id)
    return {"ok": True}


# ---------- status & history ----------


@router.get("/status")
def status(_: User, svc: Svc) -> dict[str, Any]:
    s, tz, now = svc.db.get_settings(), svc.config.tz, svc.now()
    starting = {"pc_name": svc.config.pc_name, "online": False, "summary": "Starting up…", "problems": []}
    st = dict(svc.monitor.status) or starting
    st["block"] = s["block"] | {"effective": policy.block_active(now, s, tz)}
    st["extension"] = s["extension"]
    st["holiday"] = {
        "enabled": s["holiday"]["enabled"],
        "until": s["holiday"]["until"],
        "applies_today": policy.holiday_applies(now.date(), s["holiday"]),
    }
    return st


Segment = list[Any]  # [start, end, "on" | "on_no_ssh" | "off" | "unknown"]


def build_segments(db: Database, start: int, end: int, poll_seconds: int, now: int) -> list[Segment]:
    """Turn the raw samples into on/off stretches. Gaps longer than a few polls are "unknown"."""
    gap = max(3 * poll_seconds, 120)
    rows = db.samples_between(start, end)
    prev = db.last_sample_before(start)
    if prev and start - prev["ts"] <= gap:
        rows.insert(0, {"ts": start, "online": prev["online"], "ssh": prev["ssh"]})
    segs: list[Segment] = []

    def push(a: int, b: int, state: str) -> None:
        if b <= a:
            return
        if segs and segs[-1][2] == state and segs[-1][1] >= a - 1:
            segs[-1][1] = b
        else:
            segs.append([a, b, state])

    cursor = start
    for i, r in enumerate(rows):
        ts = max(r["ts"], start)
        if ts - cursor > 0:
            push(cursor, ts, "unknown")
        state = ("on" if r["ssh"] else "on_no_ssh") if r["online"] else "off"
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else None
        seg_end = nxt if nxt is not None and nxt - ts <= gap else min(ts + poll_seconds, end, now)
        push(ts, seg_end, state)
        cursor = seg_end
    if cursor < min(end, now):
        push(cursor, min(end, now), "unknown")
    return segs


def hours_per_day(segs: list[Segment], start: int, end: int, tz: Any) -> dict[str, float]:
    """Hours on per calendar day (for the 7d+ bar chart)."""
    daily: dict[str, float] = {}
    day0 = datetime.fromtimestamp(start, tz).date()
    for i in range((datetime.fromtimestamp(end, tz).date() - day0).days + 1):
        daily[(day0 + timedelta(days=i)).isoformat()] = 0.0
    for a, b, state in segs:
        if not state.startswith("on"):
            continue
        t = a
        while t < b:
            d = datetime.fromtimestamp(t, tz)
            midnight = datetime.combine(d.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            next_midnight = int(midnight.timestamp())
            chunk_end = min(b, next_midnight)
            k = d.date().isoformat()
            if k in daily:
                daily[k] += (chunk_end - t) / 3600
            t = chunk_end
    return daily


RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}


@router.get("/history")
def history(
    _: User, svc: Svc, rng: Annotated[Literal["24h", "7d", "30d", "90d"], Query(alias="range")] = "24h"
) -> dict[str, Any]:
    end = int(svc.now().timestamp())
    start = end - RANGE_HOURS[rng] * 3600
    segs = build_segments(svc.db, start, end, svc.config.poll_seconds, end)
    events = svc.db.events_between(start, end)
    daily = hours_per_day(segs, start, end, svc.config.tz)
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        counts[e["type"]] += 1
    return {
        "start": start,
        "end": end,
        "segments": segs,
        "events": events,
        "daily_hours": [{"date": k, "hours": round(v, 2)} for k, v in daily.items()],
        "stats": {
            "on_hours": round(sum(b - a for a, b, s in segs if s.startswith("on")) / 3600, 2),
            "turned_on_outside": counts["power_on_outside"],
            "shutdowns": counts["shutdown_sent"],
            "ssh_problems": counts["ssh_unreachable"],
        },
        "event_labels": EVENT_TYPES,
    }


@router.get("/events")
def events(_: User, svc: Svc, limit: int = 200) -> list[dict[str, Any]]:
    return svc.db.recent_events(min(max(limit, 1), 1000))


# ---------- controls ----------


def parse_local(value: str | None, svc: Services) -> datetime | None:
    if not value:
        return None
    try:
        return policy.parse_dt(value, svc.config.tz)
    except ValueError as e:
        raise HTTPException(400, "Bad date/time") from e


class BlockIn(BaseModel):
    active: bool
    until: str | None = None  # local datetime, e.g. 2026-11-03T07:00


# Routes that call monitor.poke() are async so they run on the event loop, where it's safe to wake the monitor
@router.post("/block")
async def set_block(body: BlockIn, user: User, svc: Svc) -> dict[str, bool]:
    until = parse_local(body.until, svc) if body.active else None
    if until and until <= svc.now():
        raise HTTPException(400, "That time has already passed")
    svc.db.set_setting(
        "block",
        {
            "active": body.active,
            "until": until.isoformat() if until else None,
            "by": user.username if body.active else None,
        },
    )
    if body.active:
        msg = f"Blocked until {until.strftime('%a %H:%M')}" if until else "Blocked until unblocked"
        svc.db.add_event("block_on", msg, user.username)
    else:
        svc.db.add_event("block_off", "Unblocked", user.username)
    svc.monitor.poke()
    return {"ok": True}


class ExtensionIn(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    until: str | None = None


@router.post("/extension")
async def add_extension(body: ExtensionIn, user: User, svc: Svc) -> dict[str, bool]:
    now = svc.now()
    until: datetime | None
    if body.until:
        until = parse_local(body.until, svc)
    elif body.minutes:
        r = policy.evaluate(now, svc.db.get_settings() | {"block": {"active": False}}, svc.config.tz)
        until = (r["deadline"] or now) + timedelta(minutes=body.minutes)
    else:
        raise HTTPException(400, "Give minutes or until")
    if until is None or until <= now:
        raise HTTPException(400, "That time has already passed")
    svc.db.set_setting("extension", {"from": now.isoformat(), "until": until.isoformat(), "by": user.username})
    svc.db.add_event("extension", f"Allowed until {until.strftime('%a %H:%M')}", user.username)
    svc.monitor.poke()
    return {"ok": True}


@router.delete("/extension")
async def cancel_extension(user: User, svc: Svc) -> dict[str, bool]:
    svc.db.set_setting("extension", {"from": None, "until": None, "by": None})
    svc.db.add_event("extension", "Extra time cancelled", user.username)
    svc.monitor.poke()
    return {"ok": True}


class ShutdownIn(BaseModel):
    seconds: int = Field(default=60, ge=0, le=3600)


@router.post("/shutdown")
async def shutdown_now(body: ShutdownIn, user: User, svc: Svc) -> dict[str, bool]:
    ok, out = await svc.monitor.shutdown_now(user.username, body.seconds)
    if not ok:
        raise HTTPException(502, f"Shutdown failed: {out}")
    return {"ok": True}


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=200)


@router.post("/message")
async def message(body: MessageIn, _: User, svc: Svc) -> dict[str, bool]:
    ok, out = await asyncio.to_thread(svc.pc.send_message, body.message)
    if not ok:
        raise HTTPException(502, f"Couldn't send: {out}")
    return {"ok": True}


# ---------- settings ----------


class RuleIn(BaseModel):
    mode: Literal["window", "all", "none"]
    start: str = "16:00"
    end: str = "21:00"

    @field_validator("start", "end")
    @classmethod
    def _hm(cls, v: str) -> str:
        if not HM.match(v):
            raise ValueError("Use HH:MM")
        return v


class HolidayIn(RuleIn):
    enabled: bool = False
    until: str | None = None

    @field_validator("until")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v:
            datetime.strptime(v, "%Y-%m-%d")
        return v or None


class ScheduleIn(BaseModel):
    schedule: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], RuleIn]
    holiday: HolidayIn


@router.get("/settings")
def get_settings(_: User, svc: Svc) -> dict[str, Any]:
    c, s = svc.config, svc.db.get_settings()
    for private in ("block", "extension", "runtime"):
        s.pop(private, None)
    s["notify"] = dict(s["notify"])
    s["notify"]["ntfy_token"] = MASKED if s["notify"].get("ntfy_token") else ""
    s["event_labels"] = EVENT_TYPES
    s["timezone"] = c.tz_name
    host_key = "trust on first use" if c.ssh_trust_first_use else ("saved" if svc.pc.host_key_known() else "NOT saved")
    s["pc"] = {
        "name": c.pc_name,
        "host": c.pc_host,
        "user": c.pc_ssh_user,
        "port": c.pc_ssh_port,
        "arp": c.check_arp,
        "dry_run": c.dry_run,
        "host_key": host_key,
        "poll_seconds": c.poll_seconds,
    }
    return s


@router.put("/schedule")
async def put_schedule(body: ScheduleIn, user: User, svc: Svc) -> dict[str, bool]:
    if len(body.schedule) != 7:
        raise HTTPException(400, "All seven days are needed")
    db, who = svc.db, user.username
    old = db.get_settings()
    db.set_setting("schedule", {k: v.model_dump() for k, v in body.schedule.items()})
    db.set_setting("holiday", body.holiday.model_dump())
    new = db.get_settings()
    if old["schedule"] != new["schedule"]:
        db.add_event("schedule_changed", "Daily schedule updated", who)
    was, now_on = old["holiday"]["enabled"], body.holiday.enabled
    if now_on and not was:
        until = f" until {body.holiday.until}" if body.holiday.until else ""
        db.add_event("holiday_on", f"Holiday mode on{until}", who)
    elif was and not now_on:
        db.add_event("holiday_off", "Holiday mode off", who)
    elif now_on and old["holiday"] != new["holiday"]:
        db.add_event("schedule_changed", "Holiday hours updated", who)
    svc.monitor.poke()
    return {"ok": True}


class NotifyIn(BaseModel):
    ntfy_url: str = Field(default="", max_length=300)
    ntfy_token: str = Field(default="", max_length=200)
    events: list[str] = []

    @field_validator("ntfy_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^https?://", v):
            raise ValueError("Must start with http:// or https://")
        return v


class GeneralIn(BaseModel):
    warnings: list[int] = Field(max_length=6)
    grace_seconds: int = Field(ge=0, le=900)
    enforcement_enabled: bool
    notify: NotifyIn

    @field_validator("warnings")
    @classmethod
    def _w(cls, v: list[int]) -> list[int]:
        if any(w < 1 or w > 240 for w in v):
            raise ValueError("Warnings must be 1-240 minutes")
        return sorted(set(v), reverse=True)


@router.put("/settings")
async def put_settings(body: GeneralIn, user: User, svc: Svc) -> dict[str, bool]:
    db = svc.db
    old = db.get_settings()
    n = body.notify.model_dump()
    if n["ntfy_token"] == MASKED:
        n["ntfy_token"] = old["notify"].get("ntfy_token", "")
    n["events"] = [e for e in n["events"] if e in EVENT_TYPES]
    db.set_setting("warnings", body.warnings)
    db.set_setting("grace_seconds", body.grace_seconds)
    db.set_setting("notify", n)
    db.set_setting("enforcement_enabled", body.enforcement_enabled)
    if old.get("enforcement_enabled", True) != body.enforcement_enabled:
        on = body.enforcement_enabled
        db.add_event(
            "enforcement_on" if on else "enforcement_off",
            "Enforcement resumed" if on else "Enforcement paused (monitor only)",
            user.username,
        )
    elif old["warnings"] != body.warnings or old["grace_seconds"] != body.grace_seconds:
        db.add_event("settings_changed", "Warning settings updated", user.username)
    svc.monitor.poke()
    return {"ok": True}


@router.post("/test-notification")
async def test_notification(_: User, svc: Svc) -> dict[str, bool]:
    title = f"{svc.config.pc_name}: test"
    ok, out = await asyncio.to_thread(
        notify.send, svc.db.get_settings(), "test", title, "Notifications are working.", True
    )
    if not ok:
        raise HTTPException(502, out)
    return {"ok": True}


@router.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


# ---------- app ----------


async def csrf_and_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    # Browsers can't add custom headers to cross-site form posts, so requiring one blocks CSRF
    writes = request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
    if writes and request.headers.get("x-requested-with") != "pcmonitor":
        return JSONResponse({"detail": "Missing request header"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def create_app(
    config: Config | None = None,
    *,
    db: Database | None = None,
    pc: PCClient | None = None,
    monitor: Monitor | None = None,
    run_monitor: bool = True,
) -> FastAPI:
    """Build the app. With no arguments everything comes from the environment (production);
    tests pass their own config, database, fake PC or monitor."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = config or Config.from_env()
    db = db or Database(config.db_path)
    pc = pc or WindowsPC(config)
    svc = Services(config, db, pc, monitor or Monitor(config, db, pc))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if svc.db.user_count() == 0 and config.admin_password:
            svc.db.create_user(config.admin_username, hash_pw(config.admin_password), True)
            log.info("created admin user %r from environment", config.admin_username)
        task = asyncio.create_task(svc.monitor.run()) if run_monitor else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="PC Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.services = svc
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key or secrets.token_urlsafe(48),  # random if none was configured
        session_cookie="pcmonitor_session",
        max_age=60 * 60 * 24 * 30,
        same_site="strict",
        https_only=config.cookie_secure,
    )
    app.middleware("http")(csrf_and_headers)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
