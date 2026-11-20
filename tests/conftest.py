"""Shared fixtures. Nothing here touches the environment or a real data folder: every test builds
its own config pointing at its own temporary folder, with an in-memory database."""

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    config = Config(data_dir=tmp_path, pc_host="192.0.2.10", pc_ssh_user="x", dry_run=True, secret_key="test")
    app = create_app(config, db=Database(":memory:"), run_monitor=False)
    with TestClient(app) as c:
        yield c
