// Run: node --test tests/   (tests/run_tz_tests.sh repeats it with the device in several time zones)
// UK clocks in 2026: forward Sun 29 Mar 01:00 UTC (01:00 -> 02:00), back Sun 25 Oct 01:00 UTC (02:00 -> 01:00)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const T = createRequire(import.meta.url)("../app/static/tz.js");
T.setZone("Europe/London");
const Z = (s) => new Date(s);                  // a UTC instant
const iso = (d) => d.toISOString().replace(".000", "");

test("offset is right either side of both changes", () => {
  assert.equal(T.offset(+Z("2026-03-29T00:59:00Z")), 0);
  assert.equal(T.offset(+Z("2026-03-29T01:00:00Z")), 36e5);
  assert.equal(T.offset(+Z("2026-10-25T00:59:00Z")), 36e5);
  assert.equal(T.offset(+Z("2026-10-25T01:00:00Z")), 0);
});

test("'tomorrow 07:00' is the next calendar day, even when the night is 23 or 25 hours long", () => {
  // Saturday evening before clocks go forward
  assert.equal(iso(T.tzAt(1, 7, 0, Z("2026-03-28T21:00:00Z"))), "2026-03-29T06:00:00Z"); // 07:00 BST
  // 00:30 BST on the 25 Oct (the long night): tomorrow is the 26th, not later the same day
  assert.equal(iso(T.tzAt(1, 7, 0, Z("2026-10-24T23:30:00Z"))), "2026-10-26T07:00:00Z"); // 07:00 GMT
  assert.equal(iso(T.tzAt(1, 7, 0, Z("2026-10-25T22:00:00Z"))), "2026-10-26T07:00:00Z");
});

test("a time skipped by the clocks going forward lands just after the jump", () => {
  const d = T.tzAt(0, 1, 30, Z("2026-03-29T12:00:00Z"));
  assert.equal(T.localInput(d), "2026-03-29T02:30");
});

test("a time that happens twice when the clocks go back gives the first one", () => {
  assert.equal(iso(T.tzAt(0, 1, 30, Z("2026-10-25T12:00:00Z"))), "2026-10-25T00:30:00Z"); // 01:30 BST
});

test("next-hour default rolls over midnight", () => {
  assert.equal(T.localInput(T.tzAt(0, 24, 0, Z("2026-11-18T23:20:00Z"))), "2026-11-19T00:00");
});

test("localInput writes London wall-clock time through both changes", () => {
  const want = { "2026-03-29T00:30:00Z": "2026-03-29T00:30", "2026-03-29T01:30:00Z": "2026-03-29T02:30",
                 "2026-10-25T00:30:00Z": "2026-10-25T01:30", "2026-10-25T01:30:00Z": "2026-10-25T01:30" };
  for (const [k, v] of Object.entries(want)) assert.equal(T.localInput(Z(k)), v, k);
});

test("'Yesterday' uses calendar days, not 24 hours", () => {
  // Just after midnight on the first BST night, something from Sunday morning is yesterday
  assert.equal(T.fmtWhen(Z("2026-03-29T10:00:00Z"), Z("2026-03-29T23:30:00Z")), "Yesterday 11:00");
  assert.equal(T.fmtWhen(Z("2026-10-25T00:10:00Z"), Z("2026-10-26T00:30:00Z")), "Yesterday 01:10");
  assert.equal(T.fmtWhen(Z("2026-10-25T23:59:00Z"), Z("2026-10-26T00:30:00Z")), "Yesterday 23:59");
  assert.equal(T.fmtWhen(Z("2026-10-26T00:10:00Z"), Z("2026-10-26T00:30:00Z")), "Today 00:10");
});

test("graph labels for the repeated hour show it twice, at its real position", () => {
  // Hourly ticks are real instants, so the axis never folds back; the label just repeats 01:00
  const ticks = ["2026-10-25T00:00:00Z", "2026-10-25T01:00:00Z", "2026-10-25T02:00:00Z"].map(Z);
  assert.deepEqual(ticks.map(T.fmtTime), ["01:00", "01:00", "02:00"]);
  const spring = ["2026-03-29T00:00:00Z", "2026-03-29T01:00:00Z"].map(Z);
  assert.deepEqual(spring.map(T.fmtTime), ["00:00", "02:00"]);
});

test("dates from the server (YYYY-MM-DD) don't shift day", () => {
  assert.equal(T.fmtDate("2026-11-06", { day: "numeric", month: "short" }), "6 Nov");
});
