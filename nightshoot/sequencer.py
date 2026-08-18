"""Run work against the camera on a background thread.

Two kinds of job share one status surface, so the UI, pause/resume, stop and
thumbnails behave identically for both:

* a simple intervalometer ``Plan``
* a declarative YAML ``Script``
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import asdict, dataclass

from . import scripts as scriptlib
from .camera import Camera, CameraError

log = logging.getLogger("nightshoot.sequencer")


class Stopped(Exception):
    """Raised inside a worker when the user asks it to stop."""


class _TriggeredShot:
    """A frame fired in burst mode, where no preview is fetched per shot."""

    __slots__ = ("folder", "name", "started_at", "exposure_s", "saved_path", "thumb_path")

    def __init__(self, path):
        self.folder = getattr(path, "folder", "")
        self.name = getattr(path, "name", "frame")
        self.started_at = time.time()
        self.exposure_s = 0.0
        self.saved_path = None
        self.thumb_path = None


@dataclass
class Plan:
    frames: int = 100          # 0 == run until stopped or until_ts
    exposure_s: float = 20.0   # only used when bulb is True
    interval_s: float = 25.0   # shutter-open to shutter-open
    start_delay_s: float = 5.0
    bulb: bool = False
    download: bool = False
    until_ts: float | None = None   # unix time to stop at
    max_consecutive_errors: int = 5

    def validate(self) -> list[str]:
        problems = []
        if self.frames < 0:
            problems.append("frames cannot be negative")
        if self.bulb and self.exposure_s < 0.5:
            problems.append("bulb exposure must be at least 0.5 s")
        if self.interval_s < 0:
            problems.append("interval cannot be negative")
        if self.bulb and self.interval_s and self.interval_s <= self.exposure_s:
            problems.append(
                f"interval ({self.interval_s}s) must exceed the exposure "
                f"({self.exposure_s}s); allow a few seconds for card write"
            )
        if self.bulb and not self.interval_s:
            problems.append("bulb frames need an interval longer than the exposure")
        return problems


class Sequencer:
    def __init__(self, camera: Camera):
        self.camera = camera
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.RLock()

        self.plan = Plan()
        self.mode = "interval"        # interval | script
        self.script_name: str | None = None
        self.state = "idle"           # idle | waiting | exposing | paused | error | done
        self.frames_done = 0
        self.frames_total = 0         # 0 == unknown / unlimited
        self.errors = 0
        self.started_at: float | None = None
        self.next_shot_at: float | None = None
        self.exposing_until: float | None = None
        self.last_cycle_s: float | None = None
        self._last_frame_at: float | None = None
        self.last_error: str | None = None
        self.last_shot: dict | None = None
        self.log_lines: collections.deque = collections.deque(maxlen=300)

    # ------------------------------------------------------------------ public

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, plan: Plan) -> None:
        problems = plan.validate()
        if problems:
            raise ValueError("; ".join(problems))
        self._arm(mode="interval", total=plan.frames, script_name=None)
        self.plan = plan
        self._say(
            f"sequence armed: {'unlimited' if plan.frames == 0 else plan.frames} frames, "
            f"{plan.interval_s}s interval, "
            f"{f'{plan.exposure_s}s bulb' if plan.bulb else 'camera shutter speed'}"
        )
        self._spawn(self._run_plan, plan)

    def start_script(self, script: scriptlib.Script) -> None:
        self._arm(mode="script", total=script.estimated_frames or 0,
                  script_name=script.name)
        problems = self._preflight(script)
        if problems:
            with self._lock:
                self.state = "error"
                self.last_error = problems[0]
            for problem in problems:
                self._say(f"script cannot run: {problem}")
            raise ValueError(problems[0])
        estimate = ("unknown" if script.estimated_frames is None
                    else f"{script.estimated_frames} frame(s)")
        self._say(f"script '{script.name}' armed ({estimate})")
        self._spawn(self._run_script, script)

    def _preflight(self, script: scriptlib.Script) -> list[str]:
        """Check the script's settings against the camera before shooting.

        Catching a bad value here means the user sees it immediately, instead of
        the script arming and then aborting after zero frames.
        """
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for key, value in scriptlib.collect_settings(script.steps, script.vars):
            token = (key, str(value))
            if token in seen:
                continue
            seen.add(token)
            try:
                self.camera.check_setting(key, value)
            except CameraError as exc:
                problems.append(str(exc))
        return problems

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def pause(self) -> None:
        if self.running:
            self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "state": self.state,
                "running": self.running,
                "mode": self.mode,
                "script_name": self.script_name,
                "frames_done": self.frames_done,
                "frames_total": self.frames_total,
                "errors": self.errors,
                "last_error": self.last_error,
                "seconds_to_next": (max(0.0, self.next_shot_at - now)
                                    if self.next_shot_at else None),
                "exposure_left": (max(0.0, self.exposing_until - now)
                                  if self.exposing_until else None),
                "last_cycle_s": (round(self.last_cycle_s, 2)
                                 if self.last_cycle_s else None),
                "elapsed": (now - self.started_at) if self.started_at else 0.0,
                "eta": self._eta(now),
                "plan": asdict(self.plan),
                "last_shot": self.last_shot,
                "log": list(self.log_lines)[-40:],
            }

    # ---------------------------------------------------------------- plumbing

    def _arm(self, mode: str, total: int, script_name: str | None) -> None:
        if self.running:
            raise RuntimeError("a sequence is already running")
        if not self.camera.connected:
            self.camera.connect()
        with self._lock:
            self.mode = mode
            self.script_name = script_name
            self.state = "waiting"
            self.frames_done = 0
            self.frames_total = total
            self.errors = 0
            self.last_error = None
            self.last_shot = None
            self.started_at = time.time()
            self.next_shot_at = None
            self.last_cycle_s = None
            self._last_frame_at = None
        # A preview JPEG costs far more than a short frame, so never fetch them
        # faster than the UI can show them.
        self.camera.begin_run(thumb_min_interval=1.5)
        self._stop.clear()
        self._pause.clear()

    def _spawn(self, target, argument) -> None:
        self._thread = threading.Thread(
            target=self._guard, args=(target, argument), name="sequencer", daemon=True)
        self._thread.start()

    def _guard(self, target, argument) -> None:
        try:
            target(argument)
        except Stopped:
            self._finish("stopped by user")
        except Exception as exc:  # noqa: BLE001 - the thread must never die silently
            log.exception("worker crashed")
            with self._lock:
                self.state = "error"
                self.last_error = str(exc)
            self._say(f"fatal: {exc}")

    def _eta(self, now: float) -> float | None:
        if not self.running or not self.frames_total or self.mode != "interval":
            return None
        left = self.frames_total - self.frames_done
        if left <= 0:
            return now
        # With no interval the pace is whatever the camera manages, so use the
        # measured cycle time rather than the (zero) requested one.
        pace = self.plan.interval_s or self.last_cycle_s
        return now + left * pace if pace else None

    def _say(self, message: str) -> None:
        log.info(message)
        self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def _sleep_until(self, target: float) -> bool:
        """Interruptible sleep. False means we were told to stop."""
        while True:
            if self._stop.is_set():
                return False
            if self._pause.is_set():
                self._set_state("paused")
                time.sleep(0.25)
                # Paused time is not lost: push the target out.
                target = max(target, time.time())
                continue
            remaining = target - time.time()
            if remaining <= 0:
                return True
            with self._lock:
                if self.state == "paused":
                    self.state = "waiting"
                self.next_shot_at = target
            time.sleep(min(remaining, 0.25))

    def _begin_exposure(self, seconds: float | None) -> None:
        """Mark a frame as in progress so the UI can count it down."""
        if seconds is None:
            # Cached: reading the config per frame costs more than a fast frame.
            try:
                seconds = self.camera.shutter_seconds()
            except Exception:  # noqa: BLE001 - a countdown is never worth failing over
                seconds = None
        with self._lock:
            self.state = "exposing"
            self.next_shot_at = None
            # Only worth a countdown when it is long enough to watch.
            self.exposing_until = (time.time() + seconds
                                   if seconds and seconds >= 2 else None)

    def _end_exposure(self) -> None:
        with self._lock:
            self.exposing_until = None

    def _record(self, shot) -> None:
        now = time.time()
        with self._lock:
            # Measured shutter-to-shutter time: the honest answer to "how fast
            # can this actually go?", which is bounded by the camera, not by us.
            if self._last_frame_at:
                self.last_cycle_s = now - self._last_frame_at
            self._last_frame_at = now
            self.frames_done += 1
            self.last_shot = {
                "name": shot.name,
                "at": shot.started_at,
                "exposure_s": round(shot.exposure_s, 2),
                "saved_path": shot.saved_path,
                "has_thumb": bool(shot.thumb_path),
            }
            done = self.frames_done
        total = self.frames_total or "?"
        self._say(f"frame {done}/{total}: {shot.name}")

    # --------------------------------------------------------------- burst job

    def _run_burst(self, plan: Plan) -> None:
        """Fire the shutter without waiting for each file to be written.

        The camera's own buffer becomes the limit rather than the PTP round
        trip. Files are collected from the event queue as they appear, so the
        frame count stays honest.
        """
        self._say("burst mode: firing without waiting for each file")
        self._set_state("exposing")
        consecutive = 0
        fired = 0

        while not self._stop.is_set():
            if plan.frames and fired >= plan.frames:
                break
            if plan.until_ts and time.time() >= plan.until_ts:
                break
            if self._pause.is_set():
                self._set_state("paused")
                time.sleep(0.2)
                self._set_state("exposing")
                continue

            try:
                self.camera.trigger()
                fired += 1
                consecutive = 0
            except CameraError as exc:
                consecutive += 1
                if not self._note_error(exc, consecutive, plan.max_consecutive_errors):
                    self._set_state("error")
                    return self._finish("aborted after repeated capture errors", ok=False)
                # A full buffer shows up as an error; easing off lets it drain.
                if not self._sleep_until(time.time() + min(5.0, 0.5 * consecutive)):
                    return self._finish("stopped during error backoff")

            for path in self.camera.collect_new_files():
                self._record(_TriggeredShot(path))

        # The last few frames are still being written when the loop ends.
        deadline = time.time() + 20.0
        while self.frames_done < fired and time.time() < deadline:
            for path in self.camera.collect_new_files(timeout_ms=200):
                self._record(_TriggeredShot(path))
            if self._stop.is_set() and time.time() > deadline - 15.0:
                break

        self._finish("burst complete" if not self._stop.is_set() else "stopped by user")

    def _note_error(self, exc: Exception, consecutive: int, limit: int) -> bool:
        """Record a capture failure. False means give up."""
        with self._lock:
            self.errors += 1
            self.last_error = str(exc)
        self._say(f"error ({consecutive}/{limit}): {exc}")
        return consecutive < limit

    def _finish(self, reason: str, ok: bool = True) -> None:
        with self._lock:
            if ok:
                self.state = "done"
            self.next_shot_at = None
        self._say(f"{reason} — {self.frames_done} frame(s) captured")

    # ------------------------------------------------------------ interval job

    def _run_plan(self, plan: Plan) -> None:
        for warning in self.camera.prepare_for_night():
            self._say(f"warning: {warning}")

        if plan.start_delay_s > 0:
            self._say(f"starting in {plan.start_delay_s:.0f}s (let vibrations settle)")
            if not self._sleep_until(time.time() + plan.start_delay_s):
                return self._finish("stopped before first frame")

        # With no interval and nothing to download, the shutter can be fired
        # without waiting for each file — noticeably faster than a synchronous
        # capture per frame.
        if plan.interval_s == 0 and not plan.bulb and not plan.download \
                and self.camera.supports_trigger():
            return self._run_burst(plan)

        consecutive = 0
        while not self._stop.is_set():
            if plan.frames and self.frames_done >= plan.frames:
                return self._finish("sequence complete")
            if plan.until_ts and time.time() >= plan.until_ts:
                return self._finish("reached scheduled end time")

            slot_start = time.time()
            self._begin_exposure(plan.exposure_s if plan.bulb else None)

            try:
                shot = self.camera.capture(
                    exposure_s=plan.exposure_s if plan.bulb else None,
                    bulb=plan.bulb,
                    download=plan.download,
                )
                consecutive = 0
                self._end_exposure()
                self._record(shot)
            except CameraError as exc:
                consecutive += 1
                self._end_exposure()
                if not self._note_error(exc, consecutive, plan.max_consecutive_errors):
                    self._set_state("error")
                    return self._finish("aborted after repeated capture errors", ok=False)
                if not self._sleep_until(time.time() + min(30.0, 3.0 * consecutive)):
                    return self._finish("stopped during error backoff")
                continue

            self._set_state("waiting")
            if not self._sleep_until(slot_start + plan.interval_s):
                return self._finish("stopped by user")

        self._finish("stopped by user")

    # -------------------------------------------------------------- script job

    def _run_script(self, script: scriptlib.Script) -> None:
        for warning in self.camera.prepare_for_night():
            self._say(f"warning: {warning}")
        try:
            scriptlib.run(script, _ScriptRuntime(self))
        except scriptlib.ScriptError as exc:
            with self._lock:
                self.state = "error"
                self.last_error = str(exc)
            self._say(f"script error: {exc}")
            return self._finish(f"script '{script.name}' aborted", ok=False)
        except CameraError as exc:
            with self._lock:
                self.state = "error"
                self.last_error = str(exc)
            return self._finish(f"script '{script.name}' aborted: {exc}", ok=False)
        self._finish(f"script '{script.name}' complete")


class _ScriptRuntime(scriptlib.Runtime):
    """Bridges the script interpreter to the sequencer's state and controls."""

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, seq: Sequencer):
        self.seq = seq
        self.consecutive = 0

    def check_stop(self) -> None:
        if self.seq._stop.is_set():
            raise Stopped()

    def log(self, message: str) -> None:
        self.seq._say(message)

    def sleep(self, seconds: float) -> None:
        self.sleep_until(time.time() + seconds)

    def sleep_until(self, timestamp: float) -> None:
        self.seq._set_state("waiting")
        if not self.seq._sleep_until(timestamp):
            raise Stopped()

    def apply_settings(self, settings: dict) -> None:
        for key, value in settings.items():
            self.seq.camera.set_setting(key, value)

    def capture(self, bulb: float | None, download: bool) -> None:
        while True:
            self.check_stop()
            self.seq._begin_exposure(bulb)
            try:
                shot = self.seq.camera.capture(
                    exposure_s=bulb, bulb=bool(bulb), download=download)
            except CameraError as exc:
                self.consecutive += 1
                self.seq._end_exposure()
                if not self.seq._note_error(exc, self.consecutive, self.MAX_CONSECUTIVE_ERRORS):
                    raise
                self.sleep(min(30.0, 3.0 * self.consecutive))
                continue
            self.consecutive = 0
            self.seq._end_exposure()
            self.seq._record(shot)
            return
