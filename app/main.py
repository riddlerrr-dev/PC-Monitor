"""Web app: login, API and static UI."""
import asyncio
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, notify, pc, policy
from .monitor import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pcmonitor")
STATIC = Path(__file__).parent / "static"
HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except ValueError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    if db.user_count() == 0 and config.ADMIN_PASSWORD:
        db.create_user(config.ADMIN_USERNAME, hash_pw(config.ADMIN_PASSWORD), True)
        log.info("created admin user %r from environment", config.ADMIN_USERNAME)
    task = asyncio.create_task(monitor.run())
    yield
    task.cancel()


app = FastAPI(title="PC Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="pcmonitor_session",
                   max_age=60 * 60 * 24 * 30, same_site="strict", https_only=config.COOKIE_SECURE)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # Browsers can't add custom headers to cross-site form posts, so requiring one blocks CSRF
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        if request.headers.get("x-requested-with") != "pcmonitor":
            return JSONResponse({"detail": "Missing request header"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# ---------- auth ----------

def current_user(request: Request) -> dict:
    uid = request.session.get("uid")
    row = db.get_user(uid) if uid else None
    if not row:
        request.session.clear()
        raise HTTPException(401, "Not logged in")
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only")
    return user


_failures: dict[str, list[float]] = defaultdict(list)


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


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failures[ip] if t > time.time() - 600]
    _failures[ip] = recent
    if len(recent) >= 8:
        raise HTTPException(429, "Too many failed attempts. Try again in 10 minutes.")
    row = db.get_user_by_name(body.username.strip())
    if not row or not check_pw(body.password, row["password_hash"]):
        _failures[ip].append(time.time())
        raise HTTPException(401, "Wrong username or password")
    _failures.pop(ip, None)
    request.session.clear()
    request.session["uid"] = row["id"]
    return {"ok": True}


@app.post("/api/setup")
def first_run_setup(body: UserIn, request: Request):
    """Create the first admin account. Only works while there are no users at all."""
    if db.user_count() > 0:
        raise HTTPException(403, "Already set up")
    uid = db.create_user(body.username, hash_pw(body.password), True)
    request.session["uid"] = uid
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    try:
        return {"user": current_user(request), "needs_setup": False, "timezone": config.TZ_NAME}
    except HTTPException:
        return {"user": None, "needs_setup": db.user_count() == 0, "timezone": config.TZ_NAME}


@app.post("/api/me/password")
def change_password(body: NewPasswordIn, user: dict = Depends(current_user)):
    row = db.get_user(user["id"])
    if not check_pw(body.current, row["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    db.set_password(user["id"], hash_pw(body.new))
    return {"ok": True}


@app.get("/api/users")
def users(_: dict = Depends(admin_user)):
    return db.list_users()


@app.post("/api/users")
def add_user(body: UserIn, user: dict = Depends(admin_user)):
    if db.get_user_by_name(body.username):
        raise HTTPException(400, "That username is taken")
    db.create_user(body.username, hash_pw(body.password), body.is_admin)
    return {"ok": True}


class ResetIn(BaseModel):
    password: str = Field(min_length=8, max_length=256)


@app.post("/api/users/{user_id}/password")
def reset_password(user_id: int, body: ResetIn, _: dict = Depends(admin_user)):
    if not db.get_user(user_id):
        raise HTTPException(404, "No such user")
    db.set_password(user_id, hash_pw(body.password))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def remove_user(user_id: int, user: dict = Depends(admin_user)):
    if user_id == user["id"]:
        raise HTTPException(400, "You can't delete your own account")
    if not db.get_user(user_id):
        raise HTTPException(404, "No such user")
    db.delete_user(user_id)
    return {"ok": True}


# ---------- status & history ----------

def _now() -> datetime:
    return datetime.now(config.TZ)


@app.get("/api/status")
def status(_: dict = Depends(current_user)):
    s = db.get_settings()
    now = _now()
    st = dict(monitor.status) or {"pc_name": config.PC_NAME, "online": False, "summary": "Starting up…",
                                  "problems": []}
    st["block"] = s["block"] | {"effective": policy.block_active(now, s, config.TZ)}
    st["extension"] = s["extension"]
    st["holiday"] = {"enabled": s["holiday"]["enabled"], "until": s["holiday"]["until"],
                     "applies_today": policy.holiday_applies(now.date(), s["holiday"])}
    return st


def build_segments(start: int, end: int):
    gap = max(3 * config.POLL_SECONDS, 120)
    rows = list(db.samples_between(start, end))
    prev = db.last_sample_before(start)
    if prev and start - prev["ts"] <= gap:
        rows.insert(0, {"ts": start, "online": prev["online"], "ssh": prev["ssh"]})
    segs: list[list] = []

    def push(a, b, state):
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
        if nxt is not None and nxt - ts <= gap:
            seg_end = nxt
        else:
            seg_end = min(ts + config.POLL_SECONDS, end, int(time.time()))
        push(ts, seg_end, state)
        cursor = seg_end
    now = int(time.time())
    if cursor < min(end, now):
        push(cursor, min(end, now), "unknown")
    return segs


@app.get("/api/history")
def history(rng: Literal["24h", "7d", "30d", "90d"] = Query("24h", alias="range"), _: dict = Depends(current_user)):
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}[rng]
    end = int(time.time())
    start = end - hours * 3600
    segs = build_segments(start, end)
    events = db.events_between(start, end)

    # Hours on per calendar day (for the 7d+ bar chart)
    daily: dict[str, float] = {}
    day0 = datetime.fromtimestamp(start, config.TZ).date()
    for i in range((datetime.fromtimestamp(end, config.TZ).date() - day0).days + 1):
        daily[(day0 + timedelta(days=i)).isoformat()] = 0.0
    for a, b, state in segs:
        if not state.startswith("on"):
            continue
        t = a
        while t < b:
            d = datetime.fromtimestamp(t, config.TZ)
            next_midnight = int(datetime.combine(d.date() + timedelta(days=1), datetime.min.time(),
                                                 tzinfo=config.TZ).timestamp())
            chunk_end = min(b, next_midnight)
            k = d.date().isoformat()
            if k in daily:
                daily[k] += (chunk_end - t) / 3600
            t = chunk_end

    on_seconds = sum(b - a for a, b, s in segs if s.startswith("on"))
    counts = defaultdict(int)
    for e in events:
        counts[e["type"]] += 1
    return {
        "start": start, "end": end, "segments": segs, "events": events,
        "daily_hours": [{"date": k, "hours": round(v, 2)} for k, v in daily.items()],
        "stats": {
            "on_hours": round(on_seconds / 3600, 2),
            "turned_on_outside": counts["power_on_outside"],
            "shutdowns": counts["shutdown_sent"],
            "ssh_problems": counts["ssh_unreachable"],
        },
        "event_labels": db.EVENT_TYPES,
    }


@app.get("/api/events")
def events(limit: int = 200, _: dict = Depends(current_user)):
    return db.recent_events(min(max(limit, 1), 1000))


# ---------- controls ----------

def _parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return policy.parse_dt(value, config.TZ)
    except ValueError:
        raise HTTPException(400, "Bad date/time")


class BlockIn(BaseModel):
    active: bool
    until: str | None = None      # local datetime, e.g. 2026-11-03T07:00


@app.post("/api/block")
async def set_block(body: BlockIn, user: dict = Depends(current_user)):
    until = _parse_local(body.until) if body.active else None
    if until and until <= _now():
        raise HTTPException(400, "That time has already passed")
    db.set_setting("block", {"active": body.active, "until": until.isoformat() if until else None,
                             "by": user["username"] if body.active else None})
    if body.active:
        msg = f"Blocked until {until.strftime('%a %H:%M')}" if until else "Blocked until unblocked"
        db.add_event("block_on", msg, user["username"])
    else:
        db.add_event("block_off", "Unblocked", user["username"])
    monitor.poke()
    return {"ok": True}


class ExtensionIn(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    until: str | None = None


@app.post("/api/extension")
async def add_extension(body: ExtensionIn, user: dict = Depends(current_user)):
    now = _now()
    s = db.get_settings()
    if body.until:
        until = _parse_local(body.until)
    elif body.minutes:
        r = policy.evaluate(now, s | {"block": {"active": False}}, config.TZ)
        base = r["deadline"] if r["allowed"] else now
        until = base + timedelta(minutes=body.minutes)
    else:
        raise HTTPException(400, "Give minutes or until")
    if until <= now:
        raise HTTPException(400, "That time has already passed")
    db.set_setting("extension", {"from": now.isoformat(), "until": until.isoformat(), "by": user["username"]})
    db.add_event("extension", f"Allowed until {until.strftime('%a %H:%M')}", user["username"])
    monitor.poke()
    return {"ok": True}


@app.delete("/api/extension")
async def cancel_extension(user: dict = Depends(current_user)):
    db.set_setting("extension", {"from": None, "until": None, "by": None})
    db.add_event("extension", "Extra time cancelled", user["username"])
    monitor.poke()
    return {"ok": True}


class ShutdownIn(BaseModel):
    seconds: int = Field(default=60, ge=0, le=3600)


@app.post("/api/shutdown")
async def shutdown_now(body: ShutdownIn, user: dict = Depends(current_user)):
    ok, out = await monitor.shutdown_now(user["username"], body.seconds)
    if not ok:
        raise HTTPException(502, f"Shutdown failed: {out}")
    return {"ok": True}


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=200)


@app.post("/api/message")
async def message(body: MessageIn, _: dict = Depends(current_user)):
    ok, out = await asyncio.to_thread(pc.send_message, body.message)
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
    def _date(cls, v):
        if v:
            datetime.strptime(v, "%Y-%m-%d")
        return v or None


class ScheduleIn(BaseModel):
    schedule: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], RuleIn]
    holiday: HolidayIn


