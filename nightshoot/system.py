"""Host facts for the Admin panel.

Everything here answers a question you would otherwise SSH in to ask: is the Pi
overheating, is the card filling up, has it been up all night, what version am I
actually running. Each reader degrades to None rather than raising, because a
missing sysfs file must never break the panel.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time

log = logging.getLogger("nightshoot.system")

THROTTLE_FLAGS = (
    (0, "under-voltage now"),
    (1, "CPU frequency capped now"),
    (2, "throttled now"),
    (3, "soft temperature limit now"),
    (16, "under-voltage since boot"),
    (17, "CPU frequency capped since boot"),
    (18, "throttled since boot"),
    (19, "soft temperature limit since boot"),
)


def _read(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def cpu_temperature() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    # Some kernels report millidegrees, others degrees.
    return round(value / 1000 if value > 200 else value, 1)


def uptime_seconds() -> float | None:
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def load_average() -> list[float] | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def memory() -> dict | None:
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    values = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                values[key] = int(parts[0])
            except ValueError:
                continue
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total:
        return None
    return {
        "total_mb": round(total / 1024),
        "available_mb": round(available / 1024) if available else None,
        "used_percent": round(100 * (1 - available / total)) if available else None,
    }


def throttling() -> dict | None:
    """Under-voltage and thermal throttling, the usual cause of odd Pi faults."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or "=" not in result.stdout:
        return None
    try:
        bits = int(result.stdout.split("=")[1].strip(), 0)
    except ValueError:
        return None
    active = [label for bit, label in THROTTLE_FLAGS if bits & (1 << bit)]
    return {"raw": hex(bits), "flags": active, "ok": bits == 0}


def disk(path: str) -> dict | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "free_gb": round(usage.free / 1e9, 1),
        "total_gb": round(usage.total / 1e9, 1),
        "used_percent": round(100 * usage.used / usage.total) if usage.total else None,
    }


def service_since() -> float | None:
    """When this process started, so you can tell a restart from an uptime."""
    raw = _read(f"/proc/{os.getpid()}/stat")
    boot = uptime_seconds()
    if not raw or boot is None:
        return None
    try:
        # Field 22 is starttime in clock ticks since boot.
        after_comm = raw.rsplit(")", 1)[1].split()
        ticks = float(after_comm[19])
        hz = os.sysconf("SC_CLK_TCK")
        return time.time() - (boot - ticks / hz)
    except (IndexError, ValueError, OSError, AttributeError):
        return None


def version() -> str:
    from nightshoot import __version__
    return __version__


def summary(state_dir: str) -> dict:
    """Everything the Admin panel shows about the host."""
    return {
        "hostname": socket.gethostname(),
        "version": version(),
        "cpu_temp_c": cpu_temperature(),
        "uptime_s": uptime_seconds(),
        "service_since": service_since(),
        "load": load_average(),
        "memory": memory(),
        "throttling": throttling(),
        "disk": disk(state_dir),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": time.strftime("%Z"),
    }


def reboot() -> None:
    subprocess.Popen(["/sbin/reboot"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def shutdown() -> None:
    subprocess.Popen(["/sbin/shutdown", "-h", "now"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def restart_service() -> None:
    subprocess.Popen(["systemctl", "restart", "nightshoot"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
