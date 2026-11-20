from pathlib import Path

from app.config import Config
from app.pc import WindowsPC

# A real (throwaway) ed25519 public key
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def pc(tmp_path: Path, host: str = "192.0.2.10", port: int = 22) -> WindowsPC:
    return WindowsPC(Config(data_dir=tmp_path, pc_host=host, pc_ssh_port=port, pc_ssh_user="x"))


def test_unknown_host_key_is_refused(tmp_path):
    ok, msg = pc(tmp_path).run("whoami")  # no known_hosts file at all
    assert not ok and "host key" in msg


def test_host_key_lookup(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("# 192.0.2.10:22 SSH-2.0-OpenSSH_for_Windows_9.5\n192.0.2.10 " + KEY + "\n")
    assert pc(tmp_path).host_key_known()
    assert not pc(tmp_path, host="192.0.2.11").host_key_known()
    # Non-standard port uses the [host]:port form, as ssh-keyscan -p writes it
    kh.write_text("[192.0.2.10]:2222 " + KEY + "\n")
    assert pc(tmp_path, port=2222).host_key_known()


def test_dry_run_sends_nothing(tmp_path):
    p = WindowsPC(Config(data_dir=tmp_path, pc_host="192.0.2.10", pc_ssh_user="x", dry_run=True))
    assert p.run("shutdown /a") == (True, "dry run")
    assert p.boot_time() is None


def test_config_from_env(tmp_path):
    c = Config.from_env({"DATA_DIR": str(tmp_path), "PC_HOST": " 10.0.0.5 ", "POLL_SECONDS": "3", "DRY_RUN": "yes"})
    assert c.pc_host == "10.0.0.5" and c.poll_seconds == 10 and c.dry_run  # poll has a 10s floor
    assert c.secret_key and (tmp_path / "secret_key").read_text() == c.secret_key  # kept for next start
    assert Config.from_env({"DATA_DIR": str(tmp_path)}).secret_key == c.secret_key