@app.get("/api/settings")
def get_settings(_: dict = Depends(current_user)):
    s = db.get_settings()
    s.pop("block", None)
    s.pop("extension", None)
    s.pop("runtime", None)
    s["notify"] = dict(s["notify"])
    s["notify"]["ntfy_token"] = "••••" if s["notify"].get("ntfy_token") else ""
    s["event_labels"] = db.EVENT_TYPES
    s["timezone"] = config.TZ_NAME
    s["pc"] = {"name": config.PC_NAME, "host": config.PC_HOST, "user": config.PC_SSH_USER,
               "port": config.PC_SSH_PORT, "arp": config.CHECK_ARP, "dry_run": config.DRY_RUN,
               "host_key": ("trust on first use" if config.SSH_TRUST_FIRST_USE
                            else ("saved" if pc.host_key_known() else "NOT saved")),
               "poll_seconds": config.POLL_SECONDS}
    return s


@app.put("/api/schedule")
async def put_schedule(body: ScheduleIn, user: dict = Depends(current_user)):
    if len(body.schedule) != 7:
        raise HTTPException(400, "All seven days are needed")
    old = db.get_settings()
    db.set_setting("schedule", {k: v.model_dump() for k, v in body.schedule.items()})
    db.set_setting("holiday", body.holiday.model_dump())
    if old["schedule"] != db.get_settings()["schedule"]:
        db.add_event("schedule_changed", "Daily schedule updated", user["username"])
    was, now_on = old["holiday"]["enabled"], body.holiday.enabled
    if now_on and not was:
        db.add_event("holiday_on", f"Holiday mode on{f' until {body.holiday.until}' if body.holiday.until else ''}",
                     user["username"])
    elif was and not now_on:
        db.add_event("holiday_off", "Holiday mode off", user["username"])
    elif now_on and old["holiday"] != db.get_settings()["holiday"]:
        db.add_event("schedule_changed", "Holiday hours updated", user["username"])
    monitor.poke()
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
    def _w(cls, v):
        if any(w < 1 or w > 240 for w in v):
            raise ValueError("Warnings must be 1-240 minutes")
        return sorted(set(v), reverse=True)


