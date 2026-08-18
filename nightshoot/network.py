"""Wi-Fi mode control: field hotspot vs joining a known network.

Every switch here cuts the network the web UI is being served over, so nothing
in this module may depend on the HTTP request surviving. Changes are handed to a
detached process, and an optional timer puts things back if the new mode turns
out to be unreachable.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shlex
import shutil
import subprocess

log = logging.getLogger("nightshoot.network")

AP_CON = os.environ.get("NIGHTSHOOT_AP", "nightshoot-ap")
REVERT_UNIT = "nightshoot-ap-revert"


class NetworkError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise NetworkError(f"{args[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetworkError(f"{args[0]} timed out") from exc


def _nmcli(*args: str, timeout: float = 15.0) -> str:
    result = _run(["nmcli", *args], timeout=timeout)
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "nmcli failed")
    return result.stdout


def _nmcli_quiet(*args: str) -> str:
    try:
        return _nmcli(*args)
    except NetworkError:
        return ""


def wifi_device() -> str | None:
    for line in _nmcli_quiet("-t", "-f", "DEVICE,TYPE", "device").splitlines():
        name, _, kind = line.partition(":")
        if kind == "wifi":
            return name
    return None


def ap_profile(include_secret: bool = False) -> dict | None:
    """SSID and address of the hotspot profile, if it exists.

    The pre-shared key is omitted unless explicitly asked for: it is a stored
    credential and should not be sprayed into every status poll.
    """
    if AP_CON not in _nmcli_quiet("-t", "-f", "NAME", "connection").split("\n"):
        return None
    addr = _nmcli_quiet("-g", "ipv4.address", "connection", "show", AP_CON).strip()
    profile = {
        "connection": AP_CON,
        "ssid": _nmcli_quiet("-g", "802-11-wireless.ssid", "connection", "show", AP_CON).strip(),
        "address": addr.split("/")[0] if addr else "",
    }
    if include_secret:
        profile["password"] = _nmcli_quiet(
            "-s", "-g", "802-11-wireless-security.psk",
            "connection", "show", AP_CON).strip()
    return profile


def saved_networks() -> list[str]:
    """Wi-Fi profiles other than the hotspot — what we can fall back to."""
    found = []
    for line in _nmcli_quiet("-t", "-f", "NAME,TYPE", "connection").splitlines():
        name, _, kind = line.rpartition(":")
        if kind == "802-11-wireless" and name and name != AP_CON:
            found.append(name)
    return found


def revert_pending() -> bool:
    result = _run(["systemctl", "is-active", f"{REVERT_UNIT}.timer"], timeout=5)
    return result.stdout.strip() == "active"


def status(include_secret: bool = False) -> dict:
    device = wifi_device()
    active = _nmcli_quiet("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
    current = None
    for line in active.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "802-11-wireless" and parts[2] == device:
            current = parts[0]
            break

    address = ""
    if device:
        raw = _nmcli_quiet("-t", "-f", "IP4.ADDRESS", "device", "show", device)
        first = raw.splitlines()[0] if raw.splitlines() else ""
        address = first.partition(":")[2].split("/")[0]

    profile = ap_profile(include_secret=include_secret)
    return {
        "device": device,
        "mode": "hotspot" if current == AP_CON else ("wifi" if current else "offline"),
        "connection": current,
        "address": address,
        "hotspot": profile,
        "hotspot_configured": profile is not None,
        "saved_networks": saved_networks(),
        "revert_pending": revert_pending(),
    }


def client_is_on_hotspot(remote_addr: str | None) -> bool:
    """Is the browser talking to us over the hotspot it is about to change?"""
    profile = ap_profile()
    if not profile or not profile["address"] or not remote_addr:
        return False
    try:
        network = ipaddress.ip_network(f"{profile['address']}/24", strict=False)
        return ipaddress.ip_address(remote_addr) in network
    except ValueError:
        return False


def _detach(argv: list[str], delay: float) -> None:
    """Run a command that must outlive this request and the connection it used.

    Arguments are quoted rather than interpolated: NetworkManager profile names
    are attacker-influenced in principle and this runs as root.
    """
    script = f"sleep {float(delay):.3f}; " + " ".join(shlex.quote(a) for a in argv)
    subprocess.Popen(
        ["/bin/sh", "-c", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _detach_all(commands: list[list[str]], delay: float) -> None:
    parts = [f"sleep {float(delay):.3f}"]
    parts += [" ".join(shlex.quote(a) for a in argv) for argv in commands]
    subprocess.Popen(
        ["/bin/sh", "-c", "; ".join(parts)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def cancel_revert() -> None:
    _run(["systemctl", "stop", f"{REVERT_UNIT}.timer"], timeout=10)
    _run(["systemctl", "reset-failed", REVERT_UNIT], timeout=10)


def _arm_revert(seconds: int, back_to: str | None) -> bool:
    """Schedule an automatic undo. Returns False if we could not arm one."""
    if not shutil.which("systemd-run"):
        log.warning("systemd-run is unavailable; cannot arm a revert timer")
        return False
    seconds = int(seconds)
    if seconds <= 0:
        return False
    cancel_revert()
    device = wifi_device() or ""
    undo = (["/usr/bin/nmcli", "connection", "up", back_to] if back_to
            else ["/usr/bin/nmcli", "device", "connect", device])
    script = "; ".join((
        " ".join(shlex.quote(a) for a in
                 ["/usr/bin/nmcli", "connection", "down", AP_CON]),
        " ".join(shlex.quote(a) for a in undo),
    ))
    result = _run(["systemd-run", "--quiet", f"--on-active={seconds}",
                   f"--unit={REVERT_UNIT}", "/bin/sh", "-c", script], timeout=10)
    if result.returncode != 0:
        log.error("could not arm the revert timer: %s",
                  (result.stderr or result.stdout).strip())
        return False
    return True


def set_hotspot(enabled: bool, revert_after: int | None = None,
                delay: float = 1.5) -> dict:
    """Switch Wi-Fi mode. Returns what was arranged; the switch happens after.

    ``delay`` gives the HTTP response time to reach the browser before the
    network it travelled over disappears.
    """
    profile = ap_profile()
    if profile is None:
        raise NetworkError(
            f"no '{AP_CON}' profile on this Pi — run install.sh to create it")
    device = wifi_device()
    if not device:
        raise NetworkError("no Wi-Fi device found")

    if enabled:
        # Fail fast on the usual blocker rather than silently doing nothing.
        if not (shutil.which("dnsmasq") or os.path.exists("/usr/sbin/dnsmasq")):
            raise NetworkError(
                "dnsmasq-base is not installed, so the hotspot cannot hand out "
                "addresses. Fix with: sudo apt install -y dnsmasq-base")
        previous = status()["connection"]
        armed = False
        if revert_after:
            armed = _arm_revert(revert_after,
                                previous if previous != AP_CON else None)
            # The safety net is the whole reason it is safe to do this
            # remotely. Switching without it could strand the Pi.
            if not armed:
                raise NetworkError(
                    "could not arm the automatic revert, so the hotspot was not "
                    "started — without it a failed switch could leave the Pi "
                    "unreachable. Choose 'no revert' to proceed anyway.")
        _detach(["/usr/bin/nmcli", "connection", "up", AP_CON], delay)
        return {"switching_to": "hotspot", "revert_armed": armed,
                "revert_after": revert_after if armed else None,
                "ssid": profile["ssid"], "address": profile["address"]}

    if not saved_networks():
        raise NetworkError(
            "there are no other saved Wi-Fi networks, so turning the hotspot "
            "off would leave the Pi unreachable")
    cancel_revert()
    _detach_all([
        ["/usr/bin/nmcli", "connection", "down", AP_CON],
        ["/usr/bin/nmcli", "device", "connect", device],
    ], delay)
    return {"switching_to": "wifi", "revert_armed": False, "revert_after": None}
