#!/bin/sh
# Date tests with the viewing device set to different time zones.
# Fails if any run fails: the result comes from node's exit code, not from reading its output.
set -eu
cd "$(dirname "$0")/.."
for z in Europe/London UTC America/New_York Asia/Kolkata Australia/Lord_Howe; do
  echo "== device time zone: $z"
  TZ=$z node --test --test-reporter=spec tests/tz.test.mjs
done