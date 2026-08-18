"""Camera layer: settings, portable value matching, live view, non-blocking status."""

import threading
import time

import pytest

from nightshoot.camera import CameraError, parse_duration, resolve_choice
from tests.conftest import Z50_SHUTTER


class TestDurationParsing:
    @pytest.mark.parametrize("text,expected", [
        ("20", 20.0), ("20.0000s", 20.0), ("1/60", 1 / 60), ("0.0166s", 0.0166),
        ("30.0000s", 30.0), ("1/8000", 1 / 8000), ("2.5", 2.5), ("1/60s", 1 / 60),
        (25, 25.0), (0.5, 0.5),
    ])
    def test_parses(self, text, expected):
        assert parse_duration(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["Bulb", "Time", "Automatic", "", "f/2.8", None])
    def test_not_a_duration(self, text):
        assert parse_duration(text) is None


class TestResolveChoice:
    """Bodies spell the same setting differently; scripts must stay portable."""

    @pytest.mark.parametrize("given,expected", [
        ("20", "20.0000s"), ("20.0000s", "20.0000s"), ("25", "25.0000s"),
        ("30", "30.0000s"), ("1", "1.0000s"), ("1/60", "0.0166s"),
        ("1/100", "0.0100s"), ("1/4", "0.2500s"), ("0.0166s", "0.0166s"),
        ("bulb", "Bulb"), ("Bulb", "Bulb"), ("BULB", "Bulb"), ("time", "Time"),
    ])
    def test_matches_by_meaning(self, given, expected):
        assert resolve_choice("shutterspeed", given, Z50_SHUTTER) == expected

    def test_refuses_a_near_miss_rather_than_rounding(self):
        with pytest.raises(CameraError) as exc:
            resolve_choice("shutterspeed", "22", Z50_SHUTTER)
        assert "Closest is" in str(exc.value)

    def test_accepts_the_cameras_truncated_fast_speeds(self):
        """1/4000 is 0.00025 s but a Z50 reports it as '0.0002s'."""
        assert resolve_choice("shutterspeed", "1/4000", Z50_SHUTTER) == "0.0002s"
        assert resolve_choice("shutterspeed", "1/3200", Z50_SHUTTER) == "0.0003s"
        assert resolve_choice("shutterspeed", "1/1600", Z50_SHUTTER) == "0.0006s"
        assert resolve_choice("shutterspeed", "1/800", Z50_SHUTTER) == "0.0012s"
        assert resolve_choice("shutterspeed", "1/640", Z50_SHUTTER) == "0.0015s"
        assert resolve_choice("shutterspeed", "1/320", Z50_SHUTTER) == "0.0031s"
        assert resolve_choice("shutterspeed", "1/160", Z50_SHUTTER) == "0.0062s"

    def test_truncation_tolerance_does_not_blur_slow_speeds(self):
        """The absolute allowance must not let 22 s pass as 20 s."""
        with pytest.raises(CameraError):
            resolve_choice("shutterspeed", "22", Z50_SHUTTER)
        with pytest.raises(CameraError):
            resolve_choice("shutterspeed", "18", Z50_SHUTTER)

    def test_rejects_a_value_the_camera_cannot_reach(self):
        with pytest.raises(CameraError):
            resolve_choice("shutterspeed", "1/8000", Z50_SHUTTER)
    def test_long_option_lists_stay_readable(self):
        with pytest.raises(CameraError) as exc:
            resolve_choice("shutterspeed", "nonsense", Z50_SHUTTER)
        assert "more)" in str(exc.value)
        assert len(str(exc.value)) < 320

    def test_case_insensitive_for_plain_choices(self):
        assert resolve_choice("imageformat", "nef (raw)",
                              ["JPEG Fine", "NEF (Raw)"]) == "NEF (Raw)"


class TestSettings:
    def test_reads_and_writes(self, camera):
        assert camera.get_setting("shutterspeed") == "20.0000s"
        camera.set_setting("iso", "3200")
        assert camera.get_setting("iso") == "3200"

    def test_aliases_map_to_the_camera_name(self, camera):
        assert camera.get_setting("aperture") == "f/2.8"

    def test_stores_the_cameras_own_string(self, camera):
        camera.set_setting("shutterspeed", "20")
        assert camera.get_setting("shutterspeed") == "20.0000s"
        camera.set_setting("shutterspeed", "1/60")
        assert camera.get_setting("shutterspeed") == "0.0166s"

    def test_rejects_invalid_values(self, camera):
        with pytest.raises(CameraError):
            camera.set_setting("iso", "999999")

    def test_rejects_unknown_widgets(self, camera):
        with pytest.raises(CameraError, match="does not expose"):
            camera.set_setting("nosuchthing", "1")

    def test_reports_read_only_helpfully(self, camera, camera_state):
        camera_state.readonly.add("iso")
        with pytest.raises(CameraError, match="mode dial"):
            camera.set_setting("iso", "800")

    def test_check_setting_validates_without_applying(self, camera):
        camera.check_setting("shutterspeed", "25")
        assert camera.get_setting("shutterspeed") == "20.0000s"
        with pytest.raises(CameraError):
            camera.check_setting("shutterspeed", "22")


