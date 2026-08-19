"""Flask web UI + JSON API. Designed to be driven from a phone in the dark."""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import re
import shutil
import subprocess
import threading

from flask import Flask, jsonify, render_template, request, send_file

from . import system
from .camera import Camera, CameraError
from .network import NetworkError, ap_profile, cancel_revert, client_is_on_hotspot
from .network import set_hotspot as _set_hotspot
from .network import status as network_status
from .scripts import ScriptError, list_scripts, load_script, save_script
from .sequencer import Plan, Sequencer

log = logging.getLogger("nightshoot.app")

STATE_DIR = os.environ.get("NIGHTSHOOT_STATE", "/var/lib/nightshoot")
THUMB_DIR = os.path.join(STATE_DIR, "thumbs")
DOWNLOAD_DIR = os.path.join(STATE_DIR, "captures")
SCRIPT_DIR = os.environ.get("NIGHTSHOOT_SCRIPTS", os.path.join(STATE_DIR, "scripts"))

camera = Camera(thumb_dir=THUMB_DIR, download_dir=DOWNLOAD_DIR)
sequencer = Sequencer(camera)
_connect_lock = threading.Lock()

app = Flask(__name__)
# Scripts are a few kB of YAML. Anything larger is a mistake or an attack.
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024

#: Optional shared secret. When set, every request must carry it as
#: 'X-NightShoot-Token' or '?token='. Off by default: on a WPA2 field hotspot
#: it adds friction for no gain, but it matters the moment the Pi shares a
#: network with anything you do not control.
AUTH_TOKEN = os.environ.get("NIGHTSHOOT_TOKEN", "").strip()


@app.before_request
def _guard_request():
    """Two cheap protections that do not get in a legitimate user's way.

    1. Origin checking. Without it any web page you happen to open while on the
       Pi's network could POST to /api/shutdown — no preflight is required for a
       request that sends no body, so the browser would simply send it.
    2. An optional shared token, for when the Pi is not on a private network.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        pass
    else:
        origin = request.headers.get("Origin")
        if origin:
            allowed = {request.host_url.rstrip("/")}
            # host_url reflects the address actually used, so both
            # nightshoot.local and 192.168.7.1 work without configuration.
            if origin.rstrip("/") not in allowed:
                log.warning("rejected cross-origin %s from %s", request.path, origin)
                return jsonify({
                    "ok": False,
                    "error": "cross-origin requests are not accepted",
                }), 403

    if AUTH_TOKEN:
        supplied = (request.headers.get("X-NightShoot-Token")
                    or request.args.get("token", ""))
        if not hmac.compare_digest(supplied, AUTH_TOKEN):
            return jsonify({"ok": False, "error": "invalid or missing token"}), 401
    return None


def _try_connect(quiet: bool = True) -> str | None:
    with _connect_lock:
        if camera.connected:
            return None
        try:
            camera.connect()
            return None
        except CameraError as exc:
            if not quiet:
                log.warning("connect failed: %s", exc)
            return str(exc)


def _parse_until(value) -> float | None:
    """'05:30' -> unix ts of the next 05:30 local time."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BadRequest("'until' must be a time like \"05:30\"")
    match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", value)
    if not match:
        raise BadRequest(f"'until' must be a 24-hour time like \"05:30\", got {value!r}")
    now = dt.datetime.now()
    target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                         second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target.timestamp()


def _stop_liveview() -> None:
    """Live view holds the mirror/sensor busy; never leave it on during a run."""
    try:
        camera.set_liveview(False)
    except CameraError as exc:
        log.debug("could not disable live view: %s", exc)


class BadRequest(ValueError):
    """A client sent a value we cannot use. Always answered with 400."""


