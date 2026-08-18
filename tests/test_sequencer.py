"""Sequencer: interval runs, script runs, pacing, pause/stop, error handling."""

import time

import pytest

from nightshoot import scripts as S
from nightshoot.sequencer import Plan


class TestPlanValidation:
    def test_accepts_a_sane_plan(self):
        assert Plan(frames=10, interval_s=5).validate() == []

    def test_zero_interval_means_as_fast_as_possible(self):
        assert Plan(frames=5, interval_s=0).validate() == []

    def test_rejects_a_negative_interval(self):
        assert Plan(frames=5, interval_s=-1).validate() != []

    def test_bulb_needs_room_for_the_exposure(self):
        assert Plan(bulb=True, exposure_s=30, interval_s=30).validate() != []
        assert Plan(bulb=True, exposure_s=30, interval_s=35).validate() == []

    def test_bulb_needs_an_interval_at_all(self):
        assert Plan(bulb=True, exposure_s=10, interval_s=0).validate() != []


class TestIntervalRuns:
    def test_captures_the_requested_frames(self, sequencer, wait_for):
        sequencer.start(Plan(frames=4, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        status = sequencer.status()
        assert status["frames_done"] == 4
        assert status["state"] == "done"
        assert status["errors"] == 0

    def test_paces_itself(self, sequencer, wait_for):
        started = time.time()
        sequencer.start(Plan(frames=3, interval_s=0.5, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        assert 0.9 < time.time() - started < 2.0

    def test_honours_the_start_delay(self, sequencer):
        sequencer.start(Plan(frames=1, interval_s=1, start_delay_s=5))
        time.sleep(0.3)
        assert sequencer.status()["frames_done"] == 0
        sequencer.stop()

    def test_reports_the_measured_cycle_time(self, sequencer, wait_for):
        sequencer.start(Plan(frames=4, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["last_cycle_s"] is not None

    def test_refuses_to_start_twice(self, sequencer):
        sequencer.start(Plan(frames=0, interval_s=0.2, start_delay_s=0))
        with pytest.raises(RuntimeError):
            sequencer.start(Plan(frames=1, interval_s=1))
        sequencer.stop()


class TestPerFrameOverhead:
    """Regression: config reads and previews once cost more than the exposure."""

    def test_config_reads_do_not_scale_with_frame_count(self, sequencer, camera_state,
                                                        wait_for):
        """Setup costs are fine; per-frame costs are not."""
        sequencer.start(Plan(frames=5, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        few = camera_state.calls["config"]

        camera_state.calls["config"] = 0
        sequencer.start(Plan(frames=25, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        many = camera_state.calls["config"]

        # Five times the frames must not mean five times the config reads.
        assert many <= few + 2, (
            f"{few} reads for 5 frames but {many} for 25 — reads scale per frame")

    def test_previews_are_throttled(self, sequencer, camera_state, wait_for):
        sequencer.start(Plan(frames=10, interval_s=0, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        assert camera_state.calls["preview"] <= 3


class TestPauseAndStop:
    def test_pause_holds_and_resume_continues(self, sequencer, wait_for):
        sequencer.start(Plan(frames=0, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0)
        sequencer.pause()
        assert wait_for(lambda: sequencer.status()["state"] == "paused")

        done = sequencer.status()["frames_done"]
        time.sleep(0.6)
        assert sequencer.status()["frames_done"] == done

        sequencer.resume()
        assert wait_for(lambda: sequencer.status()["frames_done"] > done)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running)

    def test_stop_is_prompt_even_at_full_speed(self, sequencer, wait_for):
        sequencer.start(Plan(frames=0, interval_s=0, start_delay_s=0))
        time.sleep(0.4)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=5)

    def test_stop_interrupts_a_long_wait(self, sequencer, wait_for):
        sequencer.start(Plan(frames=5, interval_s=1, start_delay_s=600))
        time.sleep(0.3)
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=5)
        assert sequencer.status()["frames_done"] == 0


class TestErrorHandling:
    def test_retries_transient_failures(self, sequencer, camera_state, wait_for):
        camera_state.fail_captures = 2
        sequencer.start(Plan(frames=1, interval_s=0.1, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running, timeout=60)
        assert sequencer.status()["frames_done"] == 1
        assert sequencer.status()["errors"] == 2

    def test_gives_up_after_the_limit(self, sequencer, camera_state, wait_for):
        camera_state.fail_captures = 99
        sequencer.start(Plan(frames=1, interval_s=0.1, start_delay_s=0,
                             max_consecutive_errors=3))
        assert wait_for(lambda: not sequencer.running, timeout=90)
        assert sequencer.status()["state"] == "error"
        assert sequencer.status()["errors"] == 3


class TestExposureCountdown:
    def test_counts_down_a_long_frame(self, sequencer, camera_state, wait_for):
        camera_state.cost["capture"] = 0.8
        sequencer.start(Plan(frames=1, interval_s=0.1, start_delay_s=0))
        assert wait_for(lambda: sequencer.status()["exposure_left"] is not None,
                        timeout=3)
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["exposure_left"] is None

    def test_no_countdown_for_a_fast_frame(self, sequencer, camera_state):
        camera_state.values["shutterspeed"] = "0.0166s"
        sequencer.start(Plan(frames=1, interval_s=0.1, start_delay_s=0))
        time.sleep(0.15)
        assert sequencer.status()["exposure_left"] is None


class TestScriptRuns:
    def test_runs_steps_in_order(self, sequencer, camera, wait_for):
        script = S.parse_script(
            "name: ordered\nvars: {myiso: '1600'}\nsteps:\n"
            "  - set: {iso: '{{myiso}}'}\n"
            "  - capture:\n"
            "  - for_each: {setting: shutterspeed, values: ['1/60','4']}\n"
            "    steps:\n      - capture:\n", "t.yaml")
        sequencer.start_script(script)
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["frames_done"] == 3
        assert sequencer.status()["state"] == "done"
        assert camera.get_setting("iso") == "1600"

    def test_reports_script_mode(self, sequencer, wait_for):
        sequencer.start_script(S.parse_script("name: m\nsteps:\n - capture:\n", "m.yaml"))
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["mode"] == "script"
        assert sequencer.status()["script_name"] == "m"

    def test_loop_index_is_bound(self, sequencer, wait_for):
        sequencer.start_script(S.parse_script(
            "name: loopy\nsteps:\n - repeat: 3\n   steps:\n"
            "    - message: 'pass {{i}}'\n    - capture:\n", "t.yaml"))
        assert wait_for(lambda: not sequencer.running)
        log = "\n".join(sequencer.status()["log"])
        assert all(f"pass {n}" in log for n in (1, 2, 3))
        assert "pass 4" not in log

    def test_every_is_slot_aligned(self, sequencer, wait_for):
        """Three passes on a 0.4s cadence take ~0.8s, not 1.2s."""
        started = time.time()
        sequencer.start_script(S.parse_script(
            "name: paced\nsteps:\n - repeat: 3\n   every: 0.4\n"
            "   steps:\n    - capture:\n", "t.yaml"))
        assert wait_for(lambda: not sequencer.running)
        assert 0.6 < time.time() - started < 1.5

    def test_wait_until_does_not_block_stop(self, sequencer, wait_for):
        sequencer.start_script(S.parse_script(
            "name: w\nsteps:\n - wait_until: '04:00'\n - capture:\n", "t.yaml"))
        assert wait_for(lambda: sequencer.status()["state"] == "waiting")
        sequencer.stop()
        assert wait_for(lambda: not sequencer.running, timeout=5)
        assert sequencer.status()["frames_done"] == 0

    def test_pause_works_inside_a_script(self, sequencer, wait_for):
        sequencer.start_script(S.parse_script(
            "name: loop\nsteps:\n - repeat: forever\n   every: 0.2\n"
            "   steps:\n    - capture:\n", "t.yaml"))
        assert wait_for(lambda: sequencer.status()["frames_done"] > 0)
        sequencer.pause()
        assert wait_for(lambda: sequencer.status()["state"] == "paused")
        sequencer.stop()


class TestPreflight:
    def test_rejects_a_value_this_camera_cannot_reach(self, sequencer):
        script = S.parse_script(
            "name: bad\nsteps:\n - set: {shutterspeed: '22'}\n - capture:\n", "bad.yaml")
        with pytest.raises(ValueError, match="Closest is"):
            sequencer.start_script(script)
        assert not sequencer.running
        assert sequencer.status()["frames_done"] == 0

    def test_rejects_an_unknown_setting(self, sequencer):
        script = S.parse_script(
            "name: bad\nsteps:\n - set: {nosuch: '1'}\n - capture:\n", "bad.yaml")
        with pytest.raises(ValueError, match="does not expose"):
            sequencer.start_script(script)

    def test_checks_for_each_values_too(self, sequencer):
        script = S.parse_script(
            "name: fe\nsteps:\n - for_each: {setting: shutterspeed, values: ['20','999']}\n"
            "   steps:\n    - capture:\n", "fe.yaml")
        with pytest.raises(ValueError):
            sequencer.start_script(script)

    def test_allows_a_valid_script(self, sequencer, wait_for):
        script = S.parse_script(
            "name: ok\nsteps:\n - set: {shutterspeed: '20'}\n - capture:\n", "ok.yaml")
        sequencer.start_script(script)
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["frames_done"] == 1

    def test_shipped_examples_pass(self, sequencer, examples_dir, scripts_dir):
        import os
        import shutil
        for name in os.listdir(examples_dir):
            if name.endswith(".yaml"):
                shutil.copy(os.path.join(examples_dir, name), scripts_dir)
        for name in os.listdir(scripts_dir):
            script = S.load_script(str(scripts_dir), name)
            assert sequencer._preflight(script) == [], f"{name} failed pre-flight"
