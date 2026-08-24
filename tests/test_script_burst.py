"""The script ``burst:`` command and the ``for:`` duration on ``repeat``.

These exist so one script can change gear mid-run: fire flat out while something
is happening, then fall back to considered exposures, then fire again. The two
features are separate but were added together because that pattern needs both —
``burst:`` for the frame rate and ``for:`` for a bounded stretch of brackets.
"""

import time
import types

import pytest
import yaml

from nightshoot import scripts as S


def parse(text):
    return S.parse_script(text, "t.yaml")


class TestBurstValidation:
    def test_seconds_accepted(self):
        parse("steps:\n - burst: {seconds: 10}\n")

    def test_frames_accepted(self):
        parse("steps:\n - burst: {frames: 30}\n")

    def test_needs_one_of_them(self):
        with pytest.raises(S.ScriptError, match="seconds"):
            parse("steps:\n - burst: {}\n")
        with pytest.raises(S.ScriptError, match="seconds"):
            parse("steps:\n - burst:\n")

    def test_refuses_both(self):
        with pytest.raises(S.ScriptError, match="not both"):
            parse("steps:\n - burst: {seconds: 10, frames: 5}\n")

    def test_refuses_unknown_options(self):
        with pytest.raises(S.ScriptError, match="bulb"):
            parse("steps:\n - burst: {seconds: 10, bulb: 30}\n")

    def test_refuses_zero_and_negative(self):
        for bad in ("{seconds: 0}", "{seconds: -5}", "{frames: 0}", "{frames: -1}"):
            with pytest.raises(S.ScriptError):
                parse(f"steps:\n - burst: {bad}\n")

    def test_frames_must_be_whole(self):
        with pytest.raises(S.ScriptError, match="whole number"):
            parse("steps:\n - burst: {frames: 2.5}\n")

    def test_seconds_may_be_fractional(self):
        parse("steps:\n - burst: {seconds: 0.5}\n")

    def test_placeholders_are_refused(self):
        """Numbers are checked before the run starts, not during it.

        You find out a script is wrong at your desk rather than in a field at
        2 a.m., so a numeric field may not be a '{{variable}}' — the same rule
        already applies to 'bulb', 'wait' and 'every'.
        """
        with pytest.raises(S.ScriptError, match="positive number"):
            parse("vars: {n: 7}\nsteps:\n - burst: {seconds: '{{n}}'}\n")
        with pytest.raises(S.ScriptError, match="whole number"):
            parse("steps:\n - repeat: 3\n   steps:\n    - burst: {frames: '{{i}}'}\n")


class TestForValidation:
    def test_accepted_on_repeat(self):
        parse("steps:\n - repeat: forever\n   for: 60\n   steps:\n    - capture:\n")

    def test_refuses_zero_and_negative(self):
        for bad in ("0", "-30"):
            with pytest.raises(S.ScriptError, match="'for'"):
                parse(f"steps:\n - repeat: forever\n   for: {bad}\n"
                      "   steps:\n    - capture:\n")

    def test_refuses_text(self):
        with pytest.raises(S.ScriptError, match="'for'"):
            parse("steps:\n - repeat: forever\n   for: soon\n   steps:\n    - capture:\n")

    def test_coexists_with_until_and_every(self):
        parse("steps:\n - repeat: forever\n   for: 60\n   every: 10\n"
              "   until: '05:00'\n   steps:\n    - capture:\n")


class TestFrameEstimation:
    def test_frame_bounded_burst_counts(self):
        assert parse("steps:\n - burst: {frames: 30}\n").estimated_frames == 30

    def test_timed_burst_is_unknown(self):
        assert parse("steps:\n - burst: {seconds: 10}\n").estimated_frames is None

    def test_adds_to_ordinary_captures(self):
        script = parse("steps:\n - capture:\n - burst: {frames: 9}\n - capture:\n")
        assert script.estimated_frames == 11

    def test_a_timed_loop_is_unknown(self):
        script = parse("steps:\n - repeat: forever\n   for: 60\n"
                       "   steps:\n    - capture:\n")
        assert script.estimated_frames is None

    def test_a_timed_loop_with_no_captures_is_still_zero(self):
        script = parse("steps:\n - repeat: forever\n   for: 60\n"
                       "   steps:\n    - message: tick\n")
        assert script.estimated_frames == 0