@app.put("/api/settings")
async def put_settings(body: GeneralIn, user: dict = Depends(current_user)):
    old = db.get_settings()
    n = body.notify.model_dump()
    if n["ntfy_token"] == "••••":
        n["ntfy_token"] = old["notify"].get("ntfy_token", "")
    n["events"] = [e for e in n["events"] if e in db.EVENT_TYPES]
    db.set_setting("warnings", body.warnings)
    db.set_setting("grace_seconds", body.grace_seconds)
    db.set_setting("notify", n)
    db.set_setting("enforcement_enabled", body.enforcement_enabled)
    if old.get("enforcement_enabled", True) != body.enforcement_enabled:
        db.add_event("enforcement_on" if body.enforcement_enabled else "enforcement_off",
                     "Enforcement resumed" if body.enforcement_enabled else "Enforcement paused (monitor only)",
                     user["username"])
    elif old["warnings"] != body.warnings or old["grace_seconds"] != body.grace_seconds:
        db.add_event("settings_changed", "Warning settings updated", user["username"])
    monitor.poke()
    return {"ok": True}


@app.post("/api/test-notification")
async def test_notification(_: dict = Depends(current_user)):
    ok, out = await asyncio.to_thread(notify.send, db.get_settings(), "test", f"{config.PC_NAME}: test",
                                      "Notifications are working.", True)
    if not ok:
        raise HTTPException(502, out)
    return {"ok": True}


# ---------- static ----------

@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
