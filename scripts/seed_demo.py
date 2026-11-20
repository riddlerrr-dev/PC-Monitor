"""Fill a throwaway database with two weeks of made-up history, to preview the UI.

    DATA_DIR=./demo-data python -m scripts.seed_demo
Never point this at your real data folder.
"""

import random
from datetime import datetime, timedelta

from app.config import Config
from app.db import Database

config = Config.from_env()
db = Database(config.db_path)

random.seed(4)
now = datetime.now(config.tz).replace(second=0, microsecond=0)
start = now - timedelta(days=14)
step = config.poll_seconds


def on_periods(day):
    """Made-up evening: on after school, sometimes back on after shutdown."""
    d = day.date()
    base = datetime.combine(d, datetime.min.time(), tzinfo=config.tz)
    weekend = d.weekday() >= 5
    periods = []
    s = base + timedelta(hours=10 if weekend else 16, minutes=random.randint(0, 50))
    end_allowed = base + timedelta(hours=22, minutes=30) if d.weekday() in (4, 5) else base + timedelta(hours=21)
    periods.append((s, end_allowed + timedelta(seconds=random.randint(5, 40)), "normal"))
    if random.random() < 0.45:
        back = end_allowed + timedelta(minutes=random.randint(8, 40))
        periods.append((back, back + timedelta(seconds=random.randint(70, 140)), "sneaky"))
    return periods


samples, events = [], []
day = start
while day < now:
    for s, e, kind in on_periods(day):
        if s > now:
            continue
        e = min(e, now)
        events.append(
            (
                s,
                "power_on" if kind == "normal" else "power_on_outside",
                "PC turned on" if kind == "normal" else f"PC turned on outside allowed hours ({s:%H:%M})",
                None,
            )
        )
        if kind == "normal":
            dl = e.replace(second=0)
            for w in (15, 5, 1):
                t = dl - timedelta(minutes=w)
                if s < t < now:
                    events.append(
                        (t, "warning", f"{w} minute{'s' if w > 1 else ''} warning sent (shutdown at {dl:%H:%M})", None)
                    )
        else:
            events.append(
                (s + timedelta(seconds=30), "shutdown_sent", "Shutdown sent in 60s, outside allowed hours", None)
            )
        if e < now:
            events.append((e, "power_off", "PC turned off", None))
        t = s
        while t < e:
            samples.append((t, True, not (kind == "sneaky" and random.random() < 0.1)))
            t += timedelta(seconds=step)
    day += timedelta(days=1)

events.append((now - timedelta(days=3, hours=4), "block_on", "Blocked until Thu 07:00", "mum"))
events.append((now - timedelta(days=2, hours=13), "block_off", "Block ended (time reached)", None))
events.append((now - timedelta(days=5, hours=2), "extension", "Allowed until 21:30", "dad"))
events.append((now - timedelta(days=6, hours=6), "schedule_changed", "Daily schedule updated", "alex"))
events.append(
    (
        now - timedelta(days=1, hours=1),
        "ssh_unreachable",
        "PC is on the network but SSH isn't answering, so it can't be shut down.",
        None,
    )
)

# Off samples fill the gaps, except a 5-hour monitor outage 4 days ago
on_ts = {int(t.timestamp()) // step for t, _, _ in samples}
outage = (now - timedelta(days=4, hours=9), now - timedelta(days=4, hours=4))
t = start
while t < now:
    if int(t.timestamp()) // step not in on_ts and not (outage[0] <= t < outage[1]):
        samples.append((t, False, False))
    t += timedelta(seconds=step)

samples.sort()
db.add_samples([(int(t.timestamp()), o, ssh) for t, o, ssh in samples])
for t, ty, m, u in sorted(events):
    db.add_event(ty, m, u, ts=int(t.timestamp()))
print(f"seeded {len(samples)} samples and {len(events)} events into {config.data_dir}")
