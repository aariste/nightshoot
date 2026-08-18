"""Shared fixtures.

libgphoto2 is a C library and the camera is physical, so the whole test suite
runs against a stub that behaves like a Nikon Z50: the same widget names, the
same shutter-speed spellings, and the same costs for the operations that matter
(a config read and a preview download both dwarf a fast exposure).
"""

from __future__ import annotations

import os
import sys
import types

import pytest

# The stub must be importable as "gphoto2" before nightshoot is imported.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------- the fake camera

#: Exactly what a real Nikon Z50 reports over PTP.
Z50_SHUTTER = [
    "0.0002s", "0.0003s", "0.0004s", "0.0005s", "0.0006s", "0.0008s", "0.0010s",
    "0.0012s", "0.0015s", "0.0020s", "0.0025s", "0.0031s", "0.0040s", "0.0050s",
    "0.0062s", "0.0080s", "0.0100s", "0.0125s", "0.0166s", "0.0200s", "0.0250s",
    "0.0333s", "0.0400s", "0.0500s", "0.0666s", "0.0769s", "0.1000s", "0.1250s",
    "0.1666s", "0.2000s", "0.2500s", "0.3333s", "0.4000s", "0.5000s", "0.6250s",
    "0.7692s", "1.0000s", "1.3000s", "1.6000s", "2.0000s", "2.5000s", "3.0000s",
    "4.0000s", "5.0000s", "6.0000s", "8.0000s", "10.0000s", "13.0000s",
    "15.0000s", "20.0000s", "25.0000s", "30.0000s", "Bulb", "Time",
]

WIDGET_NAMES = [
    "GP_WIDGET_WINDOW", "GP_WIDGET_SECTION", "GP_WIDGET_TEXT", "GP_WIDGET_RANGE",
    "GP_WIDGET_TOGGLE", "GP_WIDGET_RADIO", "GP_WIDGET_MENU", "GP_WIDGET_BUTTON",
    "GP_WIDGET_DATE",
]
OTHER_CONSTANTS = [
    "GP_CAPTURE_IMAGE", "GP_FILE_TYPE_PREVIEW", "GP_FILE_TYPE_NORMAL",
    "GP_EVENT_FILE_ADDED", "GP_EVENT_TIMEOUT", "GP_ERROR_IO",
    "GP_ERROR_IO_USB_CLAIM", "GP_ERROR_IO_USB_FIND", "GP_ERROR_CAMERA_ERROR",
    "GP_ERROR_TIMEOUT", "GP_ERROR_MODEL_NOT_FOUND",
]


