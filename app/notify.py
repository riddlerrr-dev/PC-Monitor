"""Phone notifications via ntfy (https://ntfy.sh, free iOS/Android app)."""
import logging
import urllib.request

log = logging.getLogger("pcmonitor.notify")

PRIORITY = {
    "power_on_outside": "high",
    "ssh_unreachable": "high",
    "shutdown_failed": "high",
}


def send(settings: dict, event_type: str, title: str, message: str, force: bool = False) -> tuple[bool, str]:
    n = settings.get("notify") or {}
    url = (n.get("ntfy_url") or "").strip()
    if not url:
        return False, "No ntfy URL configured"
    if not force and event_type not in (n.get("events") or []):
        return False, "Event type not enabled"
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": PRIORITY.get(event_type, "default"),
        "Tags": "computer",
    }
    if n.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {n['ntfy_token']}"
    req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy failed: %s", e)
        return False, str(e)
