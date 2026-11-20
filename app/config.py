"""Runtime configuration, read from environment variables (see .env.example).

Nothing happens at import time: `Config.from_env()` builds an immutable config object, which the
app factory passes to the parts that need it. Tests build their own with `Config(...)`.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/data")
    tz_name: str = "Europe/London"

    # The PC being monitored
    pc_name: str = "Gaming PC"
    pc_host: str = ""
    pc_ssh_port: int = 22
    pc_ssh_user: str = ""
    pc_ssh_password: str = ""  # optional; key auth preferred
    ssh_key_path: str = ""  # default: <data_dir>/ssh/id_ed25519

    # Monitoring
    poll_seconds: int = 30
    check_arp: bool = True  # needs network_mode: host + NET_RAW
    arp_interface: str = ""  # e.g. eth0; blank = auto
    ssh_fails_before_alert: int = 4
    auth_check_seconds: int = 300  # how often to test a real SSH login
    # Off (recommended): only talk to the PC if its host key is already in data/known_hosts.
    # On: accept whatever key answers the first time and save it (trust on first use).
    ssh_trust_first_use: bool = False
    dry_run: bool = False  # log commands instead of running them over SSH

    # Web
    cookie_secure: bool = False  # set true if served over HTTPS
    admin_username: str = "admin"
    admin_password: str = ""
    secret_key: str = field(default="", repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pcmonitor.db"

    @property
    def known_hosts_path(self) -> Path:
        return self.data_dir / "known_hosts"

    @property
    def key_path(self) -> str:
        return self.ssh_key_path or str(self.data_dir / "ssh" / "id_ed25519")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        def get(name: str, default: str = "") -> str:
            return env.get(name, default).strip()

        data_dir = Path(get("DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            data_dir=data_dir,
            tz_name=get("TZ", "Europe/London"),
            pc_name=get("PC_NAME", "Gaming PC"),
            pc_host=get("PC_HOST"),
            pc_ssh_port=int(get("PC_SSH_PORT", "22")),
            pc_ssh_user=get("PC_SSH_USER"),
            pc_ssh_password=get("PC_SSH_PASSWORD"),
            ssh_key_path=get("SSH_KEY_PATH"),
            poll_seconds=max(10, int(get("POLL_SECONDS", "30"))),
            check_arp=_truthy(get("CHECK_ARP", "true")),
            arp_interface=get("ARP_INTERFACE"),
            ssh_fails_before_alert=int(get("SSH_FAILS_BEFORE_ALERT", "4")),
            auth_check_seconds=max(60, int(get("AUTH_CHECK_SECONDS", "300"))),
            ssh_trust_first_use=_truthy(get("SSH_TRUST_FIRST_USE", "false")),
            dry_run=_truthy(get("DRY_RUN", "false")),
            cookie_secure=_truthy(get("COOKIE_SECURE", "false")),
            admin_username=get("ADMIN_USERNAME", "admin"),
            admin_password=get("ADMIN_PASSWORD"),
            secret_key=get("SECRET_KEY") or load_or_create_secret(data_dir),
        )


def load_or_create_secret(data_dir: Path) -> str:
    """Session-signing key, kept in the data folder so logins survive restarts."""
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    with contextlib.suppress(OSError):  # e.g. a Windows filesystem
        path.chmod(0o600)
    return key
