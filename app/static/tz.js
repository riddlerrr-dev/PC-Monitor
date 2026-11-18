"use strict";
/* Date/time helpers. Everything is shown in the server's configured time zone (e.g. Europe/London),
   not the viewing device's, so the site matches the schedule even when you're abroad.
   Kept separate from app.js so tests/tz.test.mjs can check them across clock changes. */
var PCTZ = (function () {
  let zone = "Europe/London";
  const cache = {};

  function fmt(opts) {
    const k = zone + JSON.stringify(opts);
    return (cache[k] ||= new Intl.DateTimeFormat("en-GB", { timeZone: zone, ...opts }));
  }

  // Wall-clock parts of a moment in the zone
  function parts(d) {
    const o = {};
    const f = fmt({ year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
    for (const p of f.formatToParts(d)) o[p.type] = p.value;
    return o;
  }

  // Offset of the zone from UTC at a moment, in ms
  function offset(ms) {
    const p = parts(new Date(ms));
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000;
  }

  const dayKey = (d) => { const p = parts(d); return `${p.year}-${p.month}-${p.day}`; };
  const fmtTime = (d) => fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(d);

  // Calendar day `days` away from `base` in the zone, as a YYYY-MM-DD key (calendar maths, not +24h,
  // because a day is 23 or 25 hours long when the clocks change)
  function shiftDay(base, days) {
    const p = parts(base);
    return new Date(Date.UTC(+p.year, +p.month - 1, +p.day + days, 12)).toISOString().slice(0, 10);
  }

  function fmtWhen(d, now = new Date()) {
    const k = dayKey(d);
    if (k === dayKey(now)) return "Today " + fmtTime(d);
    if (k === shiftDay(now, -1)) return "Yesterday " + fmtTime(d);
    return fmt({ weekday: "short", day: "numeric", month: "short" }).format(d) + " " + fmtTime(d);
  }

  // A calendar date string (YYYY-MM-DD), formatted without any zone shifting
  const fmtDate = (iso, opts) => new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", ...opts }).format(new Date(iso + "T12:00:00Z"));

  // Value for <input type="datetime-local">: wall-clock time in the zone (the server reads it in the zone too)
  const localInput = (d) => { const p = parts(d); return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`; };

  // The moment that reads h:m on the wall clock, `days` calendar days after `base`.
  // h may be 24 (rolls into the next day). A time skipped when the clocks go forward
  // (01:30 in the UK) lands just after the jump; a time that happens twice gives the first.
  function tzAt(days, h, m = 0, base = new Date()) {
    const [y, mo, d] = shiftDay(base, days).split("-").map(Number);
    const wall = Date.UTC(y, mo - 1, d, h, m);
    let t = wall - offset(wall);
    t = wall - offset(t);
    const t2 = wall - offset(t - 36e5);  // earlier candidate, for the repeated hour
    if (t2 < t && localInput(new Date(t2)) === localInput(new Date(t))) t = t2;
    return new Date(t);
  }

  const setZone = (z) => { zone = z; };
  return { fmt, parts, offset, dayKey, fmtTime, fmtWhen, fmtDate, localInput, tzAt, setZone };
})();
if (typeof module !== "undefined") module.exports = PCTZ;
