"""Runtime configuration, read from environment variables (see .env.example)."""
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name, "true" if default else "false").lower()
    return v in ("1", "true", "yes", "on")


DATA_DIR = Path(_env("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TZ_NAME = _env("TZ", "Europe/London")
TZ = ZoneInfo(TZ_NAME)

# The PC being monitored
PC_NAME = _env("PC_NAME", "Gaming PC")
PC_HOST = _env("PC_HOST")
PC_SSH_PORT = int(_env("PC_SSH_PORT", "22"))
PC_SSH_USER = _env("PC_SSH_USER")
PC_SSH_PASSWORD = _env("PC_SSH_PASSWORD")  # optional; key auth preferred
SSH_KEY_PATH = _env("SSH_KEY_PATH", str(DATA_DIR / "ssh" / "id_ed25519"))
KNOWN_HOSTS_PATH = str(DATA_DIR / "known_hosts")

# Monitoring
POLL_SECONDS = max(10, int(_env("POLL_SECONDS", "30")))
CHECK_ARP = _bool("CHECK_ARP", True)          # needs network_mode: host + NET_RAW
ARP_INTERFACE = _env("ARP_INTERFACE")         # e.g. eth0; blank = auto
SSH_FAILS_BEFORE_ALERT = int(_env("SSH_FAILS_BEFORE_ALERT", "4"))
AUTH_CHECK_SECONDS = max(60, int(_env("AUTH_CHECK_SECONDS", "300")))  # how often to test a real SSH login
# Off (recommended): only talk to the PC if its host key is already in data/known_hosts.
# On: accept whatever key answers the first time and save it (trust on first use).
SSH_TRUST_FIRST_USE = _bool("SSH_TRUST_FIRST_USE", False)

# Testing: log commands instead of running them over SSH
DRY_RUN = _bool("DRY_RUN", False)

# Web
PORT = int(_env("PORT", "8080"))
COOKIE_SECURE = _bool("COOKIE_SECURE", False)  # set true if served over HTTPS
ADMIN_USERNAME = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")


def _secret_key() -> str:
    env_key = _env("SECRET_KEY")
    if env_key:
        return env_key
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _secret_key()
