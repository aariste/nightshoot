"""Persistent gphoto2 camera wrapper tuned for the Nikon Z50.

A single long-lived libgphoto2 session is held for the whole run. Spawning the
gphoto2 CLI once per frame makes the Z50 re-claim the USB interface every shot,
which is the main cause of "Could not claim the USB device" / PTP timeouts on a
multi-hour night sequence.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

import gphoto2 as gp

log = logging.getLogger("nightshoot.camera")

# Widget names the Nikon PTP driver exposes. Several have moved between
# libgphoto2 versions, so each setting is looked up by a list of candidates.
CONFIG_ALIASES = {
    "shutterspeed": ["shutterspeed", "shutterspeed2", "exptime"],
    "iso": ["iso", "isospeed"],
    "aperture": ["f-number", "aperture"],
    "imageformat": ["imageformat", "imagequality"],
    "capturetarget": ["capturetarget"],
    "bulb": ["bulb"],
    "exposuremode": ["expprogram", "autoexposuremode", "capturemode"],
    "batterylevel": ["batterylevel"],
    "focusmode": ["focusmode", "focusmode2"],
    "longexpnr": ["longexpnr", "longexposurenoisereduction"],
}

# Widget type ids vary between libgphoto2 builds, so map them by name.
WIDGET_TYPE_NAMES = {}
for _const, _label in (
    ("GP_WIDGET_WINDOW", "window"), ("GP_WIDGET_SECTION", "section"),
    ("GP_WIDGET_TEXT", "text"), ("GP_WIDGET_RANGE", "range"),
    ("GP_WIDGET_TOGGLE", "toggle"), ("GP_WIDGET_RADIO", "radio"),
    ("GP_WIDGET_MENU", "menu"), ("GP_WIDGET_BUTTON", "button"),
    ("GP_WIDGET_DATE", "date"),
):
    if hasattr(gp, _const):
        WIDGET_TYPE_NAMES[getattr(gp, _const)] = _label

# libgphoto2 error codes that mean "the link is gone, reconnect". Looked up
# defensively because the exact set of exported constants varies by version.
FATAL_ERRORS = {
    getattr(gp, name)
    for name in (
        "GP_ERROR_IO",
        "GP_ERROR_IO_USB_CLAIM",
        "GP_ERROR_IO_USB_FIND",
        "GP_ERROR_IO_READ",
        "GP_ERROR_IO_WRITE",
        "GP_ERROR_CAMERA_ERROR",
        "GP_ERROR_TIMEOUT",
        "GP_ERROR_MODEL_NOT_FOUND",
    )
    if hasattr(gp, name)
}


class CameraError(RuntimeError):
    pass


# --------------------------------------------------------------- value matching
# Bodies disagree wildly on how they spell the same setting. A Z50 reports
# shutter speeds as '20.0000s' and '0.0166s'; other bodies use '20' and '1/60'.
# Scripts should stay portable, so values are matched by meaning, not by string.

_NUMERIC_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(?:s|sec|secs|seconds|\")?\s*$", re.I)
_FRACTION_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*/\s*([0-9]*\.?[0-9]+)\s*(?:s|sec|\")?\s*$", re.I)

#: Cameras report durations truncated to four decimals, so 1/4000 (0.00025 s)
#: comes back as '0.0002s'.
TRUNCATION_DECIMALS = 4


def _truncate4(value: float) -> float:
    factor = 10 ** TRUNCATION_DECIMALS
    return int(value * factor) / factor


def _round4(value: float) -> float:
    return round(value, TRUNCATION_DECIMALS)


def parse_duration(text) -> float | None:
    """'20', '20.0000s', '1/60', '1/60s' -> seconds. None if not a duration."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    match = _FRACTION_RE.match(text)
    if match:
        denominator = float(match.group(2))
        return float(match.group(1)) / denominator if denominator else None
    match = _NUMERIC_RE.match(text)
    return float(match.group(1)) if match else None