class Recorder(S.Runtime):
    """Runs a script without a camera, noting what was asked of it."""

    def __init__(self, clock=None):
        self.events: list[tuple] = []
        self.slept = 0.0
        self._clock = clock

    def log(self, message): self.events.append(("log", message))
    def apply_settings(self, settings): self.events.append(("set", dict(settings)))
    def capture(self, bulb, download): self.events.append(("capture", bulb))
    def burst(self, seconds, frames): self.events.append(("burst", seconds, frames))
    def check_stop(self): pass

    def sleep(self, seconds):
        self.slept += seconds
        if self._clock:
            self._clock.advance(seconds)

    def sleep_until(self, timestamp): pass

    def sleep_until_monotonic(self, target):
        if self._clock:
            self._clock.advance(max(0.0, target - self._clock.now))


class Clock:
    """A monotonic clock the test drives by hand, so no test waits in real time.

    Patched onto the ``scripts`` module rather than onto ``time`` itself: the
    real ``time.monotonic`` is what pytest and the threading primitives run on,
    and replacing it globally breaks the test runner.
    """

    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    import time as real_time

    fake = Clock()
    shim = types.SimpleNamespace(monotonic=fake.monotonic, time=real_time.time,
                                 sleep=real_time.sleep)
    monkeypatch.setattr(S, "time", shim)
    return fake


class TestBurstInterpretation:
    def test_passes_seconds_through(self):
        rt = Recorder()
        S.run(parse("steps:\n - burst: {seconds: 10}\n"), rt)
        assert ("burst", 10.0, None) in rt.events

    def test_passes_frames_through(self):
        rt = Recorder()
        S.run(parse("steps:\n - burst: {frames: 30}\n"), rt)
        assert ("burst", None, 30) in rt.events

    def test_substitutes_variables(self):
        """A whole-string variable is still resolved before the runtime sees it.

        Validation refuses a placeholder in 'seconds', so this can only happen
        by building the step in code — but the interpreter must not care where
        the step came from.
        """
        rt = Recorder()
        S.run(S.Script(filename="t.yaml", name="t", description="", vars={"n": 7},
                       steps=[{"burst": {"seconds": "{{n}}"}}]), rt)
        assert ("burst", 7.0, None) in rt.events

    def test_announces_itself(self):
        rt = Recorder()
        S.run(parse("steps:\n - burst: {seconds: 10}\n"), rt)
        assert any(e[0] == "log" and "10" in str(e[1]) for e in rt.events)


class TestForDuration:
    def test_stops_when_the_window_closes(self, clock):
        rt = Recorder(clock)
        S.run(parse("steps:\n - repeat: forever\n   for: 10\n   every: 2\n"
                    "   steps:\n    - capture:\n"), rt)
        captures = [e for e in rt.events if e[0] == "capture"]
        assert len(captures) == 5, "10 seconds at one every 2 seconds"

    def test_does_not_sleep_past_the_window(self, clock):
        rt = Recorder(clock)
        S.run(parse("steps:\n - repeat: forever\n   for: 5\n   every: 10\n"
                    "   steps:\n    - capture:\n"), rt)
        assert clock.now <= 1005.0, "the loop should not overrun its own duration"

    def test_a_count_still_caps_it(self, clock):
        rt = Recorder(clock)
        S.run(parse("steps:\n - repeat: 2\n   for: 3600\n   every: 1\n"
                    "   steps:\n    - capture:\n"), rt)
        assert len([e for e in rt.events if e[0] == "capture"]) == 2

    def test_the_earlier_of_for_and_until_wins(self, clock, monkeypatch):
        # 'until' resolves to a wall-clock time far in the future; 'for' is
        # short, so 'for' must be what ends the loop.
        rt = Recorder(clock)
        monkeypatch.setattr(S, "clock_to_timestamp", lambda _: time.time() + 86400)
        S.run(parse("steps:\n - repeat: forever\n   for: 4\n   every: 2\n"
                    "   until: '23:59'\n   steps:\n    - capture:\n"), rt)
        assert len([e for e in rt.events if e[0] == "capture"]) == 2

    def test_the_script_continues_afterwards(self, clock):
        rt = Recorder(clock)
        S.run(parse("steps:\n - repeat: forever\n   for: 2\n   every: 1\n"
                    "   steps:\n    - capture:\n - message: after\n"), rt)
        assert ("log", "after") in rt.events


