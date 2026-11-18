"""Talking to the Windows PC: reachability checks and SSH commands."""
import logging
import os
import re
import shutil
import socket
import subprocess

import paramiko

from . import config

log = logging.getLogger("pcmonitor.pc")
_ARPING = shutil.which("arping")


def ssh_port_open(timeout: float = 2.0) -> bool:
    if not config.PC_HOST:
        return False
    try:
        with socket.create_connection((config.PC_HOST, config.PC_SSH_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def arp_alive() -> bool:
    """ARP replies are answered even when Windows Firewall blocks everything else,
    so this sees the PC on even if SSH has been switched off."""
    if not (config.CHECK_ARP and _ARPING and config.PC_HOST):
        return False
    cmd = [_ARPING, "-c", "2", "-w", "3"]
    if config.ARP_INTERFACE:
        cmd += ["-I", config.ARP_INTERFACE]
    cmd.append(config.PC_HOST)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=6)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check() -> tuple[bool, bool]:
    """Return (ssh_port_open, arp_ok). An open port isn't proof we can log in; see verify()."""
    ssh_ok = ssh_port_open()
    arp_ok = arp_alive() if not ssh_ok else True
    return ssh_ok, arp_ok


def _clean(msg: str) -> str:
    # Keep the message safe inside cmd.exe double quotes
    return re.sub(r'[^A-Za-z0-9 .,:!?()\'/-]', "", msg)[:200]


def run(*commands: str) -> tuple[bool, str]:
    """Run one or more commands over a single SSH connection. Each is sent separately, so
    this works whether the PC's OpenSSH default shell is cmd.exe or PowerShell.
    Only the last command's exit code counts."""
    if config.DRY_RUN:
        for c in commands:
            log.info("DRY RUN, would run: %s", c)
        return True, "dry run"
    if not (config.PC_HOST and config.PC_SSH_USER):
        return False, "PC_HOST / PC_SSH_USER not configured"
    if not config.SSH_TRUST_FIRST_USE and not host_key_known():
        return False, "PC's SSH host key isn't saved in data/known_hosts yet (see README)"
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(config.KNOWN_HOSTS_PATH)
    except FileNotFoundError:
        pass
    # Refuse any machine whose key doesn't match the saved one, unless trust-on-first-use is on
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy() if config.SSH_TRUST_FIRST_USE else paramiko.RejectPolicy())
    try:
        kwargs = dict(hostname=config.PC_HOST, port=config.PC_SSH_PORT, username=config.PC_SSH_USER,
                      timeout=8, banner_timeout=8, auth_timeout=8, allow_agent=False, look_for_keys=False)
        if config.PC_SSH_PASSWORD:
            kwargs["password"] = config.PC_SSH_PASSWORD
        else:
            kwargs["key_filename"] = config.SSH_KEY_PATH
        client.connect(**kwargs)
        if config.SSH_TRUST_FIRST_USE:
            client.save_host_keys(config.KNOWN_HOSTS_PATH)
        code, out = 0, ""
        for command in commands:
            _, stdout, stderr = client.exec_command(command, timeout=20)
            code = stdout.channel.recv_exit_status()
            out = (stdout.read() + stderr.read()).decode(errors="replace").strip()
        return code == 0, out or f"exit {code}"
    except paramiko.BadHostKeyException:
        return False, "the PC's SSH host key has CHANGED. Something else may be answering at that address"
    except paramiko.SSHException as e:
        if "not found in known_hosts" in str(e):
            return False, "the PC's SSH host key isn't in data/known_hosts"
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 - report any SSH failure to the UI
        return False, f"{type(e).__name__}: {e}"
    finally:
        client.close()


def host_key_known() -> bool:
    """True if data/known_hosts has an entry for this PC."""
    if not os.path.exists(config.KNOWN_HOSTS_PATH):
        return False
    try:
        keys = paramiko.HostKeys(config.KNOWN_HOSTS_PATH)
    except Exception:  # noqa: BLE001 - unreadable file counts as missing
        return False
    name = config.PC_HOST if config.PC_SSH_PORT == 22 else f"[{config.PC_HOST}]:{config.PC_SSH_PORT}"
    return keys.lookup(name) is not None


def verify() -> tuple[bool, str]:
    """Log in for real and run a harmless command, to prove shutdowns would work."""
    return run("whoami")


def boot_time() -> float | None:
    """When Windows last started (Unix time), or None if it can't be read."""
    if config.DRY_RUN:
        return None
    ok, out = run('powershell -NoProfile -NonInteractive -Command '
                  '"([DateTimeOffset](Get-CimInstance Win32_OperatingSystem).LastBootUpTime).ToUnixTimeSeconds()"')
    try:
        return float(out.strip().splitlines()[-1]) if ok else None
    except (ValueError, IndexError):
        return None


def schedule_shutdown(seconds: int, message: str) -> tuple[bool, str]:
    """Replace any pending shutdown with one in `seconds`, showing `message` on screen.
    Windows shows the comment as a notification, and the shutdown still happens even if
    this server goes down afterwards."""
    seconds = max(0, min(int(seconds), 315360000))
    # "shutdown /a" fails harmlessly when nothing is pending
    return run("shutdown /a", f'shutdown /s /f /t {seconds} /c "{_clean(message)}"')


def send_message(message: str) -> tuple[bool, str]:
    """Pop-up message without shutting down (msg.exe exists on Windows Pro/Edu, not Home)."""
    return run(f'msg * /TIME:120 "{_clean(message)}"')
