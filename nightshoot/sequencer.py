"""Run work against the camera on a background thread.

Two kinds of job share one status surface, so the UI, pause/resume, stop and
thumbnails behave identically for both:

* a simple intervalometer ``Plan``
* a declarative YAML ``Script``
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass

from . import scripts as scriptlib
from .camera import Camera, CameraError

log = logging.getLogger("nightshoot.sequencer")


class Stopped(Exception):
    """Raised inside a worker when the user asks it to stop."""


def next_slot(anchor: float, slot: int, interval: float, now: float) -> tuple[int, float]:
    """The next shutter-open time on a fixed grid of ``anchor + n * interval``.

    Sleeping ``interval`` between frames drifts: every cycle adds its own
    overhead, and over a few hundred frames that is seconds. Anchoring every
    slot to the start of the run instead keeps frame N at exactly
    ``anchor + N * interval`` for the whole night.

    Two late cases, handled differently on purpose:

    * Less than one slot late (the exposure slightly overran the interval):
      the returned target is in the past, so the caller fires immediately —
      the camera, not the schedule, is the limit, and shooting at the maximum
      rate is what the user wanted.
    * A full slot or more late (error backoff, a stuck card write): skip the
      missed slots and rejoin the grid at the next future one, rather than
      machine-gunning frames to "catch up" a schedule that is meant to be
      evenly spaced.
    """
    slot += 1
    target = anchor + slot * interval
    if now - target >= interval:
        slot = math.ceil((now - anchor) / interval)
        target = anchor + slot * interval
    return slot, target


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
        self._reserved = False        # claimed, worker may not have started yet
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
        with self._lock:
            # Reserved counts as running: the worker may not have started yet,
            # but the camera is already spoken for.
            if self._reserved:
                return True
            return self._thread is not None and self._thread.is_alive()

    def start(self, plan: Plan) -> None:
        problems = plan.validate()
        if problems:
            raise ValueError("; ".join(problems))
        self._arm(mode="interval", total=plan.frames, script_name=None)
        try:
            self.plan = plan
            self._say(
                f"sequence armed: {'unlimited' if plan.frames == 0 else plan.frames} frames, "
                f"{plan.interval_s}s interval, "
                f"{f'{plan.exposure_s}s bulb' if plan.bulb else 'camera shutter speed'}"
            )
            self._spawn(self._run_plan, plan)
        except BaseException:
            self._release()          # never leave the reservation stuck
            raise

    def start_script(self, script: scriptlib.Script) -> None:
        self._arm(mode="script", total=script.estimated_frames or 0,
                  script_name=script.name)
        try:
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
        except BaseException:
            self._release()
            raise

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
        """Claim the camera for a new run.

        The claim is taken atomically: two simultaneous requests must not both
        pass the "is anything running?" check and start workers that then fight
        over one USB connection.
        """
        with self._lock:
            if self._reserved or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("a sequence is already running")
            self._reserved = True

        try:
            # Connecting can be slow, so it happens outside the lock — but the
            # reservation above already keeps anyone else out.
            if not self.camera.connected:
                self.camera.connect()
        except BaseException:
            self._release()
            raise

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

    def _release(self) -> None:
        """Drop the reservation. Safe to call more than once."""
        with self._lock:
            self._reserved = False

    def _spawn(self, target, argument) -> None:
        with self._lock:
            self._thread = threading.Thread(
                target=self._guard, args=(target, argument),
                name="sequencer", daemon=True)
            thread = self._thread
        thread.start()

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
        finally:
            # The thread now owns "running"; the reservation has done its job.
            self._release()

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
        """Interruptible sleep until a wall-clock time (calendar deadlines).

        Wall clock on purpose: an ``until: "05:30"`` means 05:30, even if NTP
        steps the clock mid-wait.
        """
        return self._sleep_loop(target, time.time)

    def _sleep_until_mono(self, target: float) -> bool:
        """Interruptible sleep until a monotonic-clock time (durations).

        Intervals and backoffs must not stretch or shrink when the wall clock
        jumps — the Pi has no RTC, and NTP steps it whenever it first reaches
        the internet.
        """
        return self._sleep_loop(target, time.monotonic)

    def _sleep_for(self, seconds: float) -> bool:
        return self._sleep_until_mono(time.monotonic() + seconds)

    def _sleep_loop(self, target: float, clock) -> bool:
        """Interruptible sleep. False means we were told to stop."""
        while True:
            if self._stop.is_set():
                return False
            if self._pause.is_set():
                self._set_state("paused")
                time.sleep(0.25)
                # Paused time is not lost: push the target out.
                target = max(target, clock())
                continue
            remaining = target - clock()
            if remaining <= 0:
                return True
            with self._lock:
                if self.state == "paused":
                    self.state = "waiting"
                # Projected onto the wall clock for the UI, whichever clock
                # the deadline itself lives on.
                self.next_shot_at = time.time() + remaining
            time.sleep(min(remaining, 0.25))

    def _begin_exposure(self, seconds: float | None) -> None:
        """Mark a frame as in progress so the UI can count it down."""
        if seconds is None:
            # Cache-only on purpose: this runs between the slot boundary and
            # the shutter firing, and a PTP config read here would jitter the
            # actual shot time by however long the read takes. The cache is
            # refreshed during the waiting phase instead.
            try:
                seconds = self.camera.shutter_seconds_cached()
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
        now = time.monotonic()
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

    #: How long a burst may go with no accepted trigger *and* no new file
    #: before refused triggers count as real errors rather than backpressure.
    #: Generous on purpose: a buffered RAW burst can take seconds to drain.
    burst_stall_s = 10.0

    #: Target seconds of shooting per native-burst chunk. A trigger blocks until
    #: its whole chunk is shot and libgphoto2 exposes no way to abort one, so
    #: this is really the stop-latency budget: the worst case wait between
    #: asking a burst to stop and it stopping. Chunking by time rather than by a
    #: fixed frame count keeps that latency the same whether the camera is doing
    #: 11 fps of JPEG or 3 fps of RAW.
    #:
    #: Longer chunks are faster, but not by much: measured against a simulated
    #: Z50, going from 2 s to 60 s bought about 4%, and only when the camera
    #: makes a trigger wait for the buffer to flush. Two seconds is worth far
    #: more as responsiveness than as throughput. The drive mode, not the chunk
    #: length, is what closes the gap to the shutter button.
    burst_chunk_s = 2.0

    #: Frames in the first chunk, before there is any measured rate to go on.
    #: A full trigger's overhead is paid for these however few they are, so it
    #: is worth more than a token sample; still small enough not to overshoot a
    #: short burst badly.
    burst_chunk_first = 12

    #: Hard ceiling on a chunk, whatever the measured rate suggests. A rate
    #: estimate can be wrong — a first chunk out of an empty buffer looks much
    #: faster than the sustained rate — and an over-long chunk cannot be
    #: interrupted, because libgphoto2 exposes no way to abort one.
    burst_chunk_max = 200

    def _run_burst(self, plan: Plan) -> None:
        """Interval-mode burst: fire flat out until the plan says stop."""
        outcome = self._burst_loop(
            frames=plan.frames,
            until_ts=plan.until_ts,
            max_consecutive_errors=plan.max_consecutive_errors,
        )
        if outcome == "error":
            self._set_state("error")
            return self._finish("aborted after repeated capture errors", ok=False)
        self._finish("burst complete" if outcome == "done" else "stopped by user")

    def _burst_loop(self, frames: int = 0, until_ts: float | None = None,
                    duration_s: float | None = None,
                    max_consecutive_errors: int = 5) -> str:
        """Fire as fast as the body will go, by whichever route it supports."""
        limit = None
        try:
            limit = self.camera.burst_number_limit()
        except CameraError:
            limit = None
        if limit and limit > 1:
            try:
                outcome = self._native_burst(frames, until_ts, duration_s,
                                             max_consecutive_errors, limit)
            finally:
                # Leaving BurstNumber armed would turn the next single frame —
                # a test shot, the next interval sequence — into a burst, and
                # leaving the drive mode on continuous would do the same.
                self.camera.reset_burst_number()
                self.camera.restore_burst_release()
                self.camera.restore_speed_overrides()
            if outcome != "fallback":
                return outcome
        return self._trigger_burst(frames, until_ts, duration_s,
                                   max_consecutive_errors)

    def _native_burst(self, frames: int, until_ts: float | None,
                      duration_s: float | None, max_consecutive_errors: int,
                      limit: int) -> str:
        """Let the camera run its own continuous drive, a chunk at a time.

        Firing frame by frame over PTP cannot reach the shutter-button rate:
        libgphoto2 waits for the Nikon to report itself idle after every
        capture, polling at 100 ms, so each frame costs a poll interval however
        short the exposure. Setting BurstNumber and triggering once hands the
        whole burst to the camera, which runs it at its native rate.

        Two things have to be true for that rate to be the *continuous* rate.
        BurstNumber says how many frames; the drive mode says how fast. A body
        left in single-shot will fire the right number of frames with full
        between-shot settling, which looks like a burst but is not one.

        The cost is that the trigger blocks for the entire chunk — libgphoto2
        exposes no way to abort one — so chunks are sized by time, which is
        really the stop-latency budget.
        """
        applied = self.camera.arm_burst_release()
        if applied:
            self._say(f"burst mode: drive set to {applied}")
        else:
            self._say("burst mode: letting the camera drive at its own rate")
        for key in self.camera.apply_speed_overrides():
            self._say(f"burst mode: {key} turned off for speed")
        fmt = str(self.camera.snapshot().get("imageformat") or "")
        if fmt and not any(w in fmt.lower() for w in ("jpeg", "jpg")):
            # Not changed automatically: what the frames *are* is the
            # photographer's decision, not a knob to turn for a benchmark.
            self._say(f"burst mode: shooting {fmt} — JPEG would be markedly faster")
        self._set_state("exposing")
        consecutive = 0
        fired = 0
        started_at_frame = self.frames_done
        ends_at = (time.monotonic() + duration_s) if duration_s else None
        rate = 0.0
        outcome = "done"

        while not self._stop.is_set():
            if frames and fired >= frames:
                break
            if until_ts and time.time() >= until_ts:
                break
            if ends_at and time.monotonic() >= ends_at:
                break
            if self._pause.is_set():
                self._set_state("paused")
                time.sleep(0.2)
                self._set_state("exposing")
                if ends_at:
                    ends_at += 0.2
                continue

            # Until a chunk has been timed there is no rate to reason about, so
            # start small: a first chunk that overshoots a short burst cannot be
            # taken back.
            chunk = (max(1, round(rate * self.burst_chunk_s)) if rate > 0
                     else self.burst_chunk_first)
            chunk = min(chunk, limit, self.burst_chunk_max)
            if frames:
                chunk = min(chunk, frames - fired)
            if ends_at and rate > 0:
                # Do not start a chunk that would run past the deadline.
                fits = int((ends_at - time.monotonic()) * rate)
                chunk = max(1, min(chunk, fits))
            if chunk < 1:
                break

            began = time.monotonic()
            try:
                self.camera.set_burst_number(chunk)
            except CameraError as exc:
                if fired == 0:
                    # The widget is there but will not take a value. Nothing has
                    # been shot yet, so fall back rather than spend the night
                    # retrying something this body plainly will not do.
                    self._say(f"cannot arm a camera burst ({exc}) — "
                              "falling back to firing frame by frame")
                    return "fallback"
                consecutive += 1
                if not self._note_error(exc, consecutive, max_consecutive_errors):
                    return "error"
                if not self._sleep_for(min(5.0, 0.5 * consecutive)):
                    return "stopped"
                continue

            try:
                self.camera.trigger()
            except CameraError as exc:
                consecutive += 1
                if not self._note_error(exc, consecutive, max_consecutive_errors):
                    return "error"
                if not self._sleep_for(min(5.0, 0.5 * consecutive)):
                    return "stopped"
                continue

            consecutive = 0
            fired += chunk
            # Collect only what has already landed. Waiting for the rest would
            # let the camera's buffer drain while the shutter sits idle, which
            # is the opposite of what a burst wants: the buffer should stay
            # topped up, exactly as it does when the button is held down. The
            # stragglers are counted after the last chunk.
            self._drain(started_at_frame + fired, budget_s=0.0, quiet_s=0.0,
                        timeout_ms=0)

            elapsed = time.monotonic() - began
            if elapsed > 0:
                measured = chunk / elapsed
                # Smoothed, because the first chunk of a burst is faster than
                # the sustained rate once the camera's buffer is full.
                rate = measured if rate == 0 else (rate * 0.5 + measured * 0.5)

        if self._stop.is_set():
            outcome = "stopped"
        self._drain(started_at_frame + fired, budget_s=20.0, timeout_ms=200)
        return outcome

    def _drain(self, target: int, budget_s: float, quiet_s: float = 1.0,
               timeout_ms: int = 200) -> None:
        """Collect files until the frame count catches up.

        An empty poll does not mean the camera is finished: a frame that has
        just been shot is still on its way to the card, so giving up on the
        first quiet moment loses it from the count. Instead keep asking until
        the target is met, the budget runs out, or nothing has arrived for
        ``quiet_s`` — which is the honest signal that there is no more to come.

        A zero budget means "take whatever is already queued and return", for
        use between chunks of a burst where waiting would idle the shutter.
        """
        deadline = time.monotonic() + budget_s
        last_seen = time.monotonic()
        while self.frames_done < target:
            found = self.camera.collect_new_files(timeout_ms=timeout_ms)
            for path in found:
                self._record(_TriggeredShot(path))
            now = time.monotonic()
            if found:
                last_seen = now
                continue
            if now >= deadline or now - last_seen >= quiet_s:
                break

    def _trigger_burst(self, frames: int = 0, until_ts: float | None = None,
                       duration_s: float | None = None,
                       max_consecutive_errors: int = 5) -> str:
        """Fire the shutter frame by frame, without waiting for each file.

        The fallback for bodies with no BurstNumber property. Slower than a
        native burst — libgphoto2 waits for the camera to report itself idle
        after every capture — but it still beats a synchronous capture, which
        additionally waits for the file to be fetched.

        A refused trigger is not an error at full speed: it means the buffer
        is full, and the camera will accept again as soon as a frame reaches
        the card. Backing off a fixed half-second there turns a full buffer
        into a stutter — burst, stall, burst, stall. Instead, wait for
        evidence of drain (a new file event) and fire the moment space opens,
        so a long burst settles at the card's sustained write speed. Only a
        spell with no progress at all is treated as a real failure.

        Returns "done", "stopped" or "error".
        """
        self._say("burst mode: firing without waiting for each file")
        self._set_state("exposing")
        consecutive = 0
        fired = 0
        said_pacing = False
        last_progress = time.monotonic()   # last accepted trigger or new file
        last_collect = time.monotonic()
        ends_at = (time.monotonic() + duration_s) if duration_s else None
        # A script burst starts with frames already on the counter, so the
        # drain target is relative to where this burst began.
        started_at_frame = self.frames_done
        outcome = "done"

        while not self._stop.is_set():
            if frames and fired >= frames:
                break
            if until_ts and time.time() >= until_ts:
                break
            if ends_at and time.monotonic() >= ends_at:
                break
            if self._pause.is_set():
                self._set_state("paused")
                time.sleep(0.2)
                self._set_state("exposing")
                # Pausing must not eat the burst's allotted time.
                if ends_at:
                    ends_at += 0.2
                continue

            try:
                self.camera.trigger()
                fired += 1
                consecutive = 0
                last_progress = time.monotonic()
            except CameraError as exc:
                # Poll for a landed file — proof the buffer is draining and
                # the wait that paces this loop while the camera is busy.
                drained = self.camera.collect_new_files(timeout_ms=150, budget_s=0.3)
                for path in drained:
                    self._record(_TriggeredShot(path))
                now = time.monotonic()
                if drained:
                    consecutive = 0
                    last_progress = last_collect = now
                if now - last_progress < self.burst_stall_s:
                    if not said_pacing:
                        said_pacing = True
                        self._say("camera buffer full — pacing to the card's write speed")
                    continue
                consecutive += 1
                if not self._note_error(exc, consecutive, max_consecutive_errors):
                    return "error"
                if not self._sleep_for(min(5.0, 0.5 * consecutive)):
                    return "stopped"
                continue

            # Collecting is a USB event poll, so doing it after every trigger
            # caps the fire rate. A few times a second keeps the frame count
            # honest without slowing the shutter down.
            if time.monotonic() - last_collect >= 0.2:
                for path in self.camera.collect_new_files():
                    self._record(_TriggeredShot(path))
                last_collect = time.monotonic()

        if self._stop.is_set():
            outcome = "stopped"

        # The last few frames are still being written when the loop ends.
        target = started_at_frame + fired
        deadline = time.monotonic() + 20.0
        while self.frames_done < target and time.monotonic() < deadline:
            for path in self.camera.collect_new_files(timeout_ms=200):
                self._record(_TriggeredShot(path))
            if self._stop.is_set() and time.monotonic() > deadline - 15.0:
                break
        return outcome

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
            if not self._sleep_for(plan.start_delay_s):
                return self._finish("stopped before first frame")

        # With no interval and nothing to download, the shutter can be fired
        # without waiting for each file — noticeably faster than a synchronous
        # capture per frame.
        if plan.interval_s == 0 and not plan.bulb and not plan.download \
                and self.camera.supports_trigger():
            return self._run_burst(plan)

        if not plan.bulb:
            # Warm the shutter-duration cache now, forcing a fresh read; the
            # capture path only ever reads the cache, so the first frame's
            # countdown works too.
            self.camera.shutter_seconds(max_age=0.0)

        # Every shot sits on a fixed grid anchored here — see next_slot().
        anchor = time.monotonic()
        slot = 0
        consecutive = 0
        while not self._stop.is_set():
            if plan.frames and self.frames_done >= plan.frames:
                return self._finish("sequence complete")
            if plan.until_ts and time.time() >= plan.until_ts:
                return self._finish("reached scheduled end time")

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
                if not self._sleep_for(min(30.0, 3.0 * consecutive)):
                    return self._finish("stopped during error backoff")
                continue

            if plan.frames and self.frames_done >= plan.frames:
                return self._finish("sequence complete")

            self._set_state("waiting")
            if plan.interval_s:
                if not plan.bulb:
                    # Refresh the countdown cache here, in the waiting phase,
                    # where a PTP round trip cannot delay a shutter opening.
                    self.camera.shutter_seconds()
                slot, target = next_slot(anchor, slot, plan.interval_s,
                                         time.monotonic())
                if not self._sleep_until_mono(target):
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
        # A duration, so it lives on the monotonic clock: "wait: 10" means ten
        # real seconds even if NTP steps the wall clock mid-wait.
        self.sleep_until_monotonic(time.monotonic() + seconds)

    def sleep_until(self, timestamp: float) -> None:
        self.seq._set_state("waiting")
        if not self.seq._sleep_until(timestamp):
            raise Stopped()

    def sleep_until_monotonic(self, target: float) -> None:
        self.seq._set_state("waiting")
        if not self.seq._sleep_until_mono(target):
            raise Stopped()

    def apply_settings(self, settings: dict) -> None:
        for key, value in settings.items():
            self.seq.camera.set_setting(key, value)

    def burst(self, seconds: float | None, frames: int | None) -> None:
        """Fire flat out for a time or a frame count, then carry on.

        Refused when the body cannot trigger, rather than silently degrading to
        ordinary captures: a script asking for a burst wants the frame rate, and
        quietly giving it something slower would be worse than saying so.
        """
        if not self.seq.camera.supports_trigger():
            raise CameraError(
                "this camera cannot fire without waiting for each file, so "
                "'burst' is unavailable. Use 'repeat' with 'capture' instead."
            )
        self.check_stop()
        outcome = self.seq._burst_loop(
            frames=frames or 0,
            duration_s=seconds,
            max_consecutive_errors=self.MAX_CONSECUTIVE_ERRORS,
        )
        if outcome == "stopped":
            raise Stopped()
        if outcome == "error":
            raise CameraError("burst aborted after repeated capture errors")
        self.seq._set_state("waiting")

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