def _number(body: dict, key: str, default, minimum=None, maximum=None):
    """Read a JSON number without letting str/list/None reach float()."""
    value = body.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BadRequest(f"'{key}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{key}' must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise BadRequest(f"'{key}' must be a finite number")
    if minimum is not None and number < minimum:
        raise BadRequest(f"'{key}' must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise BadRequest(f"'{key}' must be at most {maximum}")
    return number


def _integer(body: dict, key: str, default, minimum=None, maximum=None) -> int:
    number = _number(body, key, default, minimum, maximum)
    if float(number) != int(number):
        raise BadRequest(f"'{key}' must be a whole number")
    return int(number)


def _flag(body: dict, key: str, default: bool = False) -> bool:
    """Strict booleans: bool('false') is True, which is not what a user means."""
    value = body.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    raise BadRequest(f"'{key}' must be true or false, got {value!r}")


def _body() -> dict:
    """The JSON body as a mapping, or 400 if it is anything else."""
    try:
        data = request.get_json(silent=True)
    except Exception:  # noqa: BLE001 - malformed input must not 500
        raise BadRequest("body must be JSON") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    return data


@app.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    error = None
    if not camera.connected and not sequencer.running:
        error = _try_connect()
    usage = shutil.disk_usage(STATE_DIR)
    return jsonify(
        {
            "camera": camera.snapshot(),
            "camera_error": error,
            "card": camera.storage() if camera.connected else None,
            "sequence": sequencer.status(),
            "disk_free_gb": round(usage.free / 1e9, 1),
            "now": dt.datetime.now().strftime("%H:%M:%S"),
        }
    )


@app.post("/api/connect")
def api_connect():
    camera.disconnect()
    error = _try_connect(quiet=False)
    if error:
        return jsonify({"ok": False, "error": error}), 503
    return jsonify({"ok": True, "camera": camera.snapshot()})


@app.get("/api/choices/<key>")
def api_choices(key: str):
    try:
        return jsonify({"ok": True, "choices": camera.get_choices(key)})
    except CameraError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.post("/api/settings")
def api_settings():
    if sequencer.running:
        return jsonify({"ok": False, "error": "stop the sequence before changing settings"}), 409
    applied, failed = [], {}
    for key, value in (request.json or {}).items():
        if value in (None, ""):
            continue
        try:
            camera.set_setting(key, value)
            applied.append(key)
        except CameraError as exc:
            failed[key] = str(exc)
    return jsonify({"ok": not failed, "applied": applied, "failed": failed,
                    "camera": camera.snapshot()})


@app.post("/api/test-shot")
def api_test_shot():
    if sequencer.running:
        return jsonify({"ok": False, "error": "sequence is running"}), 409
    body = _body()
    bulb = _flag(body, "bulb")
    exposure = _number(body, "exposure_s", 1, minimum=0.1, maximum=3600) if bulb else None
    try:
        camera.begin_run(thumb_min_interval=0.0)   # a single frame always previews
        shot = camera.capture(exposure_s=exposure, bulb=bulb, download=False)
    except CameraError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True, "name": shot.name, "at": shot.started_at,
                    "has_thumb": bool(shot.thumb_path)})


@app.post("/api/start")
def api_start():
    body = _body()
    plan = Plan(
        frames=_integer(body, "frames", 100, minimum=0, maximum=1_000_000),
        exposure_s=_number(body, "exposure_s", 20, minimum=0, maximum=86_400),
        interval_s=_number(body, "interval_s", 25, minimum=0, maximum=86_400),
        start_delay_s=_number(body, "start_delay_s", 5, minimum=0, maximum=86_400),
        bulb=_flag(body, "bulb"),
        download=_flag(body, "download"),
        until_ts=_parse_until(body.get("until")),
    )
    try:
        _stop_liveview()
        sequencer.start(plan)
    except (ValueError, RuntimeError, CameraError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "sequence": sequencer.status()})


@app.post("/api/stop")
def api_stop():
    sequencer.stop()
    return jsonify({"ok": True})


@app.get("/api/scripts")
def api_scripts():
    """Every script sitting in the scripts folder, parse errors included."""
    return jsonify({"ok": True, "dir": SCRIPT_DIR, "scripts": list_scripts(SCRIPT_DIR)})


@app.get("/api/scripts/<path:filename>")
def api_script_detail(filename: str):
    try:
        script = load_script(SCRIPT_DIR, filename)
    except ScriptError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, **script.summary(), "source": script.source})


@app.post("/api/scripts/upload")
def api_script_upload():
    """Accept a .yaml file (or pasted text) and store it if it parses.

    A script that fails validation is never written, so the folder cannot fill
    up with files that will only fail hours later in a field.
    """
    uploads: list[tuple[str, str | bytes]] = []
    overwrite = False

    if request.files:
        overwrite = str(request.form.get("overwrite", "")).lower() in ("1", "true", "yes")
        for storage in request.files.getlist("file"):
            if storage and storage.filename:
                uploads.append((storage.filename, storage.read()))
    else:
        body = request.json or {}
        overwrite = bool(body.get("overwrite"))
        if body.get("source") is not None:
            uploads.append((body.get("filename") or "", body.get("source")))

    if not uploads:
        return jsonify({"ok": False, "error": "no file received"}), 400

    saved, failed, conflicts = [], {}, []
    for filename, text in uploads:
        try:
            script = save_script(SCRIPT_DIR, filename, text, overwrite=overwrite)
            saved.append(script.summary())
            log.info("uploaded script %s", script.filename)
        except FileExistsError as exc:
            conflicts.append(str(exc))
        except ScriptError as exc:
            failed[os.path.basename(filename or "?")] = str(exc)
        except OSError as exc:
            failed[os.path.basename(filename or "?")] = f"could not save: {exc}"

    status = 200 if saved and not failed and not conflicts else 400
    if conflicts and not failed and not saved:
        status = 409
    return jsonify({"ok": bool(saved) and not failed and not conflicts,
                    "saved": saved, "failed": failed, "conflicts": conflicts}), status


