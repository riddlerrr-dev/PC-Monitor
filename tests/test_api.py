"""The web API, end to end through FastAPI's test client."""

import pytest

H = {"X-Requested-With": "pcmonitor"}


@pytest.fixture
def admin(client):
    r = client.post("/api/setup", json={"username": "alex", "password": "password123"}, headers=H)
    assert r.status_code == 200, r.text
    return client


def event_types(c):
    return [e["type"] for e in c.get("/api/events").json()]


def test_first_run_setup_only_once(client):
    assert client.get("/api/me").json()["needs_setup"] is True
    assert client.post("/api/setup", json={"username": "alex", "password": "password123"}, headers=H).status_code == 200
    r = client.post("/api/setup", json={"username": "eve", "password": "password123"}, headers=H)
    assert r.status_code == 403


def test_login_logout_and_auth_required(admin):
    me = admin.get("/api/me").json()
    assert me["user"]["username"] == "alex" and me["user"]["is_admin"] and me["timezone"]
    admin.post("/api/logout", headers=H)
    assert admin.get("/api/status").status_code == 401
    assert admin.post("/api/login", json={"username": "alex", "password": "nope"}, headers=H).status_code == 401
    assert admin.post("/api/login", json={"username": "ALEX", "password": "password123"}, headers=H).status_code == 200
    assert admin.get("/api/status").status_code == 200


def test_login_rate_limited(client):
    client.post("/api/setup", json={"username": "alex", "password": "password123"}, headers=H)
    client.post("/api/logout", headers=H)
    for _ in range(8):
        client.post("/api/login", json={"username": "alex", "password": "wrong"}, headers=H)
    r = client.post("/api/login", json={"username": "alex", "password": "password123"}, headers=H)
    assert r.status_code == 429


def test_csrf_header_required(admin):
    r = admin.post("/api/block", json={"active": True})
    assert r.status_code == 403


def test_block_and_unblock_are_logged(admin):
    assert admin.post("/api/block", json={"active": True}, headers=H).status_code == 200
    st = admin.get("/api/status").json()
    assert st["block"]["effective"] and st["block"]["by"] == "alex"
    admin.post("/api/block", json={"active": False}, headers=H)
    assert not admin.get("/api/status").json()["block"]["effective"]
    assert {"block_on", "block_off"} <= set(event_types(admin))


def test_block_in_the_past_rejected(admin):
    r = admin.post("/api/block", json={"active": True, "until": "2020-01-01T07:00"}, headers=H)
    assert r.status_code == 400


def test_extension_minutes_and_cancel(admin):
    assert admin.post("/api/extension", json={"minutes": 30}, headers=H).status_code == 200
    assert admin.get("/api/status").json()["extension"]["until"]
    assert admin.delete("/api/extension", headers=H).status_code == 200
    assert admin.get("/api/status").json()["extension"]["until"] is None
    assert admin.post("/api/extension", json={}, headers=H).status_code == 400


def _schedule(**over):
    day = {"mode": "window", "start": "16:00", "end": "21:00"}
    body = {
        "schedule": {d: dict(day) for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
        "holiday": {"enabled": False, "mode": "window", "start": "09:00", "end": "23:00", "until": None},
    }
    body.update(over)
    return body


def test_schedule_save_and_validation(admin):
    body = _schedule()
    body["schedule"]["fri"] = {"mode": "window", "start": "16:00", "end": "00:30"}
    assert admin.put("/api/schedule", json=body, headers=H).status_code == 200
    assert admin.get("/api/settings").json()["schedule"]["fri"]["end"] == "00:30"
    bad = _schedule()
    bad["schedule"]["mon"]["start"] = "25:00"
    assert admin.put("/api/schedule", json=bad, headers=H).status_code == 422
    hol = _schedule(holiday={"enabled": True, "mode": "all", "start": "00:00", "end": "00:00", "until": "2030-01-01"})
    admin.put("/api/schedule", json=hol, headers=H)
    assert "holiday_on" in event_types(admin)


def test_settings_save_masks_token(admin):
    body = {
        "warnings": [5, 15, 1],
        "grace_seconds": 30,
        "enforcement_enabled": False,
        "notify": {"ntfy_url": "https://ntfy.sh/x", "ntfy_token": "secret", "events": ["warning", "bogus"]},
    }
    assert admin.put("/api/settings", json=body, headers=H).status_code == 200
    s = admin.get("/api/settings").json()
    assert s["warnings"] == [15, 5, 1] and s["grace_seconds"] == 30
    assert s["notify"]["ntfy_token"] == "••••" and s["notify"]["events"] == ["warning"]
    assert "runtime" not in s and "block" not in s
    # Saving again with the masked token keeps the real one
    body["notify"]["ntfy_token"] = "••••"
    admin.put("/api/settings", json=body, headers=H)
    assert admin.get("/api/settings").json()["notify"]["ntfy_token"] == "••••"
    assert "enforcement_off" in event_types(admin)
    bad = dict(body, notify=dict(body["notify"], ntfy_url="ftp://x"))
    assert admin.put("/api/settings", json=bad, headers=H).status_code == 422


def test_user_management(admin):
    assert admin.post("/api/users", json={"username": "mum", "password": "password1"}, headers=H).status_code == 200
    assert admin.post("/api/users", json={"username": "mum", "password": "password1"}, headers=H).status_code == 400
    users = admin.get("/api/users").json()
    mum = next(u for u in users if u["username"] == "mum")
    me = next(u for u in users if u["username"] == "alex")
    assert admin.delete(f"/api/users/{me['id']}", headers=H).status_code == 400
    assert admin.post(f"/api/users/{mum['id']}/password", json={"password": "newpass99"}, headers=H).status_code == 200
    # A member can't manage users
    admin.post("/api/logout", headers=H)
    admin.post("/api/login", json={"username": "mum", "password": "newpass99"}, headers=H)
    assert admin.get("/api/users").status_code == 403
    admin.post("/api/logout", headers=H)
    admin.post("/api/login", json={"username": "alex", "password": "password123"}, headers=H)
    assert admin.delete(f"/api/users/{mum['id']}", headers=H).status_code == 200


def test_change_own_password(admin):
    r = admin.post("/api/me/password", json={"current": "wrong", "new": "password456"}, headers=H)
    assert r.status_code == 400
    r = admin.post("/api/me/password", json={"current": "password123", "new": "password456"}, headers=H)
    assert r.status_code == 200


def test_history_shape(admin):
    h = admin.get("/api/history?range=7d").json()
    assert set(h) >= {"start", "end", "segments", "events", "daily_hours", "stats", "event_labels"}
    assert len(h["daily_hours"]) in (7, 8)
    assert admin.get("/api/history?range=1y").status_code == 422


def test_static_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/tz.js").status_code == 200
    assert client.get("/api/health").json() == {"ok": True}
