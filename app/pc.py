"""Talking to the Windows PC: reachability checks and SSH commands."""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import socket
import subprocess
from typing import Any, Protocol

import paramiko

from .config import Config

log = logging.getLogger("pcmonitor.pc")

Result = tuple[bool, str]


class PCClient(Protocol):
    """What the monitor and the API need from the PC. `WindowsPC` is the real one; tests use a fake."""

    def check(self) -> tuple[bool, bool]: ...
    def verify(self) -> Result: ...
    def boot_time(self) -> float | None: ...
    def run(self, *commands: str) -> Result: ...
    def schedule_shutdown(self, seconds: int, message: str) -> Result: ...
    def send_message(self, message: str) -> Result: ...
    def host_key_known(self) -> bool: ...


def clean_message(msg: str) -> str:
    """Keep a message safe inside cmd.exe double quotes."""
    return re.sub(r"[^A-Za-z0-9 .,:!?()'/-]", "", msg)[:200]


class WindowsPC:
    """A Windows PC running OpenSSH Server."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._arping = shutil.which("arping")

    # ---------- reachability ----------

    def ssh_port_open(self, timeout: float = 2.0) -> bool:
        c = self.config
        if not c.pc_host:
            return False
        try:
            with socket.create_connection((c.pc_host, c.pc_ssh_port), timeout=timeout):
                return True
        except OSError:
            return False

    def arp_alive(self) -> bool:
        """ARP replies are answered even when Windows Firewall blocks everything else,
        so this sees the PC on even if SSH has been switched off."""
        c = self.config
        if not (c.check_arp and self._arping and c.pc_host):
            return False
        cmd = [self._arping, "-c", "2", "-w", "3"]
        if c.arp_interface:
            cmd += ["-I", c.arp_interface]
        cmd.append(c.pc_host)
        try:
            return subprocess.run(cmd, capture_output=True, timeout=6).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def check(self) -> tuple[bool, bool]:
        """Return (ssh_port_open, arp_ok). An open port isn't proof we can log in; see verify()."""
        port = self.ssh_port_open()
        return port, (True if port else self.arp_alive())

    # ---------- SSH ----------

    def host_key_known(self) -> bool:
        """True if data/known_hosts has an entry for this PC."""
        c = self.config
        if not c.known_hosts_path.exists():
            return False
        try:
            keys = paramiko.HostKeys(str(c.known_hosts_path))
        except Exception:  # noqa: BLE001 - unreadable file counts as missing
            return False
        name = c.pc_host if c.pc_ssh_port == 22 else f"[{c.pc_host}]:{c.pc_ssh_port}"
        return keys.lookup(name) is not None

    def run(self, *commands: str) -> Result:
        """Run one or more commands over a single SSH connection. Each is sent separately, so
        this works whether the PC's OpenSSH default shell is cmd.exe or PowerShell.
        Only the last command's exit code counts."""
        c = self.config
        if c.dry_run:
            for cmd in commands:
                log.info("DRY RUN, would run: %s", cmd)
            return True, "dry run"
        if not (c.pc_host and c.pc_ssh_user):
            return False, "PC_HOST / PC_SSH_USER not configured"
        if not c.ssh_trust_first_use and not self.host_key_known():
            return False, "PC's SSH host key isn't saved in data/known_hosts yet (see README)"
        client = paramiko.SSHClient()
        with contextlib.suppress(FileNotFoundError):
            client.load_host_keys(str(c.known_hosts_path))
        # Refuse any machine whose key doesn't match the saved one, unless trust-on-first-use is on
        policy = paramiko.AutoAddPolicy() if c.ssh_trust_first_use else paramiko.RejectPolicy()
        client.set_missing_host_key_policy(policy)
        try:
            kwargs: dict[str, Any] = {
                "hostname": c.pc_host,
                "port": c.pc_ssh_port,
                "username": c.pc_ssh_user,
                "timeout": 8,
                "banner_timeout": 8,
                "auth_timeout": 8,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if c.pc_ssh_password:
                kwargs["password"] = c.pc_ssh_password
            else:
                kwargs["key_filename"] = c.key_path
            client.connect(**kwargs)
            if c.ssh_trust_first_use:
                client.save_host_keys(str(c.known_hosts_path))
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

    def verify(self) -> Result:
        """Log in for real and run a harmless command, to prove shutdowns would work."""
        return self.run("whoami")

    def boot_time(self) -> float | None:
        """When Windows last started (Unix time), or None if it can't be read."""
        if self.config.dry_run:
            return None
        ok, out = self.run(
            "powershell -NoProfile -NonInteractive -Command "
            '"([DateTimeOffset](Get-CimInstance Win32_OperatingSystem).LastBootUpTime).ToUnixTimeSeconds()"'
        )
        try:
            return float(out.strip().splitlines()[-1]) if ok else None
        except (ValueError, IndexError):
            return None

    def schedule_shutdown(self, seconds: int, message: str) -> Result:
        """Replace any pending shutdown with one in `seconds`, showing `message` on screen.
        Windows shows the comment as a notification, and the shutdown still happens even if
        this server goes down afterwards."""
        seconds = max(0, min(int(seconds), 315360000))
        # "shutdown /a" fails harmlessly when nothing is pending
        return self.run("shutdown /a", f'shutdown /s /f /t {seconds} /c "{clean_message(message)}"')

    def send_message(self, message: str) -> Result:
        """Pop-up message without shutting down (msg.exe exists on Windows Pro/Edu, not Home)."""
        return self.run(f'msg * /TIME:120 "{clean_message(message)}"')