@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"ok": False, "error": "that file is too large for a script"}), 413


@app.post("/api/scripts/run")
def api_script_run():
    filename = (request.json or {}).get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "no script selected"}), 400
    try:
        script = load_script(SCRIPT_DIR, filename)
        _stop_liveview()
        sequencer.start_script(script)
    except (ScriptError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except (RuntimeError, CameraError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True, "sequence": sequencer.status()})


@app.post("/api/pause")
def api_pause():
    sequencer.pause()
    return jsonify({"ok": True})


@app.post("/api/resume")
def api_resume():
    sequencer.resume()
    return jsonify({"ok": True})


@app.get("/thumb.jpg")
def thumb():
    path = os.path.join(THUMB_DIR, "latest.jpg")
    if not os.path.exists(path):
        return "", 404
    response = send_file(path, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/preview.jpg")
def preview():
    """One live-view frame. Refused while the shutter is busy."""
    if sequencer.running:
        return "", 409
    if not camera.connected:
        return "", 503
    try:
        frame = camera.preview()
    except CameraError as exc:
        return str(exc), 503
    response = app.response_class(frame, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/liveview")
def api_liveview():
    enabled = bool((request.json or {}).get("enabled"))
    if enabled and sequencer.running:
        return jsonify({"ok": False,
                        "error": "stop the sequence before using live view"}), 409
    if not camera.connected:
        return jsonify({"ok": False, "error": "camera not connected"}), 503
    try:
        camera.set_liveview(enabled)
    except CameraError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True, "enabled": enabled})


@app.get("/api/network")
def api_network():
    """Current Wi-Fi mode, plus whether the caller would cut itself off."""
    try:
        state = network_status()
    except NetworkError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    state["you_are_on_hotspot"] = client_is_on_hotspot(request.remote_addr)
    state["sequence_running"] = sequencer.running
    return jsonify({"ok": True, **state})


@app.get("/api/network/hotspot-password")
def api_hotspot_password():
    """The hotspot PSK, on request only.

    Kept out of the polled status so a stored credential is not repeated in
    every response and every proxy log.
    """
    try:
        profile = ap_profile(include_secret=True)
    except NetworkError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    if not profile:
        return jsonify({"ok": False, "error": "no hotspot profile"}), 404
    return jsonify({"ok": True, "ssid": profile["ssid"],
                    "password": profile.get("password", "")})


@app.post("/api/network/hotspot")
def api_network_hotspot():
    body = _body()
    if "enabled" not in body:
        return jsonify({"ok": False, "error": "'enabled' is required"}), 400
    enabled = _flag(body, "enabled")
    # 0/None means "no safety net"; anything else must be a sane duration.
    revert = body.get("revert_after")
    revert = None if revert in (None, "", 0, "0") else _integer(
        body, "revert_after", 600, minimum=30, maximum=86_400)

    try:
        result = _set_hotspot(enabled, revert_after=revert)
    except NetworkError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    log.info("network switch requested: %s", result)
    # The capture thread is unaffected: it talks to the camera over USB.
    return jsonify({"ok": True, **result})


@app.post("/api/network/cancel-revert")
def api_network_cancel_revert():
    try:
        cancel_revert()
    except NetworkError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True})


@app.get("/api/system")
def api_system():
    """Host health: temperature, uptime, memory, throttling, disk."""
    return jsonify({"ok": True, **system.summary(STATE_DIR)})


@app.get("/api/logs")
def api_logs():
    """Recent service log, so a failed night can be diagnosed from the phone."""
    lines = max(10, min(500, int(request.args.get("lines", 120) or 120)))
    try:
        result = subprocess.run(
            ["journalctl", "-u", "nightshoot", "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=10)
        text = result.stdout or result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        text = f"could not read the journal: {exc}"
    return jsonify({"ok": True, "text": text})


@app.post("/api/restart")
def api_restart():
    """Restart the service without rebooting — the usual fix for a wedged USB."""
    if sequencer.running:
        return jsonify({"ok": False,
                        "error": "stop the sequence before restarting"}), 409
    sequencer.stop()
    camera.disconnect()
    threading.Timer(0.5, system.restart_service).start()
    return jsonify({"ok": True})


@app.post("/api/reboot")
def api_reboot():
    if sequencer.running:
        return jsonify({"ok": False,
                        "error": "stop the sequence before rebooting"}), 409
    sequencer.stop()
    camera.disconnect()
    threading.Timer(1.0, system.reboot).start()
    return jsonify({"ok": True})


@app.post("/api/shutdown")
def api_shutdown():
    """Clean power-down so the SD card survives being unplugged in the field."""
    sequencer.stop()
    camera.disconnect()
    threading.Timer(1.0, system.shutdown).start()
    return jsonify({"ok": True})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    _try_connect()
    app.run(host="0.0.0.0", port=int(os.environ.get("NIGHTSHOOT_PORT", 8080)),
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