class TestSnapshot:
    def test_reports_the_essentials(self, camera):
        snap = camera.snapshot()
        assert snap["connected"] is True
        assert snap["model"] == "Nikon Z50"
        assert snap["shutter_seconds"] == pytest.approx(20.0)

    def test_never_blocks_during_a_long_exposure(self, camera, camera_state):
        """Regression: capture holds the camera lock for the whole exposure."""
        camera_state.cost["capture"] = 2.0
        worker = threading.Thread(target=camera.capture, daemon=True)
        worker.start()
        time.sleep(0.3)

        started = time.time()
        snap = camera.snapshot()
        assert time.time() - started < 1.0, "status blocked behind the shutter"
        assert snap["connected"] is True
        assert snap["busy"] is True
        assert snap["shutterspeed"] == "20.0000s", "should serve cached values"
        worker.join(timeout=10)


class TestShutterCache:
    def test_repeated_lookups_do_not_re_read_the_camera(self, camera, camera_state):
        camera.shutter_seconds()
        camera_state.calls["config"] = 0
        for _ in range(20):
            camera.shutter_seconds()
        assert camera_state.calls["config"] == 0

    def test_changing_the_shutter_invalidates_it(self, camera):
        assert camera.shutter_seconds() == pytest.approx(20.0)
        camera.set_setting("shutterspeed", "1/60")
        assert camera.shutter_seconds() == pytest.approx(0.0166)


class TestThumbnailThrottle:
    def test_first_frame_always_previews(self, camera):
        camera.begin_run(thumb_min_interval=1.5)
        assert camera.capture().thumb_path is not None

    def test_fast_following_frames_do_not(self, camera, camera_state):
        camera.begin_run(thumb_min_interval=1.5)
        camera.capture()
        camera_state.calls["preview"] = 0
        assert camera.capture().thumb_path is None
        assert camera_state.calls["preview"] == 0

    def test_single_shots_are_never_throttled(self, camera):
        camera.begin_run(thumb_min_interval=0.0)
        assert camera.capture().thumb_path is not None
        assert camera.capture().thumb_path is not None


class TestStorage:
    def test_reports_card_space(self, camera):
        card = camera.storage(max_age=0)
        assert card["free_gb"] == pytest.approx(29.6, abs=0.1)
        assert card["free_images"] == 1180

    def test_is_cached(self, camera, camera_state):
        camera.storage(max_age=0)
        camera_state.calls["storage"] = 0
        camera.storage()
        camera.storage()
        assert camera_state.calls["storage"] == 0

    def test_degrades_quietly_when_unsupported(self, camera, camera_state):
        camera_state.storage_supported = False
        camera.invalidate_storage()
        camera._storage_cache = None
        assert camera.storage() is None


class TestLiveView:
    def test_returns_jpeg_bytes(self, camera):
        frame = camera.preview()
        assert frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")

    def test_toggles_the_viewfinder_widget(self, camera):
        camera.set_liveview(True)
        assert camera.get_setting("viewfinder") == 1
        camera.set_liveview(False)
        assert camera.get_setting("viewfinder") == 0

    def test_surfaces_failures_with_advice(self, camera, camera_state):
        camera_state.preview_fails = True
        with pytest.raises(CameraError, match="live view failed"):
            camera.preview()


class TestCapture:
    def test_saves_a_thumbnail(self, camera):
        shot = camera.capture()
        assert shot.name.endswith(".NEF")
        assert shot.thumb_path is not None

    def test_downloads_when_asked(self, camera):
        shot = camera.capture(download=True)
        assert shot.saved_path is not None

    def test_wraps_driver_errors(self, camera, camera_state):
        camera_state.fail_captures = 1
        with pytest.raises(CameraError, match="capture failed"):
            camera.capture()


class TestPrepareForNight:
    def test_warns_about_the_mode_dial(self, camera, camera_state):
        camera_state.values["expprogram"] = "A"
        assert any("mode" in w.lower() for w in camera.prepare_for_night())

    def test_warns_about_long_exposure_nr(self, camera, camera_state):
        camera_state.values["longexpnr"] = "1"
        assert any("Long Exposure NR" in w for w in camera.prepare_for_night())

    def test_quiet_when_set_up_correctly(self, camera):
        assert camera.prepare_for_night() == []
