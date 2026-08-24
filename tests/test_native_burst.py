"""Letting the camera drive its own burst, via PTP's BurstNumber property.

Firing frame by frame cannot reach the rate the shutter button gives you.
libgphoto2's Nikon path waits for the body to report itself idle after every
capture, polling DeviceReady at 100 ms, so each frame costs a poll interval
however short the exposure — which is why a naive loop stutters: a few fast
frames, a stall, one more, a stall.

BurstNumber (PTP 0x5018) moves that cost from once per frame to once per burst:
set it to N, fire once, and the camera runs its own continuous drive for N
frames. Confirmed on the Z50 in gphoto/libgphoto2#968.
"""

import time

import pytest

from nightshoot.sequencer import Plan


@pytest.fixture
def per_frame(camera_state, camera):
    """A body with no BurstNumber, which must fall back to per-frame triggers."""
    camera_state.burst_number_max = None
    camera._burst_probed = False
    camera._burst_limit = None
    return camera_state


class TestCapabilityDetection:
    def test_found_on_a_nikon(self, camera):
        assert camera.burst_number_limit() == 65535

    def test_absent_bodies_report_none(self, per_frame, camera):
        assert camera.burst_number_limit() is None

    def test_a_read_only_widget_is_not_usable(self, camera_state, camera):
        camera_state.readonly.add("burstnumber")
        camera._burst_probed = False
        assert camera.burst_number_limit() is None

    def test_probed_once_per_connection(self, camera, camera_state):
        camera.burst_number_limit()
        before = camera_state.calls["config"]
        for _ in range(5):
            camera.burst_number_limit()
        assert camera_state.calls["config"] == before, "capability re-probed"


class TestArmingAndDisarming:
    def test_sets_the_value(self, camera, camera_state):
        camera.set_burst_number(8)
        assert camera_state.values["burstnumber"] == 8

    def test_repeat_writes_are_skipped(self, camera, camera_state):
        """Every config write is a USB round trip — the thing a burst cannot spare."""
        camera.set_burst_number(8)
        before = camera_state.calls["config"]
        camera.set_burst_number(8)
        assert camera_state.calls["config"] == before

    def test_reset_returns_to_one(self, camera, camera_state):
        camera.set_burst_number(8)
        camera.reset_burst_number()
        assert camera_state.values["burstnumber"] == 1

    def test_reset_survives_a_camera_error(self, camera, monkeypatch):
        """It runs on the way out of a burst; raising would mask the real cause."""
        from nightshoot.camera import CameraError

        camera.set_burst_number(8)

        def refuse(key, value):
            raise CameraError("camera went away")

        monkeypatch.setattr(camera, "set_setting", refuse)
        camera.reset_burst_number()          # must not raise
        # And it must believe it is disarmed, so a later burst re-arms properly.
        assert camera._burst_number == 1