def resolve_choice(key: str, value, choices: list[str]) -> str:
    """Map a human-written value onto the exact string this camera expects."""
    wanted = str(value).strip()
    if wanted in choices:
        return wanted

    lowered = {choice.lower(): choice for choice in choices}
    if wanted.lower() in lowered:
        return lowered[wanted.lower()]

    target = parse_duration(wanted)
    if target is not None and target > 0:
        numeric = [(choice, parse_duration(choice)) for choice in choices]
        numeric = [(choice, seconds) for choice, seconds in numeric
                   if seconds is not None and seconds > 0]
        if numeric:
            # Cameras report durations truncated to four decimals, so 1/4000
            # (0.00025 s) arrives as '0.0002s'. Applying the same truncation to
            # what the user asked for inverts that exactly, which beats
            # nearest-match: 0.00025 is equidistant from 0.0002 and 0.0003.
            for rounder in (_truncate4, _round4):
                reduced = rounder(target)
                for choice, seconds in numeric:
                    if abs(seconds - reduced) < 1e-9:
                        return choice

            best, best_error = None, None
            for choice, seconds in numeric:
                # Relative error, because shutter speeds span 1/8000 s to 30 s.
                error = abs(seconds - target) / target
                if best_error is None or error < best_error:
                    best, best_error = choice, error
            # 1% covers ordinary rounding without silently substituting a
            # genuinely different exposure.
            if best_error is not None and best_error <= 0.01:
                return best
            if best is not None:
                raise CameraError(
                    f"{key}: this camera has no setting equal to '{wanted}'. "
                    f"Closest is '{best}'."
                )

    raise CameraError(f"'{wanted}' is not valid for {key}. Options: {_summarise(choices)}")


