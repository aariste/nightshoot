"""Flask web UI + JSON API. Designed to be driven from a phone in the dark."""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import subprocess
import threading

from flask import Flask, jsonify, render_template, request, send_file

from .camera import Camera, CameraError
from .network import NetworkError, cancel_revert, client_is_on_hotspot
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


def _parse_until(value: str | None) -> float | None:
    """'05:30' -> unix ts of the next 05:30 local time."""
    if not value:
        return None
    hour, minute = (int(part) for part in value.split(":", 1))
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target.timestamp()


def _stop_liveview() -> None:
    """Live view holds the mirror/sensor busy; never leave it on during a run."""
    try:
        camera.set_liveview(False)
    except CameraError as exc:
        log.debug("could not disable live view: %s", exc)


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
    body = request.json or {}
    try:
        camera.begin_run(thumb_min_interval=0.0)   # a single frame always previews
        shot = camera.capture(
            exposure_s=float(body.get("exposure_s", 1)) if body.get("bulb") else None,
            bulb=bool(body.get("bulb")),
            download=False,
        )
    except CameraError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True, "name": shot.name, "at": shot.started_at,
                    "has_thumb": bool(shot.thumb_path)})


@app.post("/api/start")
def api_start():
    body = request.json or {}
    try:
        plan = Plan(
            frames=int(body.get("frames", 100)),
            exposure_s=float(body.get("exposure_s", 20)),
            interval_s=float(body.get("interval_s", 25)),
            start_delay_s=float(body.get("start_delay_s", 5)),
            bulb=bool(body.get("bulb", False)),
            download=bool(body.get("download", False)),
            until_ts=_parse_until(body.get("until")),
        )
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


@app.post("/api/network/hotspot")
def api_network_hotspot():
    body = request.json or {}
    if "enabled" not in body:
        return jsonify({"ok": False, "error": "'enabled' is required"}), 400
    revert = body.get("revert_after")
    try:
        revert = int(revert) if revert else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "revert_after must be a number of seconds"}), 400

    try:
        result = _set_hotspot(bool(body["enabled"]), revert_after=revert)
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


@app.post("/api/shutdown")
def api_shutdown():
    """Clean power-down so the SD card survives being unplugged in the field."""
    sequencer.stop()
    camera.disconnect()
    threading.Timer(
        1.0, lambda: subprocess.run(["/sbin/shutdown", "-h", "now"], check=False)
    ).start()
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
