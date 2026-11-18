#!/bin/sh
# Date tests with the viewing device set to different time zones
set -e
cd "$(dirname "$0")/.."
for z in Europe/London UTC America/New_York Asia/Kolkata Australia/Lord_Howe; do
  echo "device time zone: $z"
  TZ=$z node --test tests/tz.test.mjs | grep -E "^# (pass|fail)"
done
