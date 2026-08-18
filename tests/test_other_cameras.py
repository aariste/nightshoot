"""Other camera brands.

Nothing is hard-coded to the Nikon Z50: widget names come from alias lists,
values are matched by meaning, and bulb / live view / trigger are probed. These
tests drive the same code against Canon- and Sony-shaped fakes.

They cannot prove NightShoot works on a real Canon — only that the code adapts
to the shapes those drivers present.
"""

import pytest

from nightshoot.camera import Camera, CameraError, resolve_choice
from nightshoot.sequencer import Plan

# Canon reports shutter speeds as ready-made fractions, not decimals.
CANON_SHUTTER = ["bulb", "30", "25", "20", "15", "10", "8", "4", "2", "1", "0.8",
                 "1/4", "1/15", "1/60", "1/125", "1/500", "1/1000", "1/4000"]


@pytest.fixture
def canon(camera_state, state_dir):
    """A Canon-shaped body: eosremoterelease, eosviewfinder, fraction strings."""
    camera_state.values.clear()
    camera_state.values.update({
        "shutterspeed": "1/60",
        "iso": "800",
        "aperture": "f/2.8",              # Canon uses 'aperture', not 'f-number'
        "imageformat": "RAW",
        "capturetarget": "card",
        "autoexposuremode": "Manual",     # not 'expprogram'
        "batterylevel": "72%",
        "eosremoterelease": "None",
        "eosviewfinder": 0,
        "eosfocusmode": "Manual",
    })
    camera_state.choices.clear()
    camera_state.choices.update({
        "shutterspeed": CANON_SHUTTER,
        "iso": ["100", "400", "800", "1600", "3200"],
        "aperture": ["f/1.8", "f/2.8", "f/4"],
        "imageformat": ["Large Fine JPEG", "RAW"],
        "capturetarget": ["Internal RAM", "card"],
        "autoexposuremode": ["Manual", "AV", "TV"],
        "eosremoterelease": ["None", "Press Half", "Press Full",
                             "Release Half", "Release Full", "Immediate"],
        "eosfocusmode": ["One Shot", "Manual"],
    })
    cam = Camera(thumb_dir=str(state_dir / "thumbs"))
    cam.connect()
    yield cam
    cam.disconnect()


@pytest.fixture
def sony(camera_state, state_dir):
    """A sparse body: no bulb control, no capture target, no live view flag."""
    camera_state.values.clear()
    camera_state.values.update({
        "shutterspeed": "0.0166s",
        "iso": "800",
        "f-number": "f/2.8",
        "imageformat": "RAW",
        "shootingmode": "M",
    })
    camera_state.choices.clear()
    camera_state.choices.update({
        "shutterspeed": ["0.0166s", "1.0000s", "30.0000s"],
        "iso": ["100", "800", "6400"],
        "f-number": ["f/2.8"],
        "imageformat": ["JPEG", "RAW"],
        "shootingmode": ["M", "A"],
    })
    cam = Camera(thumb_dir=str(state_dir / "thumbs"))
    cam.connect()
    yield cam
    cam.disconnect()


class TestCanonWidgetNames:
    def test_finds_aperture_under_its_canon_name(self, canon):
        assert canon.get_setting("aperture") == "f/2.8"

    def test_finds_exposure_mode_under_autoexposuremode(self, canon):
        assert canon.get_setting("exposuremode") == "Manual"

    def test_accepts_canon_manual_mode_without_warning(self, canon):
        assert not any("mode dial" in w for w in canon.prepare_for_night())

    def test_finds_focus_mode(self, canon):
        assert canon.get_setting("focusmode") == "Manual"

    def test_reports_the_model(self, canon):
        assert canon.snapshot()["connected"] is True


class TestCanonShutterValues:
    def test_fraction_strings_pass_straight_through(self, canon):
        canon.set_setting("shutterspeed", "1/500")
        assert canon.get_setting("shutterspeed") == "1/500"

    def test_a_decimal_request_still_matches(self, canon):
        """A script written for a Nikon should work here too."""
        assert resolve_choice("shutterspeed", "0.0166s", CANON_SHUTTER) == "1/60"
        assert resolve_choice("shutterspeed", "20", CANON_SHUTTER) == "20"

    def test_bulb_is_case_insensitive(self, canon):
        canon.set_setting("shutterspeed", "Bulb")
        assert canon.get_setting("shutterspeed") == "bulb"

    def test_still_refuses_a_speed_the_body_lacks(self, canon):
        with pytest.raises(CameraError):
            canon.set_setting("shutterspeed", "1/8000")