class TestBurstUsesTheCamera:
    def test_one_trigger_per_chunk_not_per_frame(self, sequencer, camera_state,
                                                 wait_for):
        sequencer.start(Plan(frames=24, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 24
        # 24 frames in chunks of 8 is 3 triggers, not 24.
        assert camera_state.calls["trigger"] == 3

    def test_every_frame_is_counted(self, sequencer, camera_state, wait_for):
        """Files land after the trigger returns, so the drain must wait for them."""
        camera_state.write_s = 0.05
        sequencer.start(Plan(frames=16, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 16

    def test_a_short_burst_does_not_overshoot(self, sequencer, camera_state,
                                              wait_for):
        sequencer.start(Plan(frames=3, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 3
        assert camera_state.calls["trigger"] == 1
        assert camera_state.values["burstnumber"] == 1, "left armed after the burst"

    def test_faster_than_firing_frame_by_frame(self, sequencer, camera_state,
                                               camera, wait_for):
        """The whole point. Same camera, same frames, both paths timed."""
        def run(burst_max):
            camera_state.reset()
            camera_state.cost["trigger"] = 0.05     # the device-ready poll floor
            camera_state.burst_frame_s = 0.01       # native continuous rate
            camera_state.burst_number_max = burst_max
            camera._burst_probed = False
            camera._burst_limit = None
            camera._burst_number = 1
            started = time.monotonic()
            sequencer.start(Plan(frames=24, interval_s=0, start_delay_s=0))
            assert wait_for(lambda: not sequencer.running, timeout=120)
            assert sequencer.status()["frames_done"] == 24
            return time.monotonic() - started

        per_frame_s = run(None)
        native_s = run(65535)
        assert native_s < per_frame_s * 0.75, (
            f"native burst {native_s:.2f}s was not clearly faster than "
            f"per-frame {per_frame_s:.2f}s")


class TestTheCameraIsLeftUsable:
    def test_burstnumber_is_reset_when_the_burst_ends(self, sequencer, camera_state,
                                                      wait_for):
        sequencer.start(Plan(frames=16, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.values["burstnumber"] == 1

    def test_burstnumber_is_reset_after_a_stop(self, sequencer, camera_state,
                                               wait_for):
        camera_state.burst_frame_s = 0.02
        sequencer.start(Plan(frames=0, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0, timeout=60)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert camera_state.values["burstnumber"] == 1

    def test_a_later_single_frame_is_not_a_burst(self, sequencer, camera_state,
                                                 wait_for):
        """Leaving it armed would silently turn every later frame into a burst."""
        sequencer.start(Plan(frames=8, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)

        before = sequencer.status()["frames_done"]
        sequencer.start(Plan(frames=1, interval_s=30, start_delay_s=0, download=True))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 1, "single frame fired a burst"
        assert before == 8

    def test_a_stale_armed_value_is_cleared_before_a_run(self, sequencer,
                                                         camera_state, wait_for):
        """A run that died mid-burst must not poison the next night."""
        camera_state.values["burstnumber"] = 20
        sequencer.start(Plan(frames=1, interval_s=30, start_delay_s=0, download=True))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 1


class TestControlDuringANativeBurst:
    def test_stop_takes_effect_within_a_chunk(self, sequencer, camera_state,
                                              wait_for):
        camera_state.burst_frame_s = 0.03      # a chunk of 8 is ~0.24s
        sequencer.start(Plan(frames=0, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0, timeout=60)
        started = time.monotonic()
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=30)
        assert time.monotonic() - started < 5.0, "stop waited for the whole burst"

    def test_a_timed_burst_respects_its_deadline(self, sequencer, camera_state,
                                                 wait_for):
        camera_state.burst_frame_s = 0.02
        started = time.monotonic()
        sequencer.start_script(_script("steps:\n - burst: {seconds: 1}\n"))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        elapsed = time.monotonic() - started
        # Chunks are bounded, so overshoot is bounded too.
        assert 0.9 <= elapsed < 4.0, f"a 1s burst took {elapsed:.1f}s"
        assert sequencer.status()["frames_done"] > 0

    def test_frames_bounded_script_burst_is_exact(self, sequencer, wait_for):
        sequencer.start_script(_script(
            "steps:\n - capture:\n - burst: {frames: 10}\n - capture:\n"))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 12


class TestFallback:
    def test_used_when_the_body_has_no_burstnumber(self, sequencer, per_frame,
                                                   wait_for):
        sequencer.start(Plan(frames=6, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 6
        assert per_frame.calls["trigger"] == 6, "should be one trigger per frame"

    def test_a_body_that_cannot_arm_still_shoots(self, sequencer, camera,
                                                 camera_state, monkeypatch,
                                                 wait_for):
        """The widget exists but refuses the write — shoot anyway, slowly."""
        from nightshoot.camera import CameraError

        def refuse(key, value):
            if key == "burstnumber" and value != 1:
                raise CameraError("nope")

        monkeypatch.setattr(camera, "set_setting", refuse)
        sequencer.start(Plan(frames=4, interval_s=0, start_delay_s=0,
                             max_consecutive_errors=99))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] > 0


def _script(text):
    from nightshoot import scripts as S
    return S.parse_script(text, "t.yaml")
