import os
import tempfile


from app import config, pc  # noqa: E402

# A real (throwaway) ed25519 public key
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def test_unknown_host_key_is_refused(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "PC_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "PC_SSH_USER", "x")
    monkeypatch.setattr(config, "SSH_TRUST_FIRST_USE", False)
    monkeypatch.setattr(config, "KNOWN_HOSTS_PATH", os.path.join(tempfile.mkdtemp(), "known_hosts"))
    ok, msg = pc.run("whoami")
    assert not ok and "host key" in msg


def test_host_key_lookup(monkeypatch, tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("# 192.0.2.10:22 SSH-2.0-OpenSSH_for_Windows_9.5\n192.0.2.10 " + KEY + "\n")
    monkeypatch.setattr(config, "KNOWN_HOSTS_PATH", str(kh))
    monkeypatch.setattr(config, "PC_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "PC_SSH_PORT", 22)
    assert pc.host_key_known()
    monkeypatch.setattr(config, "PC_HOST", "192.0.2.11")
    assert not pc.host_key_known()
    # Non-standard port uses the [host]:port form, as ssh-keyscan -p writes it
    kh.write_text("[192.0.2.10]:2222 " + KEY + "\n")
    monkeypatch.setattr(config, "PC_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "PC_SSH_PORT", 2222)
    assert pc.host_key_known()