class FakeCameraState:
    """Everything a test might want to inspect or bend."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.values = {
            "shutterspeed": "20.0000s", "iso": "800", "f-number": "f/2.8",
            "imageformat": "NEF (Raw)", "capturetarget": "Memory card",
            "expprogram": "M", "batterylevel": "84%", "bulb": 0,
            "longexpnr": "0", "focusmode": "Manual", "viewfinder": 0,
        }
        self.choices = {
            "shutterspeed": list(Z50_SHUTTER),
            "iso": ["100", "200", "400", "800", "1600", "3200", "6400"],
            "f-number": ["f/1.8", "f/2.8", "f/4"],
            "imageformat": ["JPEG Fine", "NEF (Raw)"],
            "capturetarget": ["Internal RAM", "Memory card"],
        }
        self.readonly = set()
        self.counter = 0
        self.calls = {"config": 0, "preview": 0, "capture": 0, "storage": 0, "trigger": 0}
        # Modelled on a real Z50 over USB 2: a synchronous capture waits for the
        # file, a trigger does not.
        self.cost = {"config": 0.0, "preview": 0.0, "capture": 0.0, "trigger": 0.0}
        self.fail_captures = 0
        self.preview_fails = False
        self.storage_supported = True
        self.free_images = 1180
        self.supports_trigger = True
        self.pending_files = []      # fired but not yet reported as written
        self.buffer_limit = 0        # 0 = unlimited; else error when full


STATE = FakeCameraState()


def _build_module() -> types.ModuleType:
    import time as _time

    gp = types.ModuleType("gphoto2")
    for index, name in enumerate(WIDGET_NAMES):
        setattr(gp, name, index)
    for index, name in enumerate(OTHER_CONSTANTS):
        setattr(gp, name, -index)

    class GPhoto2Error(Exception):
        def __init__(self, code=-1, msg="fake gphoto2 error"):
            super().__init__(msg)
            self.code = code

    gp.GPhoto2Error = GPhoto2Error

    class CameraFilePath:
        def __init__(self, name):
            self.folder = "/store_00010001/DCIM/100NZ_50"
            self.name = name

    class Widget:
        def __init__(self, name):
            self._name = name

        def get_name(self):
            return self._name

        def get_label(self):
            return self._name

        def get_value(self):
            return STATE.values[self._name]

        def set_value(self, value):
            STATE.values[self._name] = value
            # Closing the bulb shutter makes the camera write a file, exactly
            # as the real one does.
            if self._name == "bulb" and not value:
                STATE.counter += 1
                STATE.pending_files.append(
                    CameraFilePath(f"DSC_{STATE.counter:04d}.NEF"))

        def get_readonly(self):
            return self._name in STATE.readonly

        def get_type(self):
            if self._name in ("bulb", "viewfinder"):
                return gp.GP_WIDGET_TOGGLE
            if self._name in STATE.choices:
                return gp.GP_WIDGET_RADIO
            return gp.GP_WIDGET_TEXT

        def count_choices(self):
            return len(STATE.choices.get(self._name, []))

        def get_choice(self, index):
            return STATE.choices[self._name][index]

    class Config:
        def get_child_by_name(self, name):
            if name not in STATE.values:
                raise GPhoto2Error(-1, f"no widget named {name}")
            return Widget(name)

    class CameraFile:
        def __init__(self, data=b"\xff\xd8fake-jpeg\xff\xd9"):
            self._data = data

        def get_data_and_size(self):
            return self._data

        def save(self, path):
            with open(path, "wb") as handle:
                handle.write(self._data)

    class StorageInfo:
        capacitykbytes = 62_000_000          # ~59 GB card
        freekbytes = 31_000_000              # ~29.6 GB free

        def __init__(self, free_images):
            self.freeimages = free_images

    class Camera:
        def init(self):
            pass

        def exit(self):
            pass

        def get_config(self):
            STATE.calls["config"] += 1
            _time.sleep(STATE.cost["config"])
            return Config()

        def set_config(self, config):
            pass

        def get_summary(self):
            return "Nikon Z50"

        def capture(self, capture_type):
            if STATE.fail_captures > 0:
                STATE.fail_captures -= 1
                raise GPhoto2Error(-1, "simulated USB drop")
            STATE.calls["capture"] += 1
            _time.sleep(STATE.cost["capture"])
            STATE.counter += 1
            return CameraFilePath(f"DSC_{STATE.counter:04d}.NEF")

        def wait_for_event(self, timeout_ms):
            # Files fired by trigger_capture surface here, as on a real camera.
            if STATE.pending_files:
                return (gp.GP_EVENT_FILE_ADDED, STATE.pending_files.pop(0))
            _time.sleep(min(timeout_ms, 5) / 1000.0)
            return (gp.GP_EVENT_TIMEOUT, None)

        def trigger_capture(self):
            if not STATE.supports_trigger:
                raise GPhoto2Error(-1, "trigger not supported")
            if STATE.buffer_limit and len(STATE.pending_files) >= STATE.buffer_limit:
                raise GPhoto2Error(-1, "camera buffer full")
            STATE.calls["trigger"] += 1
            _time.sleep(STATE.cost["trigger"])
            STATE.counter += 1
            STATE.pending_files.append(CameraFilePath(f"DSC_{STATE.counter:04d}.NEF"))

        def file_get(self, folder, name, file_type):
            STATE.calls["preview"] += 1
            _time.sleep(STATE.cost["preview"])
            return CameraFile()

        def capture_preview(self):
            if STATE.preview_fails:
                raise GPhoto2Error(-1, "camera is not in live view")
            return CameraFile()

        def get_storageinfo(self):
            STATE.calls["storage"] += 1
            if not STATE.storage_supported:
                raise GPhoto2Error(-1, "storage info unsupported")
            return [StorageInfo(STATE.free_images)]

    gp.Camera = Camera
    return gp


sys.modules.setdefault("gphoto2", _build_module())


# ------------------------------------------------------------------- fixtures

@pytest.fixture
def camera_state():
    """The fake camera's mutable state, reset for every test."""
    STATE.reset()
    return STATE


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A throwaway NIGHTSHOOT_STATE tree."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("NIGHTSHOOT_STATE", str(tmp_path))
    monkeypatch.setenv("NIGHTSHOOT_SCRIPTS", str(scripts))
    return tmp_path


@pytest.fixture
def scripts_dir(state_dir):
    return state_dir / "scripts"


@pytest.fixture
def camera(camera_state, state_dir):
    from nightshoot.camera import Camera

    cam = Camera(thumb_dir=str(state_dir / "thumbs"),
                 download_dir=str(state_dir / "captures"))
    cam.connect()
    yield cam
    cam.disconnect()


@pytest.fixture
def sequencer(camera):
    from nightshoot.sequencer import Sequencer

    seq = Sequencer(camera)
    yield seq
    seq.stop()


@pytest.fixture
def client(camera, scripts_dir, monkeypatch):
    """A Flask test client wired to the fake camera."""
    from nightshoot import app as appmod
    from nightshoot.sequencer import Sequencer

    monkeypatch.setattr(appmod, "camera", camera)
    monkeypatch.setattr(appmod, "sequencer", Sequencer(camera))
    monkeypatch.setattr(appmod, "SCRIPT_DIR", str(scripts_dir))
    monkeypatch.setattr(appmod, "THUMB_DIR", camera.thumb_dir)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


@pytest.fixture
def wait_for():
    """Poll until a condition holds, so tests never sleep longer than needed."""
    import time

    def _wait(predicate, timeout=20.0, interval=0.02):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    return _wait


@pytest.fixture
def examples_dir():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples", "scripts")
