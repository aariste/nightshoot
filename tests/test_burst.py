"""Burst mode: firing the shutter without waiting for each file.

A synchronous capture waits for the exposure *and* the file to be written, which
is a full PTP round trip per frame. Burst mode fires and collects files from the
event queue afterwards. It cannot reach the camera's own continuous-release
rate, but it should clearly beat the synchronous path.
"""

import time

from nightshoot.sequencer import Plan


class TestTriggerSupport:
    def test_detected(self, camera):
        assert camera.supports_trigger() is True

    def test_absent_bodies_report_false(self, camera, camera_state, monkeypatch):
        monkeypatch.delattr(type(camera._cam), "trigger_capture", raising=False)
        camera._can_trigger = None
        assert camera.supports_trigger() is False

    def test_trigger_returns_without_a_file(self, camera, camera_state):
        camera.trigger()
        assert camera_state.calls["trigger"] == 1
        assert camera_state.calls["preview"] == 0, "burst must not fetch previews"

    def test_files_arrive_afterwards(self, camera):
        camera.trigger()
        camera.trigger()
        found = camera.collect_new_files(timeout_ms=50)
        assert len(found) == 2

    def test_collect_is_non_blocking_when_idle(self, camera):
        started = time.time()
        assert camera.collect_new_files() == []
        assert time.time() - started < 0.5


class TestBurstIsFaster:
    def test_beats_the_synchronous_path(self, sequencer, camera_state, wait_for):
        """The whole point: a trigger costs less than a capture round trip."""
        camera_state.cost["capture"] = 0.05     # synchronous: waits for the file
        camera_state.cost["trigger"] = 0.005    # fire and forget

        started = time.time()
        sequencer.start(Plan(frames=20, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        burst = time.time() - started
        assert sequencer.status()["frames_done"] == 20

        # Force the synchronous path by asking for downloads.
        camera_state.reset()
        camera_state.cost["capture"] = 0.05
        camera_state.cost["trigger"] = 0.005
        started = time.time()
        sequencer.start(Plan(frames=20, interval_s=0, start_delay_s=0, download=True))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        synchronous = time.time() - started

        assert burst < synchronous, (
            f"burst ({burst:.2f}s) should beat synchronous ({synchronous:.2f}s)")

    def test_does_not_fetch_previews_per_frame(self, sequencer, camera_state, wait_for):
        sequencer.start(Plan(frames=15, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.calls["preview"] == 0


class TestBurstCorrectness:
    def test_counts_every_frame(self, sequencer, wait_for):
        sequencer.start(Plan(frames=12, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        status = sequencer.status()
        assert status["frames_done"] == 12
        assert status["state"] == "done"
        assert status["errors"] == 0

    def test_drains_files_written_after_the_last_trigger(self, sequencer, camera_state,
                                                         wait_for):
        sequencer.start(Plan(frames=8, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 8
        assert camera_state.pending_files == [], "files were left uncollected"

    def test_reports_burst_mode_in_the_log(self, sequencer, wait_for):
        sequencer.start(Plan(frames=3, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert any("burst" in line for line in sequencer.status()["log"])

    def test_stop_is_prompt(self, sequencer, wait_for):
        sequencer.start(Plan(frames=0, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0, timeout=10)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=25)

    def test_pause_holds(self, sequencer, camera_state, wait_for):
        camera_state.cost["trigger"] = 0.02
        sequencer.start(Plan(frames=0, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0, timeout=10)
        sequencer.pause()
        assert wait_for(lambda: sequencer.status()["state"] == "paused", timeout=5)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=25)


class TestBufferPressure:
    def test_a_full_buffer_is_backpressure_not_an_error(self, sequencer, camera_state,
                                                        wait_for):
        """A refused trigger means the buffer is full; the loop waits for a
        file to land and fires again — no error counted, no abort."""
        camera_state.buffer_limit = 3
        sequencer.start(Plan(frames=10, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=90)
        status = sequencer.status()
        assert status["frames_done"] == 10
        assert status["state"] == "done"
        assert status["errors"] == 0, "buffer pressure was miscounted as errors"

    def test_gives_up_on_persistent_failure(self, sequencer, camera_state, wait_for):
        camera_state.supports_trigger = False   # trigger_capture now raises
        sequencer.burst_stall_s = 0.5           # keep the test quick
        sequencer.start(Plan(frames=10, interval_s=0, start_delay_s=0,
                             max_consecutive_errors=3))
        assert wait_for(lambda: not sequencer.running, timeout=90)
        assert sequencer.status()["state"] == "error"


class TestWhenBurstIsNotUsed:
    """Burst skips per-frame previews and downloads, so it only applies when
    those are not wanted."""

    def test_not_used_when_downloading(self, sequencer, camera_state, wait_for):
        sequencer.start(Plan(frames=3, interval_s=0, start_delay_s=0, download=True))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.calls["capture"] == 3
        assert camera_state.calls["trigger"] == 0

    def test_not_used_with_an_interval(self, sequencer, camera_state, wait_for):
        sequencer.start(Plan(frames=3, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.calls["capture"] == 3
        assert camera_state.calls["trigger"] == 0

    def test_not_used_for_bulb(self, sequencer, camera_state, wait_for):
        sequencer.start(Plan(frames=1, interval_s=2, start_delay_s=0,
                             bulb=True, exposure_s=0.5))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.calls["trigger"] == 0

    def test_falls_back_when_unsupported(self, sequencer, camera, camera_state,
                                         monkeypatch, wait_for):
        monkeypatch.delattr(type(camera._cam), "trigger_capture", raising=False)
        camera._can_trigger = None
        sequencer.start(Plan(frames=3, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 3
        assert camera_state.calls["capture"] == 3