class TestCanonBulb:
    def test_uses_eosremoterelease(self, canon):
        strategy = canon.bulb_strategy()
        assert strategy is not None
        assert strategy[0] == "eosremoterelease"

    def test_picks_values_the_widget_actually_offers(self, canon):
        _, open_value, close_value = canon.bulb_strategy()
        assert open_value in canon.get_choices("eosremoterelease")
        assert close_value in canon.get_choices("eosremoterelease")

    def test_reported_as_supported(self, canon):
        assert canon.snapshot()["supports_bulb"] is True

    def test_capabilities_name_the_mechanism(self, canon):
        caps = canon.capabilities()
        assert caps["bulb"] is True
        assert caps["bulb_via"] == "eosremoterelease"

    def test_a_bulb_frame_drives_the_release(self, canon, camera_state):
        shot = canon.capture(exposure_s=0.2, bulb=True)
        assert shot.name.endswith(".NEF")
        # Left released, never stuck open.
        assert camera_state.values["eosremoterelease"] == "Release Full"


class TestCanonCaptureTarget:
    def test_matches_the_bodys_own_spelling(self, canon):
        canon.prepare_for_night(capture_to_card=True)
        assert canon.get_setting("capturetarget") == "card"

    def test_does_not_warn_about_a_different_spelling(self, canon):
        assert not any("capturetarget" in w for w in canon.prepare_for_night())


class TestCanonLiveView:
    def test_uses_eosviewfinder(self, canon):
        canon.set_liveview(True)
        assert canon.get_setting("viewfinder") == 1
        canon.set_liveview(False)
        assert canon.get_setting("viewfinder") == 0

    def test_reported_in_capabilities(self, canon):
        assert canon.capabilities()["liveview"] is True


class TestSparseBody:
    """A body missing several optional controls must still shoot."""

    def test_no_bulb_control_is_reported_not_crashed(self, sony):
        assert sony.bulb_strategy() is None
        assert sony.snapshot()["supports_bulb"] is False
        assert sony.capabilities()["bulb"] is False

    def test_bulb_capture_fails_with_a_clear_message(self, sony):
        with pytest.raises(CameraError, match="does not expose a bulb control"):
            sony.capture(exposure_s=1.0, bulb=True)

    def test_missing_capture_target_is_not_fatal(self, sony):
        warnings = sony.prepare_for_night()
        assert not any("capturetarget" in w for w in warnings)

    def test_missing_live_view_flag_is_not_fatal(self, sony):
        sony.set_liveview(True)          # must not raise

    def test_exposure_mode_found_under_shootingmode(self, sony):
        assert sony.get_setting("exposuremode") == "M"

    def test_plain_capture_still_works(self, sony):
        assert sony.capture().name.endswith(".NEF")

    def test_missing_widgets_are_absent_from_the_snapshot(self, sony):
        snap = sony.snapshot()
        assert snap["connected"] is True
        assert "batterylevel" not in snap        # this body does not report it


class TestSequencingOnOtherBodies:
    def test_interval_run_on_a_canon(self, canon, wait_for, state_dir):
        from nightshoot.sequencer import Sequencer
        seq = Sequencer(canon)
        seq.start(Plan(frames=3, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not seq.running)
        assert seq.status()["frames_done"] == 3
        assert seq.status()["errors"] == 0

    def test_script_preflight_uses_the_bodys_own_values(self, canon):
        from nightshoot import scripts as S
        from nightshoot.sequencer import Sequencer
        seq = Sequencer(canon)
        good = S.parse_script(
            "name: ok\nsteps:\n - set: {shutterspeed: '1/60'}\n - capture:\n", "ok.yaml")
        assert seq._preflight(good) == []
        bad = S.parse_script(
            "name: bad\nsteps:\n - set: {shutterspeed: '1/8000'}\n - capture:\n", "bad.yaml")
        assert seq._preflight(bad) != []

    def test_a_nikon_written_script_runs_on_a_canon(self, canon, wait_for):
        """Portable values are the point: '20' means 20 s on any body."""
        from nightshoot import scripts as S
        from nightshoot.sequencer import Sequencer
        seq = Sequencer(canon)
        script = S.parse_script(
            "name: portable\nsteps:\n - set: {shutterspeed: '20'}\n - capture:\n",
            "p.yaml")
        assert seq._preflight(script) == []
        seq.start_script(script)
        assert wait_for(lambda: not seq.running)
        assert canon.get_setting("shutterspeed") == "20"
