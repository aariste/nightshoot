"""Nikon scope.

NightShoot targets Nikon bodies. That still covers a wide range — a D5300 and a
Z9 disagree about widget names, shutter-speed spelling and which controls exist
at all — so these tests cover that range, plus what happens when something else
is plugged in.
"""

import pytest

from nightshoot.camera import Camera, CameraError
from nightshoot.sequencer import Plan, Sequencer


@pytest.fixture
def dslr(camera_state, state_dir):
    """An older Nikon DSLR: alternate widget names, no live view, no trigger."""
    camera_state.values.clear()
    camera_state.values.update({
        "shutterspeed2": "0.0166s",         # not 'shutterspeed'
        "isospeed": "800",                  # not 'iso'
        "f-number": "f/5.6",
        "imagequality": "NEF (Raw)",        # not 'imageformat'
        "capturetarget": "Memory card",
        "capturemode": "M",                 # not 'expprogram'
        "batterylevel": "60%",
        "focusmode2": "Manual",
    })
    camera_state.choices.clear()
    camera_state.choices.update({
        "shutterspeed2": ["0.0166s", "1.0000s", "20.0000s", "30.0000s", "Bulb"],
        "isospeed": ["100", "400", "800", "1600"],
        "f-number": ["f/3.5", "f/5.6"],
        "imagequality": ["JPEG Fine", "NEF (Raw)"],
        "capturetarget": ["Internal RAM", "Memory card"],
        "capturemode": ["M", "A", "S", "P"],
    })
    camera_state.model = "Nikon DSC D5300"
    cam = Camera(thumb_dir=str(state_dir / "thumbs"))
    cam.connect()
    yield cam
    cam.disconnect()


class TestNikonVariation:
    """Widget names differ across Nikon bodies and libgphoto2 versions."""

    def test_finds_shutterspeed2(self, dslr):
        assert dslr.get_setting("shutterspeed") == "0.0166s"

    def test_finds_isospeed(self, dslr):
        assert dslr.get_setting("iso") == "800"

    def test_finds_imagequality(self, dslr):
        assert dslr.get_setting("imageformat") == "NEF (Raw)"

    def test_finds_capturemode(self, dslr):
        assert dslr.get_setting("exposuremode") == "M"

    def test_finds_focusmode2(self, dslr):
        assert dslr.get_setting("focusmode") == "Manual"

    def test_no_mode_warning_when_already_manual(self, dslr):
        assert not any("mode dial" in w for w in dslr.prepare_for_night())

    def test_portable_values_still_resolve(self, dslr):
        dslr.set_setting("shutterspeed", "20")
        assert dslr.get_setting("shutterspeed") == "20.0000s"
        dslr.set_setting("shutterspeed", "1/60")
        assert dslr.get_setting("shutterspeed") == "0.0166s"

    def test_shoots_a_sequence(self, dslr, wait_for):
        seq = Sequencer(dslr)
        seq.start(Plan(frames=3, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not seq.running)
        assert seq.status()["frames_done"] == 3
        assert seq.status()["errors"] == 0


class TestMissingControls:
    """A body without bulb must say so, not fail on the first frame."""

    def test_bulb_reported_absent(self, dslr):
        assert dslr.supports_bulb() is False
        assert dslr.snapshot()["supports_bulb"] is False

    def test_bulb_capture_explains_itself(self, dslr):
        with pytest.raises(CameraError, match="not exposing a bulb control"):
            dslr.capture(exposure_s=1.0, bulb=True)

    def test_advice_mentions_the_mode_dial(self, dslr):
        with pytest.raises(CameraError, match="mode dial"):
            dslr.capture(exposure_s=1.0, bulb=True)

    def test_missing_live_view_is_not_fatal(self, dslr):
        dslr.set_liveview(True)          # must not raise

    def test_bulb_appears_when_the_widget_does(self, camera):
        """The Z50 fixture has the bulb toggle, so it is offered."""
        assert camera.supports_bulb() is True
        assert camera.snapshot()["supports_bulb"] is True


class TestVendorDetection:
    def test_a_nikon_is_recognised(self, camera):
        assert camera.is_nikon is True
        assert camera.snapshot()["vendor_warning"] is None

    def test_a_non_nikon_is_flagged(self, camera_state, state_dir):
        camera_state.model = "Canon EOS R6"
        cam = Camera(thumb_dir=str(state_dir / "thumbs"))
        cam.connect()
        try:
            assert cam.is_nikon is False
            warning = cam.snapshot()["vendor_warning"]
            assert warning and "not a Nikon" in warning
            assert "Canon EOS R6" in warning
        finally:
            cam.disconnect()

    def test_a_non_nikon_still_connects(self, camera_state, state_dir, wait_for):
        """Flagged, not blocked: much of this is plain PTP."""
        camera_state.model = "Canon EOS R6"
        cam = Camera(thumb_dir=str(state_dir / "thumbs"))
        cam.connect()
        try:
            assert cam.connected is True
            seq = Sequencer(cam)
            seq.start(Plan(frames=2, interval_s=0.2, start_delay_s=0))
            assert wait_for(lambda: not seq.running)
            assert seq.status()["frames_done"] == 2
        finally:
            cam.disconnect()

    def test_capabilities_report_the_vendor(self, camera):
        caps = camera.capabilities()
        assert caps["is_nikon"] is True
        assert caps["vendor_warning"] is None
        assert caps["bulb"] is True

    def test_status_endpoint_carries_the_warning(self, client, camera_state, state_dir,
                                                 monkeypatch):
        from nightshoot import app as appmod
        camera_state.model = "Sony ILCE-7M3"
        cam = Camera(thumb_dir=str(state_dir / "thumbs"))
        cam.connect()
        monkeypatch.setattr(appmod, "camera", cam)
        try:
            body = client.get("/api/status").get_json()
            assert body["camera"]["is_nikon"] is False
            assert "not a Nikon" in body["camera"]["vendor_warning"]
        finally:
            cam.disconnect()


class TestNoCanonCodePathsRemain:
    """Scope check: unverified vendor branches should be gone."""

    def test_no_canon_widget_aliases(self):
        from nightshoot.camera import CONFIG_ALIASES
        flat = [name for names in CONFIG_ALIASES.values() for name in names]
        assert not [n for n in flat if n.startswith("eos")]

    def test_no_bulb_strategy_table(self):
        import nightshoot.camera as cm
        assert not hasattr(cm, "BULB_STRATEGIES")
        assert not hasattr(cm.Camera, "bulb_strategy")
