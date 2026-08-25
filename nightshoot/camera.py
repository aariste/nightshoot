"""Persistent gphoto2 camera wrapper for Nikon bodies.

A single long-lived libgphoto2 session is held for the whole run. Spawning the
gphoto2 CLI once per frame makes the camera re-claim the USB interface every
shot, which is the main cause of "Could not claim the USB device" / PTP timeouts
on a multi-hour night sequence.

Scope is deliberately Nikon. Other vendors present genuinely different shapes —
Canon holds the shutter open with a two-step remote-release protocol rather than
a bulb toggle, for instance — and supporting them without hardware to test on
would mean shipping guesses. Nikon bodies still vary among themselves, so widget
names are looked up through candidate lists and values are matched by meaning
rather than exact spelling.
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

# Nikon's own widget names have moved between libgphoto2 versions and differ
# across bodies, so each setting is looked up by a list of candidates.
CONFIG_ALIASES = {
    "shutterspeed": ["shutterspeed", "shutterspeed2", "exptime"],
    "iso": ["iso", "isospeed"],
    "aperture": ["f-number"],
    "imageformat": ["imageformat", "imagequality"],
    "capturetarget": ["capturetarget"],
    "bulb": ["bulb"],
    "exposuremode": ["expprogram", "autoexposuremode", "capturemode"],
    "batterylevel": ["batterylevel"],
    "focusmode": ["focusmode", "focusmode2"],
    "longexpnr": ["longexpnr", "longexposurenoisereduction"],
    "highisonr": ["highisonr", "highisonoisereduction", "noisereduction"],
    "viewfinder": ["viewfinder"],
    # The drive/release mode (PTP StillCaptureMode). Distinct from
    # "exposuremode" above, which is the P/S/A/M dial.
    "releasemode": ["capturemode", "stillcapturemode"],
    "burstnumber": ["burstnumber"],
}

#: Preference order for the drive mode used during a burst, matched against
#: whatever the body actually offers.
#:
#: The awkward part is that libgphoto2 does not label Nikon's vendor values on Z
#: bodies: Continuous High shows up as "Unknown value 8011" rather than by name,
#: so matching on words alone finds Continuous Low (0x8010, roughly half the
#: rate) or the generic PTP "Burst" and stops there. The raw values are matched
#: too, ahead of those, because on a Z body they are the fast one.
#:
#: Each entry is a tuple of substrings that must all appear in the choice,
#: lowercased.
BURST_RELEASE_PREFERENCE = (
    ("continuous", "high"),   # labelled properly on some bodies
    ("8011",),                # Nikon Z: Continuous High, left unlabelled
    ("burst",),               # generic PTP burst; what libgphoto2 pairs with
                              # BurstNumber in its own Nikon capture path
    ("continuous",),          # Continuous Low: better than single shot
)

#: Settings that cost time per frame and nothing else, turned off for the
#: duration of a burst and put back afterwards. Values are the substrings that
#: identify an "off" choice, tried in order; a toggle widget just gets 0.
BURST_SPEED_OVERRIDES = (
    # Noise reduction runs after each frame and blocks the next one. Long
    # Exposure NR can double the cycle time on its own.
    ("longexpnr", ("off",), 0),
    ("highisonr", ("off",), 0),
    # Live view competes for the same USB pipe and the same processor the
    # camera needs for shooting and writing.
    ("viewfinder", (), 0),
)

#: Set NIGHTSHOOT_ALLOW_ANY=1 to try a non-Nikon body anyway. Nothing is
#: blocked beyond the warning; it simply will not have been tested.
ALLOW_ANY_VENDOR = os.environ.get("NIGHTSHOOT_ALLOW_ANY", "").lower() in ("1", "true", "yes")

#: Nikon bodies often report a model with no manufacturer in it — a Z50 says
#: just "Z 50" — so recognise the naming schemes as well as the word "nikon".
#:   Z 50, Z6_2, Z fc, D5300, D850, COOLPIX P1000, Df, 1 J5
NIKON_MODEL_RE = re.compile(
    r"^\s*(nikon\b|z\s*\d|z\s*f|d\d{1,4}\b|df\b|coolpix\b|1\s+[jvs]\d)",
    re.IGNORECASE,
)

#: Other vendors, used only to decide whether we are *confident* the body is not
#: a Nikon. An unrecognised name is left alone rather than warned about.
OTHER_VENDOR_RE = re.compile(
    r"\b(canon|sony|fujifilm|fuji|olympus|om system|panasonic|lumix|pentax|"
    r"ricoh|leica|sigma|hasselblad|gopro|kodak|casio|samsung)\b",
    re.IGNORECASE,
)

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
        self._bulb_supported: bool | None = None
        self._burst_limit: int | None = None
        self._burst_probed = False
        self._burst_number = 1
        self._release_mode_saved = None
        self._release_mode_applied = None
        self._speed_saved: dict = {}
        self._manufacturer = ""
        self._vendor_warning: str | None = None
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
                    self._manufacturer = self._read_manufacturer()
                    self._bulb_supported = None
                    self._can_trigger = None
                    self._burst_limit = None
                    self._burst_probed = False
                    # A fresh session starts at the camera's own setting, and
                    # we have not touched it yet.
                    self._burst_number = 1
                    self._release_mode_saved = None
                    self._release_mode_applied = None
                    # A new session cannot restore what the old one saved.
                    self._speed_saved = {}
                    self._check_vendor()
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
                f"no camera found ({last}). Check the USB cable, that the camera "
                f"is powered on, and that nothing else has claimed it."
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

    def _check_vendor(self) -> None:
        """Warn only when this is confidently *not* a Nikon.

        Model strings are unreliable — a Z50 reports itself as 'Z 50', with no
        manufacturer at all — so an unrecognised name is left alone. Crying wolf
        at the camera the tool was built for is worse than staying quiet.
        """
        self._vendor_warning = None
        if self.is_nikon:
            return
        identity = f"{self._manufacturer} {self._model}".strip()
        other = OTHER_VENDOR_RE.search(identity)
        if not other:
            log.info("could not identify the vendor of '%s'; assuming Nikon", self._model)
            return

        message = (
            f"'{self._model}' looks like a {other.group(1)} body. NightShoot is "
            f"built and tested for Nikon only; bulb, live view and burst may not "
            f"work here."
        )
        self._vendor_warning = message
        if ALLOW_ANY_VENDOR:
            log.warning("%s Continuing because NIGHTSHOOT_ALLOW_ANY is set.", message)
        else:
            log.warning("%s Set NIGHTSHOOT_ALLOW_ANY=1 to silence this.", message)

    @staticmethod
    def _read_summary_model(cam: gp.Camera) -> str:
        """A human-readable model name, preferring one that names the vendor.

        Bodies report themselves inconsistently: a Z50's 'cameramodel' widget
        says just 'Z 50'. libgphoto2's own abilities carry the driver name,
        which does include the manufacturer ('Nikon Z 50'), so prefer that.
        """
        try:
            abilities = cam.get_abilities()
            name = str(getattr(abilities, "model", "") or "").strip()
            if name:
                return name
        except (gp.GPhoto2Error, AttributeError):
            pass
        try:
            cfg = cam.get_config()
            for widget in ("cameramodel", "model"):
                try:
                    return str(cfg.get_child_by_name(widget).get_value())
                except gp.GPhoto2Error:
                    continue
            return str(cam.get_summary()).splitlines()[0]
        except gp.GPhoto2Error:
            return "unknown"

    def _read_manufacturer(self) -> str:
        """The vendor string, when the body publishes one."""
        try:
            cfg = self._require().get_config()
        except (gp.GPhoto2Error, CameraError):
            return ""
        for widget in ("manufacturer", "vendorextension", "make"):
            try:
                return str(cfg.get_child_by_name(widget).get_value()).strip()
            except gp.GPhoto2Error:
                continue
        return ""

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
                    f"'{key}' is read-only right now. That usually means the "
                    f"mode dial is not on M, or the camera is in playback."
                )
            wtype = widget.get_type()
            kind = WIDGET_TYPE_NAMES.get(wtype)
            if kind == "toggle":
                widget.set_value(int(value))
            elif kind in ("radio", "menu"):
                choices = [widget.get_choice(i) for i in range(widget.count_choices())]
                value = resolve_choice(key, value, choices)
                widget.set_value(value)
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
                # We know exactly what was written, so the cache can be updated
                # in place rather than invalidated — the capture path then
                # never has to re-read it over PTP.
                self._shutter_s = parse_duration(value)
                self._shutter_at = time.time()
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
                    f"live view failed: {exc}. Check that the lens cap is off, "
                    f"the camera is not in playback, and the mode dial is on M."
                ) from exc
            return bytes(memoryview(data))

    def set_liveview(self, enabled: bool) -> None:
        """Flip whichever live-view flag this body exposes.

        Nikon and most PTP bodies call it 'viewfinder'; some Canon models use
        'eosviewfinder'. Both are covered by the alias list.
        """
        with self._lock:
            try:
                self.set_setting("viewfinder", 1 if enabled else 0)
            except CameraError:
                # Plenty of bodies start live view implicitly on capture_preview.
                log.debug("no live-view widget; relying on implicit live view")

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
            out["supports_bulb"] = self.supports_bulb()
            out["is_nikon"] = self.is_nikon
            out["vendor_warning"] = self._vendor_warning
            # Lets the UI count down the frame in progress.
            out["shutter_seconds"] = parse_duration(out.get("shutterspeed"))
            # The UI polls this endpoint anyway, so the shutter-duration cache
            # stays warm for free — the capture path must never pay for a
            # config read of its own.
            self._shutter_s = out["shutter_seconds"]
            self._shutter_at = time.time()
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

    def shutter_seconds_cached(self) -> float | None:
        """Last known shutter duration, without touching the camera.

        For the moment between an interval slot opening and the shutter firing:
        a PTP config read there would jitter the actual shot time by however
        long the read takes.
        """
        return self._shutter_s

    def prepare_for_night(self, capture_to_card: bool = True) -> list[str]:
        """Apply the settings a night sequence depends on. Returns warnings."""
        warnings: list[str] = []

        # A previous run that died mid-burst may have left BurstNumber high,
        # which would silently turn every frame of this sequence into a burst.
        # Assume nothing about what the last session left behind.
        if self.burst_number_limit():
            self._burst_number = 0      # force the write even if we think it's 1
            self.reset_burst_number()

        # Vendors name these differently — Nikon says "Memory card", Canon may
        # offer "card" or "SDRAM" — so pick from what this body actually lists
        # rather than asserting one spelling.
        targets = self.get_choices("capturetarget")
        if targets:
            wanted = ["Memory card", "card", "Card"] if capture_to_card \
                else ["Internal RAM", "SDRAM", "RAM"]
            for candidate in wanted:
                match = next((t for t in targets
                              if candidate.lower() in t.lower()), None)
                if match:
                    try:
                        self.set_setting("capturetarget", match)
                    except CameraError as exc:
                        warnings.append(f"capturetarget: {exc}")
                    break

        mode = self.get_setting("exposuremode")
        if mode is not None and str(mode).upper() not in ("M", "MANUAL"):
            warnings.append(
                f"Camera is in '{mode}' mode. Set the mode dial to M, or the "
                f"shutter, ISO and aperture controls will be locked."
            )
        if str(self.get_setting("longexpnr")).lower() in ("1", "on", "true"):
            warnings.append(
                "Long Exposure NR is on. The camera will be busy for as long as the "
                "exposure after every frame — turn it off for timelapse/star trails."
            )
        return warnings

    def capabilities(self) -> dict:
        """What this particular body supports, for the UI and troubleshooting."""
        return {
            "model": self._model,
            "is_nikon": self.is_nikon,
            "vendor_warning": self._vendor_warning,
            "bulb": self.supports_bulb(),
            "trigger": self.supports_trigger(),
            "liveview": self._find_widget(self._require().get_config(), "viewfinder") is not None,
            "storage": self.storage() is not None,
        }

    @property
    def is_nikon(self) -> bool:
        """True when the manufacturer or the model naming says Nikon."""
        if "nikon" in (self._manufacturer or "").lower():
            return True
        return bool(NIKON_MODEL_RE.match(self._model or ""))

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

        On Nikon "immediately" is relative. libgphoto2's Nikon path brackets
        every capture with ``nikon_wait_busy``, which polls the body's
        DeviceReady opcode every 100 ms until it goes idle — so one call costs
        at least one poll interval, and a per-frame loop cannot beat roughly
        one frame per 100 ms no matter how short the exposure. See
        ``burst_number_limit`` for the way around that.
        """
        with self._lock:
            cam = self._require()
            try:
                cam.trigger_capture()
            except gp.GPhoto2Error as exc:
                self._reconnect_if_fatal(exc)
                raise CameraError(f"trigger failed: {exc}") from exc

    def burst_number_limit(self) -> int | None:
        """Most frames one trigger can produce, or None if unsupported.

        Nikon exposes PTP's BurstNumber property. Set it to N, fire once, and
        the camera runs its *own* continuous drive for N frames at the rate the
        shutter button would give you — the buffer stays full and refreshing
        rather than draining between shots.

        This matters because ``trigger_capture`` blocks until the body reports
        itself idle again, so firing frame by frame pays a device-ready poll per
        frame. With BurstNumber that cost is paid once per burst instead of once
        per frame, which is the difference between a stutter and a burst.
        """
        if self._burst_probed:
            return self._burst_limit
        self._burst_probed = True
        if self._cam is None or not self._lock.acquire(timeout=1.0):
            self._burst_probed = False
            return None
        try:
            widget = self._find_widget(self._cam.get_config(), "burstnumber")
            if widget is None or widget.get_readonly():
                return None
            if WIDGET_TYPE_NAMES.get(widget.get_type()) != "range":
                return None
            _low, high, _step = widget.get_range()
            self._burst_limit = int(high)
        except (gp.GPhoto2Error, AttributeError, TypeError, ValueError):
            self._burst_limit = None
        finally:
            self._lock.release()
        return self._burst_limit

    def set_burst_number(self, count: int) -> None:
        """Arm the camera to shoot ``count`` frames per trigger.

        A no-op when the value is already set: every config write is a USB round
        trip, and during a burst those are exactly what we are trying to avoid.
        """
        count = max(1, int(count))
        if count == self._burst_number:
            return
        self.set_setting("burstnumber", count)
        self._burst_number = count

    def reset_burst_number(self) -> None:
        """Put the camera back to one frame per trigger.

        Essential rather than tidy: leaving BurstNumber above 1 would turn every
        later single exposure — a test frame, the next interval sequence — into
        an unwanted burst. Failures are swallowed because this runs on the way
        out of a burst, where raising would mask whatever actually ended it.
        """
        if self._burst_number == 1:
            return
        try:
            self.set_setting("burstnumber", 1)
        except (CameraError, gp.GPhoto2Error) as exc:
            log.warning("could not reset burstnumber: %s", exc)
        finally:
            self._burst_number = 1

    def burst_release_choice(self) -> str | None:
        """The body's fastest continuous drive mode, or None if it has none.

        BurstNumber says *how many* frames a trigger produces; the drive mode
        says how fast they come. A camera left in single-shot will happily fire
        the requested number of frames at single-shot cadence, with full
        between-shot settling — which is most of the difference between this and
        holding the button down.

        Matched by label against what the body actually lists, because the
        numeric values are vendor-specific and libgphoto2 leaves several of them
        unlabelled on Z bodies.
        """
        choices = self.get_choices("releasemode")
        for words in BURST_RELEASE_PREFERENCE:
            for choice in choices:
                lowered = choice.lower()
                if all(word in lowered for word in words):
                    return choice
        return None

    def arm_burst_release(self) -> str | None:
        """Switch to a continuous drive mode, remembering what was there.

        Returns the mode applied, or None if the body has none to offer or
        refused it — in which case the burst still runs, just slower.
        """
        if self._release_mode_saved is not None:
            return self._release_mode_applied
        wanted = self.burst_release_choice()
        if not wanted:
            return None
        try:
            current = self.get_setting("releasemode")
        except (CameraError, gp.GPhoto2Error):
            return None
        if current is not None and str(current) == wanted:
            return None            # already there; nothing to restore later
        try:
            self.set_setting("releasemode", wanted)
        except (CameraError, gp.GPhoto2Error) as exc:
            log.warning("could not set the drive mode to %s: %s", wanted, exc)
            return None
        self._release_mode_saved = current
        self._release_mode_applied = wanted
        return wanted

    def restore_burst_release(self) -> None:
        """Put the drive mode back to whatever the user had it on.

        Leaving the camera in continuous would make the next single frame fire a
        burst from one press, which is not what anyone asked for.
        """
        if self._release_mode_saved is None:
            return
        previous, self._release_mode_saved = self._release_mode_saved, None
        self._release_mode_applied = None
        try:
            self.set_setting("releasemode", previous)
        except (CameraError, gp.GPhoto2Error) as exc:
            log.warning("could not restore the drive mode: %s", exc)

    def _off_value(self, key: str, wanted, fallback):
        """The value on this widget that means "off".

        A radio or menu widget has to be given one of its own choices, so the
        wanted words are matched against them. A toggle or plain-value widget
        has no choices to pick from and takes the raw fallback instead.
        """
        choices = self.get_choices(key)
        if not choices:
            return fallback
        for candidate in wanted:
            for choice in choices:
                if str(candidate).lower() == str(choice).lower():
                    return choice
            for choice in choices:
                if str(candidate).lower() in str(choice).lower():
                    return choice
        return None

    def apply_speed_overrides(self) -> list[str]:
        """Turn off everything that costs time per frame. Returns what changed.

        Noise reduction and live view do not change what the burst is *of* —
        they only make the camera busy between frames — so a run asking for
        maximum speed can have them off and put back afterwards. Anything that
        would change the picture itself (image format above all) is left alone;
        that is the photographer's call, not ours.
        """
        changed = []
        for key, wanted, fallback in BURST_SPEED_OVERRIDES:
            try:
                current = self.get_setting(key)
            except (CameraError, gp.GPhoto2Error):
                continue
            if current is None:
                continue
            target = self._off_value(key, wanted, fallback)
            if target is None or str(current).lower() == str(target).lower():
                continue
            try:
                self.set_setting(key, target)
            except (CameraError, gp.GPhoto2Error) as exc:
                log.debug("could not override %s: %s", key, exc)
                continue
            self._speed_saved[key] = current
            changed.append(key)
        return changed

    def restore_speed_overrides(self) -> None:
        """Put everything apply_speed_overrides touched back."""
        while self._speed_saved:
            key, value = self._speed_saved.popitem()
            try:
                self.set_setting(key, value)
            except (CameraError, gp.GPhoto2Error) as exc:
                log.warning("could not restore %s: %s", key, exc)

    def collect_new_files(self, timeout_ms: int = 1, budget_s: float = 0.25) -> list:
        """Drain any files the camera has finished writing. Never blocks long.

        Nikon bodies interleave property-change events with file events, so a
        non-file event must not end the drain — files would queue up behind the
        chatter and the frame count would lag. Only a timeout (or the overall
        time budget) stops it.
        """
        found = []
        if self._cam is None or not self._lock.acquire(timeout=0.2):
            return found
        try:
            end = time.monotonic() + budget_s
            while True:
                try:
                    etype, data = self._cam.wait_for_event(timeout_ms)
                except gp.GPhoto2Error:
                    return found
                if etype == gp.GP_EVENT_FILE_ADDED:
                    found.append(data)
                elif etype == gp.GP_EVENT_TIMEOUT:
                    return found
                if time.monotonic() >= end:
                    return found
        finally:
            self._lock.release()

    def supports_bulb(self) -> bool:
        """Does this body expose the Nikon bulb toggle?

        Not every Nikon does — it disappears when the mode dial is off M, and a
        few bodies never expose it. Probing once avoids arming a sequence that
        would fail on its first frame.
        """
        if self._bulb_supported is None:
            try:
                cfg = self._require().get_config()
                self._bulb_supported = self._find_widget(cfg, "bulb") is not None
            except gp.GPhoto2Error:
                return False
        return bool(self._bulb_supported)

    def _bulb_toggles(self, cam):
        """Prepared open/close operations for the bulb widget.

        Closing is latency-critical: every millisecond spent assembling the
        command happens while the shutter is still open, so it stretches the
        real exposure. Prefer libgphoto2's single-config calls (one small PTP
        transaction each); otherwise walk the config tree once, up front, and
        reuse it for both toggles — never a fresh ``get_config`` at close time.
        """
        if hasattr(cam, "get_single_config") and hasattr(cam, "set_single_config"):
            for name in CONFIG_ALIASES.get("bulb", ["bulb"]):
                try:
                    widget = cam.get_single_config(name)
                except gp.GPhoto2Error:
                    continue

                def flip(value, name=name, widget=widget):
                    widget.set_value(value)
                    cam.set_single_config(name, widget)

                return (lambda: flip(1)), (lambda: flip(0))

        cfg = cam.get_config()
        widget = self._find_widget(cfg, "bulb")
        if widget is None:
            raise CameraError(
                "this camera is not exposing a bulb control. Check the mode dial "
                "is on M and the shutter speed is set to Bulb, then reconnect."
            )

        def flip_tree(value):
            widget.set_value(value)
            cam.set_config(cfg)

        return (lambda: flip_tree(1)), (lambda: flip_tree(0))

    def _capture_bulb(self, seconds: float):
        """Hold the shutter open for a set time, then collect the new file.

        Timed on the monotonic clock: an NTP step mid-exposure (the Pi has no
        RTC, and the clock jumps when it first reaches the internet) must not
        lengthen or truncate a frame.
        """
        cam = self._require()
        if not self.supports_bulb():
            raise CameraError(
                "this camera is not exposing a bulb control. Check the mode dial "
                "is on M and the shutter speed is set to Bulb, then reconnect."
            )

        self._drain_events(200)
        open_bulb, close_bulb = self._bulb_toggles(cam)
        open_bulb()
        try:
            deadline = time.monotonic() + seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.25))
        finally:
            close_bulb()
            self._snapshot_cache = None

        # The file appears a moment after the shutter closes.
        end = time.monotonic() + max(30.0, seconds * 0.5 + 15.0)
        while time.monotonic() < end:
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