def _summarise(choices: list[str], limit: int = 12) -> str:
    """Keep long option lists readable in a log line."""
    if len(choices) <= limit:
        return ", ".join(choices)
    head = ", ".join(choices[:limit // 2])
    tail = ", ".join(choices[-(limit // 2):])
    return f"{head}, … ({len(choices) - limit} more) …, {tail}"


@dataclass
class Shot:
    """One captured frame."""

    folder: str
    name: str
    started_at: float
    exposure_s: float
    saved_path: str | None = None
    thumb_path: str | None = None
    meta: dict = field(default_factory=dict)


class Camera:
    """Thread-safe facade over a single gphoto2.Camera instance."""

    def __init__(self, thumb_dir: str, download_dir: str | None = None):
        self._lock = threading.RLock()
        self._cam: gp.Camera | None = None
        self._model = "unknown"
        self._storage_cache: dict | None = None
        self._storage_at = 0.0
        self._snapshot_cache: dict | None = None
        self._can_trigger: bool | None = None
        self._shutter_s: float | None = None
        self._shutter_at = 0.0
        # Fetching a preview JPEG costs far more than a fast frame does. At high
        # cadence we deliberately skip most of them; the UI only needs to show
        # that something is happening.
        self.thumb_min_interval = 0.0
        self._last_thumb_at = 0.0
        self.thumb_dir = thumb_dir
        self.download_dir = download_dir
        os.makedirs(thumb_dir, exist_ok=True)
        if download_dir:
            os.makedirs(download_dir, exist_ok=True)

    # ---------------------------------------------------------------- lifecycle

    @property
    def model(self) -> str:
        return self._model

    @property
    def connected(self) -> bool:
        return self._cam is not None

    def connect(self, retries: int = 3) -> None:
        with self._lock:
            self.disconnect()
            last: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    cam = gp.Camera()
                    cam.init()
                    self._cam = cam
                    self._model = self._read_summary_model(cam)
                    log.info("connected to %s", self._model)
                    self._drain_events(500)
                    self._storage_at = 0.0
                    # Warm the cache so the first status read during an
                    # exposure still has something truthful to serve.
                    self.snapshot()
                    return
                except gp.GPhoto2Error as exc:
                    last = exc
                    log.warning("connect attempt %d/%d failed: %s", attempt, retries, exc)
                    time.sleep(2 * attempt)
            raise CameraError(
                f"no camera found ({last}). Check the USB cable, that the Z50 is "
                f"powered on, and that nothing else has claimed it."
            )

    def disconnect(self) -> None:
        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.exit()
                except gp.GPhoto2Error:
                    pass
                self._cam = None

    def _require(self) -> gp.Camera:
        if self._cam is None:
            raise CameraError("camera not connected")
        return self._cam

    @staticmethod
    def _read_summary_model(cam: gp.Camera) -> str:
        try:
            cfg = cam.get_config()
            for name in ("cameramodel", "model"):
                try:
                    return str(cfg.get_child_by_name(name).get_value())
                except gp.GPhoto2Error:
                    continue
            return str(cam.get_summary()).splitlines()[0]
        except gp.GPhoto2Error:
            return "unknown"

    def _reconnect_if_fatal(self, exc: gp.GPhoto2Error) -> None:
        if exc.code in FATAL_ERRORS:
            log.warning("fatal gphoto2 error %s, reconnecting", exc)
            try:
                self.connect()
            except CameraError as reconnect_exc:
                log.error("reconnect failed: %s", reconnect_exc)

    # ------------------------------------------------------------------ config

    def _find_widget(self, cfg, key: str):
        for name in CONFIG_ALIASES.get(key, [key]):
            try:
                return cfg.get_child_by_name(name)
            except gp.GPhoto2Error:
                continue
        return None

    def get_setting(self, key: str):
        with self._lock:
            cfg = self._require().get_config()
            widget = self._find_widget(cfg, key)
            return None if widget is None else widget.get_value()

    def get_choices(self, key: str) -> list[str]:
        with self._lock:
            cfg = self._require().get_config()
            widget = self._find_widget(cfg, key)
            if widget is None or widget.get_type() not in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                return []
            return [widget.get_choice(i) for i in range(widget.count_choices())]

    def storage(self, max_age: float = 30.0) -> dict | None:
        """Free space on the camera card.

        Card space is what actually ends a night — the Pi's disk is irrelevant
        unless downloading. Cached, and never allowed to block: a long exposure
        owns the camera lock for minutes at a time.
        """
        now = time.time()
        if self._storage_cache and now - self._storage_at < max_age:
            return self._storage_cache
        if self._cam is None or not self._lock.acquire(timeout=0.4):
            return self._storage_cache
        try:
            infos = self._cam.get_storageinfo()
        except (gp.GPhoto2Error, AttributeError):
            return self._storage_cache
        finally:
            self._lock.release()

        total_free = total_size = free_images = 0
        for info in infos:
            total_free += getattr(info, "freekbytes", 0) or 0
            total_size += getattr(info, "capacitykbytes", 0) or 0
            free_images += getattr(info, "freeimages", 0) or 0
        if not total_size:
            return self._storage_cache
        self._storage_cache = {
            "free_gb": round(total_free / 1048576, 1),
            "total_gb": round(total_size / 1048576, 1),
            "free_images": free_images or None,
        }
        self._storage_at = now
        return self._storage_cache

    def invalidate_storage(self) -> None:
        self._storage_at = 0.0

    def check_setting(self, key: str, value) -> None:
        """Validate a value without applying it. Raises CameraError if unusable."""
        with self._lock:
            cfg = self._require().get_config()
            widget = self._find_widget(cfg, key)
            if widget is None:
                raise CameraError(f"this camera does not expose '{key}'")
            if WIDGET_TYPE_NAMES.get(widget.get_type()) in ("radio", "menu"):
                choices = [widget.get_choice(i) for i in range(widget.count_choices())]
                resolve_choice(key, value, choices)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            cam = self._require()
            cfg = cam.get_config()
            widget = self._find_widget(cfg, key)
            if widget is None:
                raise CameraError(f"this camera does not expose '{key}'")
            if widget.get_readonly():
                raise CameraError(
                    f"'{key}' is read-only right now. On the Z50 that usually means "
                    f"the mode dial is not on M."
                )
            wtype = widget.get_type()
            kind = WIDGET_TYPE_NAMES.get(wtype)
            if kind == "toggle":
                widget.set_value(int(value))
            elif kind in ("radio", "menu"):
                choices = [widget.get_choice(i) for i in range(widget.count_choices())]
                widget.set_value(resolve_choice(key, value, choices))
            elif kind == "range":
                low, high, _step = widget.get_range()
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise CameraError(f"{key} expects a number, got {value!r}") from None
                if not low <= number <= high:
                    raise CameraError(f"{key} must be between {low} and {high}")
                widget.set_value(number)
            else:
                widget.set_value(str(value) if kind == "text" else value)
            cam.set_config(cfg)
            # Anything we just changed makes the cached reads stale.
            self._snapshot_cache = None
            if key in ("shutterspeed", "shutterspeed2", "exptime"):
                self._shutter_at = 0.0
            if key == "imageformat":
                self._storage_at = 0.0

    # --------------------------------------------------------------- live view

    def preview(self) -> bytes:
        """One live-view JPEG frame.

        The mirror/shutter is busy during an exposure, so callers must not ask
        for a preview while a capture is in flight — the shared lock serialises
        them, and a long bulb frame would otherwise block this for minutes.
        """
        with self._lock:
            cam = self._require()
            try:
                cam_file = cam.capture_preview()
                data = cam_file.get_data_and_size()
            except gp.GPhoto2Error as exc:
                self._reconnect_if_fatal(exc)
                raise CameraError(
                    f"live view failed: {exc}. On the Z50 make sure the lens cap is "
                    f"off, the camera is not in playback, and the mode dial is on M."
                ) from exc
            return bytes(memoryview(data))

    def set_liveview(self, enabled: bool) -> None:
        """Nikon bodies need the 'viewfinder' flag flipped to stream live view."""
        with self._lock:
            try:
                self.set_setting("viewfinder", 1 if enabled else 0)
            except CameraError:
                # Plenty of bodies start live view implicitly on capture_preview.
                log.debug("no 'viewfinder' widget; relying on implicit live view")

    def snapshot(self) -> dict:
        """Everything the UI wants to show, read in one config pass.

        Must never block: ``capture`` holds the camera lock for the whole
        exposure, so during a 4-minute bulb frame this would otherwise hang the
        status endpoint and the UI would look dead. When the camera is busy we
        serve the last known values and flag them.
        """
        if self._cam is None:
            return {"connected": False}
        if not self._lock.acquire(timeout=0.4):
            busy = dict(self._snapshot_cache or {"connected": True, "model": self._model})
            busy["busy"] = True
            return busy
        try:
            try:
                cfg = self._cam.get_config()
            except gp.GPhoto2Error as exc:
                self._reconnect_if_fatal(exc)
                return {"connected": False, "error": str(exc)}

            out: dict = {"connected": True, "model": self._model, "busy": False}
            for key in ("shutterspeed", "iso", "aperture", "imageformat",
                        "exposuremode", "batterylevel", "focusmode", "longexpnr"):
                widget = self._find_widget(cfg, key)
                if widget is None:
                    continue
                try:
                    out[key] = widget.get_value()
                except gp.GPhoto2Error:
                    continue
            out["supports_bulb"] = self._find_widget(cfg, "bulb") is not None
            # Lets the UI count down the frame in progress.
            out["shutter_seconds"] = parse_duration(out.get("shutterspeed"))
            self._snapshot_cache = out
            return out
        finally:
            self._lock.release()

    def shutter_seconds(self, max_age: float = 15.0) -> float | None:
        """Cached shutter duration.

        Reading the config is a PTP round trip, so at a fast cadence doing it per
        frame costs more than the exposure itself. Cached and invalidated
        whenever the shutter speed is actually changed.
        """
        now = time.time()
        if self._shutter_at and now - self._shutter_at < max_age:
            return self._shutter_s
        if self._cam is None or not self._lock.acquire(timeout=0.2):
            return self._shutter_s
        try:
            cfg = self._cam.get_config()
            widget = self._find_widget(cfg, "shutterspeed")
            self._shutter_s = parse_duration(widget.get_value()) if widget else None
            self._shutter_at = now
        except gp.GPhoto2Error:
            pass
        finally:
            self._lock.release()
        return self._shutter_s

    def prepare_for_night(self, capture_to_card: bool = True) -> list[str]:
        """Apply the settings a night sequence depends on. Returns warnings."""
        warnings: list[str] = []
        try:
            self.set_setting("capturetarget", "Memory card" if capture_to_card else "Internal RAM")
        except CameraError as exc:
            warnings.append(f"capturetarget: {exc}")

        mode = self.get_setting("exposuremode")
        if mode is not None and str(mode).upper() not in ("M", "MANUAL"):
            warnings.append(
                f"Camera is in '{mode}' mode. Set the Z50 mode dial to M or the "
                f"shutter/ISO/aperture controls will be locked."
            )
        if str(self.get_setting("longexpnr")).lower() in ("1", "on", "true"):
            warnings.append(
                "Long Exposure NR is on. The camera will be busy for as long as the "
                "exposure after every frame — turn it off for timelapse/star trails."
            )
        return warnings

    # ----------------------------------------------------------------- capture

    def begin_run(self, thumb_min_interval: float = 0.0) -> None:
        """Called once when a sequence starts."""
        self.thumb_min_interval = thumb_min_interval
        self._last_thumb_at = 0.0      # the first frame always gets a preview

    def capture(self, exposure_s: float | None = None, bulb: bool = False,
                download: bool = False, thumbnail: bool = True) -> Shot:
        """Take one frame. With bulb=True the shutter is held open by us."""
        with self._lock:
            started = time.time()
            try:
                if bulb:
                    path = self._capture_bulb(exposure_s or 1.0)
                else:
                    path = self._require().capture(gp.GP_CAPTURE_IMAGE)
            except gp.GPhoto2Error as exc:
                self._reconnect_if_fatal(exc)
                raise CameraError(f"capture failed: {exc}") from exc

            shot = Shot(
                folder=path.folder,
                name=path.name,
                started_at=started,
                exposure_s=exposure_s if exposure_s is not None else (time.time() - started),
            )
            if thumbnail and (time.time() - self._last_thumb_at) >= self.thumb_min_interval:
                shot.thumb_path = self._save_thumbnail(path)
                self._last_thumb_at = time.time()
            if download and self.download_dir:
                shot.saved_path = self._download(path)
            return shot

    def supports_trigger(self) -> bool:
        """Can this body fire the shutter without waiting for the file?"""
        if self._can_trigger is None:
            self._can_trigger = hasattr(self._cam, "trigger_capture")
        return bool(self._can_trigger)

    def trigger(self) -> None:
        """Fire the shutter and return immediately.

        ``capture()`` is synchronous: it waits for the exposure *and* for the
        camera to make the file available, which is a full PTP round trip per
        frame. For bursts we only need the shutter to fire; files are collected
        afterwards from the event queue.
        """
        with self._lock:
            cam = self._require()
            try:
                cam.trigger_capture()
            except gp.GPhoto2Error as exc:
                self._reconnect_if_fatal(exc)
                raise CameraError(f"trigger failed: {exc}") from exc

    def collect_new_files(self, timeout_ms: int = 1) -> list:
        """Drain any files the camera has finished writing. Never blocks long."""
        found = []
        if self._cam is None or not self._lock.acquire(timeout=0.2):
            return found
        try:
            while True:
                try:
                    etype, data = self._cam.wait_for_event(timeout_ms)
                except gp.GPhoto2Error:
                    return found
                if etype == gp.GP_EVENT_FILE_ADDED:
                    found.append(data)
                    continue
                return found
        finally:
            self._lock.release()

    def _capture_bulb(self, seconds: float):
        """Nikon bulb: toggle the 'bulb' action, then collect the new file."""
        cam = self._require()
        self._drain_events(200)
        self.set_setting("bulb", 1)
        try:
            deadline = time.time() + seconds
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.25))
        finally:
            self.set_setting("bulb", 0)

        # The file appears a moment after the shutter closes.
        end = time.time() + max(30.0, seconds * 0.5 + 15.0)
        while time.time() < end:
            etype, data = cam.wait_for_event(1000)
            if etype == gp.GP_EVENT_FILE_ADDED:
                return data
        raise CameraError(
            "bulb exposure finished but the camera never reported a new file. "
            "Check that shutter speed is set to Bulb and the card is not full."
        )

    def _drain_events(self, ms: int) -> None:
        cam = self._cam
        if cam is None:
            return
        end = time.time() + ms / 1000.0
        while time.time() < end:
            try:
                etype, _ = cam.wait_for_event(50)
            except gp.GPhoto2Error:
                return
            if etype == gp.GP_EVENT_TIMEOUT:
                return

    def _save_thumbnail(self, path) -> str | None:
        try:
            cam_file = self._require().file_get(path.folder, path.name, gp.GP_FILE_TYPE_PREVIEW)
            target = os.path.join(self.thumb_dir, "latest.jpg")
            tmp = target + ".part"
            cam_file.save(tmp)
            os.replace(tmp, target)
            return target
        except gp.GPhoto2Error as exc:
            log.debug("no thumbnail for %s: %s", path.name, exc)
            return None

    def _download(self, path) -> str:
        cam_file = self._require().file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
        target = os.path.join(self.download_dir, path.name)
        cam_file.save(target)
        log.info("downloaded %s", target)
        return target