class TestBurstOnTheSequencer:
    """End to end, against the fake camera."""

    def test_shoots_a_frame_count(self, sequencer, wait_for):
        sequencer.start_script(parse("steps:\n - burst: {frames: 8}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=30)
        assert sequencer.status()["frames_done"] == 8
        assert sequencer.status()["state"] == "done"

    def test_counts_across_mixed_phases(self, sequencer, wait_for):
        sequencer.start_script(parse(
            "steps:\n - capture:\n - burst: {frames: 5}\n - capture:\n"))
        assert wait_for(lambda: not sequencer.running, timeout=30)
        assert sequencer.status()["frames_done"] == 7

    def test_a_timed_burst_ends_on_time(self, sequencer, camera_state, wait_for):
        camera_state.cost["trigger"] = 0.01
        started = time.time()
        sequencer.start_script(parse("steps:\n - burst: {seconds: 1}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=30)
        elapsed = time.time() - started
        assert 0.9 <= elapsed < 8, f"took {elapsed:.1f}s for a 1s burst"
        assert sequencer.status()["frames_done"] > 0

    def test_stop_ends_the_whole_script(self, sequencer, camera_state, wait_for):
        camera_state.cost["trigger"] = 0.02
        sequencer.start_script(parse(
            "steps:\n - burst: {seconds: 30}\n - capture:\n"))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0, timeout=30)
        during = sequencer.status()["frames_done"]
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=30)
        # A stop is an ordinary end, not a failure, so the state is "done" and
        # the reason lives in the log. What matters is that the step *after*
        # the burst never ran.
        assert sequencer.status()["errors"] == 0
        assert any("stopped by user" in line for line in sequencer.status()["log"])
        assert sequencer.status()["frames_done"] < during + 20

    def test_refused_when_the_body_cannot_trigger(self, sequencer, camera,
                                                 monkeypatch, wait_for):
        monkeypatch.setattr(camera, "supports_trigger", lambda: False)
        sequencer.start_script(parse("steps:\n - burst: {seconds: 5}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=30)
        status = sequencer.status()
        assert status["state"] == "error"
        assert "burst" in status["last_error"].lower()

    def test_the_burst_bracket_burst_pattern(self, sequencer, camera_state, wait_for):
        camera_state.cost["trigger"] = 0.01
        camera_state.cost["capture"] = 0.01
        sequencer.start_script(parse(
            "steps:\n"
            " - burst: {frames: 4}\n"
            " - repeat: forever\n"
            "   for: 1\n"
            "   every: 0.3\n"
            "   steps:\n"
            "    - capture:\n"
            " - burst: {frames: 4}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["state"] == "done"
        # Eight burst frames plus however many brackets fitted in the second.
        assert sequencer.status()["frames_done"] >= 9

    def test_a_setting_before_a_burst_is_applied(self, sequencer, camera, wait_for):
        sequencer.start_script(parse(
            "steps:\n - set: {iso: '3200'}\n - burst: {frames: 3}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=30)
        assert camera.get_setting("iso") == "3200"
        assert sequencer.status()["frames_done"] == 3


class TestShippedExample:
    def test_the_example_parses(self):
        here = __file__.rsplit("tests", 1)[0]
        path = f"{here}examples/scripts/burst-bracket-burst.yaml"
        with open(path, encoding="utf-8") as handle:
            script = S.parse_script(handle.read(), "burst-bracket-burst.yaml")
        assert script.name == "Burst, bracket, burst"
        # Two bursts and a bounded bracket loop.
        assert yaml.safe_dump(script.steps).count("burst:") == 2
        assert script.estimated_frames is None
