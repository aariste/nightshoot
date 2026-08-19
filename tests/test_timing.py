"""Timing: the interval grid, bulb exposure accuracy, and burst collection.

The three complaints these guard against:

* interval sequences drifting (or shifting permanently after a stall),
* bulb frames running long because config I/O happened with the shutter open,
* bursts throttled or under-counted by event-queue bookkeeping.
"""

import time

import pytest

from nightshoot.sequencer import Plan, next_slot


class TestNextSlot:
    """The shot grid is pure arithmetic, so its policy is tested exactly."""

    def test_advances_along_the_grid(self):
        assert next_slot(anchor=100.0, slot=0, interval=25.0, now=100.1) == (1, 125.0)

    def test_targets_stay_multiples_of_the_interval(self):
        # Overhead in the previous cycle must not push later frames: slot 4 is
        # anchor + 4 * interval regardless of when slot 3's work finished.
        assert next_slot(anchor=100.0, slot=3, interval=25.0, now=176.4) == (4, 200.0)

    def test_slight_overrun_fires_immediately(self):
        # One second late on a 25s grid: the target is already past, so the
        # caller fires at once rather than waiting out a whole fresh interval.
        slot, target = next_slot(anchor=100.0, slot=1, interval=25.0, now=151.0)
        assert (slot, target) == (2, 150.0)

    def test_long_stall_rejoins_the_grid(self):
        # A 90s stall on a 25s grid must not machine-gun catch-up frames; the
        # missed slots are skipped and the next future grid point is used.
        slot, target = next_slot(anchor=100.0, slot=1, interval=25.0, now=216.0)
        assert (slot, target) == (5, 225.0)
        assert target >= 216.0

    def test_exactly_on_a_late_slot_boundary(self):
        # A full slot late lands exactly on the next grid point: fire now.
        slot, target = next_slot(anchor=0.0, slot=0, interval=10.0, now=20.0)
        assert (slot, target) == (2, 20.0)


class TestIntervalRunTiming:
    def test_no_trailing_interval_sleep_after_the_last_frame(self, sequencer, wait_for):
        started = time.monotonic()
        sequencer.start(Plan(frames=1, interval_s=5.0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=3)
        assert time.monotonic() - started < 2.0, \
            "the run waited out an interval after its final frame"

    def test_countdown_uses_the_cache_not_a_config_read(self, sequencer, camera,
                                                        camera_state):
        """A PTP config read between the slot boundary and the shutter firing
        would jitter the shot time, so _begin_exposure must never do one."""
        camera.snapshot()                      # the UI keeps the cache warm
        before = camera_state.calls["config"]
        sequencer._begin_exposure(None)
        assert camera_state.calls["config"] == before
        assert sequencer.exposing_until is not None   # 20s frame -> countdown
        sequencer._end_exposure()


class TestShutterCacheUpdates:
    def test_set_setting_updates_the_cache_in_place(self, camera, camera_state):
        camera.set_setting("shutterspeed", "1/60")
        before = camera_state.calls["config"]
        assert camera.shutter_seconds_cached() == pytest.approx(0.0166)
        assert camera_state.calls["config"] == before, "cached read hit the camera"

    def test_snapshot_warms_the_cache(self, camera, camera_state):
        camera.snapshot()
        before = camera_state.calls["config"]
        assert camera.shutter_seconds_cached() == pytest.approx(20.0)
        assert camera_state.calls["config"] == before

    def test_bulb_position_caches_no_duration(self, camera):
        camera.set_setting("shutterspeed", "Bulb")
        assert camera.shutter_seconds_cached() is None


class TestBulbTiming:
    def _held_for(self, camera_state) -> float:
        opens = [t for t, value in camera_state.bulb_events if value]
        closes = [t for t, value in camera_state.bulb_events if not value]
        assert len(opens) == 1 and len(closes) == 1
        return closes[0] - opens[0]

    def test_uses_single_config_for_the_toggles(self, camera, camera_state):
        camera.capture(exposure_s=0.3, bulb=True)
        assert camera_state.calls["single_config"] >= 2, \
            "bulb toggles should use the cheap single-config path"

    def test_exposure_is_not_stretched_by_config_reads(self, camera, camera_state):
        """Regression: closing the shutter used to re-walk the whole config
        tree while it was still open, stretching every bulb frame."""
        camera_state.cost["config"] = 0.2
        camera.capture(exposure_s=0.4, bulb=True)
        held = self._held_for(camera_state)
        assert held < 0.52, f"asked for 0.4s, shutter held open {held:.2f}s"
        assert held >= 0.38

    def test_fallback_path_prefetches_the_config_tree(self, camera, camera_state,
                                                      monkeypatch):
        """Without single-config support, the tree is fetched once *before*
        the shutter opens, never while it is open."""
        monkeypatch.delattr(type(camera._cam), "get_single_config", raising=False)
        camera_state.cost["config"] = 0.2
        camera.capture(exposure_s=0.4, bulb=True)
        held = self._held_for(camera_state)
        assert held < 0.52, f"asked for 0.4s, shutter held open {held:.2f}s"
        assert held >= 0.38


class TestBurstCollection:
    def test_files_behind_property_chatter_are_collected(self, camera, camera_state):
        """Nikon bodies interleave property-change events with file events;
        the drain must skip past them, not stop at the first one."""
        camera.trigger()
        camera.trigger()
        camera_state.event_noise = 5
        assert len(camera.collect_new_files(timeout_ms=50)) == 2

    def test_collect_stays_prompt_despite_chatter(self, camera, camera_state):
        camera_state.event_noise = 3
        started = time.monotonic()
        assert camera.collect_new_files() == []
        assert time.monotonic() - started < 0.5

    def test_burst_frame_count_survives_event_chatter(self, sequencer, camera_state,
                                                      wait_for):
        camera_state.event_noise = 8
        sequencer.start(Plan(frames=10, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 10
        assert sequencer.status()["state"] == "done"
