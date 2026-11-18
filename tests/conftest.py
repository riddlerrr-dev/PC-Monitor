"""Every test run gets its own empty data folder, and every test starts with an empty database,
so results never depend on test order or on data left over from a previous run."""
import os
import tempfile

# Must happen before the app is imported: config reads DATA_DIR at import time.
# Always overridden (never inherited), so a real data folder can't be touched by tests.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pcmonitor-test-")

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    c = db.conn()
    for table in ("events", "samples", "settings", "users"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    yield
