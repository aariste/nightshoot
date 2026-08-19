"""Admin panel: host health, service log, restart and reboot."""

import subprocess

import pytest

from nightshoot import system


class TestReaders:
    """Every reader must degrade to None rather than raise: a missing sysfs
    file on some other board must not break the whole panel."""

    def test_cpu_temperature_handles_millidegrees(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: "48312")
        assert system.cpu_temperature() == pytest.approx(48.3, abs=0.1)

    def test_cpu_temperature_handles_degrees(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: "48.3")
        assert system.cpu_temperature() == pytest.approx(48.3, abs=0.1)

    @pytest.mark.parametrize("raw", [None, "", "not a number"])
    def test_cpu_temperature_survives_nonsense(self, monkeypatch, raw):
        monkeypatch.setattr(system, "_read", lambda path: raw)
        assert system.cpu_temperature() is None

    def test_uptime(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: "12345.67 98765.43")
        assert system.uptime_seconds() == pytest.approx(12345.67)

    def test_uptime_survives_nonsense(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: "garbage")
        assert system.uptime_seconds() is None

    def test_memory(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: (
            "MemTotal:        1000000 kB\n"
            "MemFree:          200000 kB\n"
            "MemAvailable:     400000 kB\n"))
        mem = system.memory()
        assert mem["total_mb"] == 977
        assert mem["available_mb"] == 391
        assert mem["used_percent"] == 60

    def test_memory_survives_a_missing_file(self, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: None)
        assert system.memory() is None

    def test_disk(self, tmp_path):
        info = system.disk(str(tmp_path))
        assert info["free_gb"] > 0 and info["total_gb"] > 0

    def test_disk_survives_a_missing_path(self):
        assert system.disk("/definitely/not/here") is None

    def test_version_is_reported(self):
        assert system.version()


class TestThrottling:
    """Under-voltage is the commonest cause of mysterious Pi faults."""

    def _fake_vcgencmd(self, monkeypatch, output, returncode=0):
        monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/vcgencmd")
        monkeypatch.setattr(system.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(
                                a[0], returncode, output, ""))

    def test_clean_state(self, monkeypatch):
        self._fake_vcgencmd(monkeypatch, "throttled=0x0\n")
        result = system.throttling()
        assert result["ok"] is True and result["flags"] == []

    def test_under_voltage_now(self, monkeypatch):
        self._fake_vcgencmd(monkeypatch, "throttled=0x1\n")
        result = system.throttling()
        assert result["ok"] is False
        assert any("under-voltage now" in f for f in result["flags"])

    def test_historic_under_voltage(self, monkeypatch):
        self._fake_vcgencmd(monkeypatch, "throttled=0x50000\n")
        flags = system.throttling()["flags"]
        assert any("since boot" in f for f in flags)

    def test_absent_tool_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: None)
        assert system.throttling() is None

    def test_unparseable_output(self, monkeypatch):
        self._fake_vcgencmd(monkeypatch, "nonsense\n")
        assert system.throttling() is None


class TestSummary:
    def test_returns_every_field_the_ui_shows(self, tmp_path):
        data = system.summary(str(tmp_path))
        for key in ("hostname", "version", "cpu_temp_c", "uptime_s", "load",
                    "memory", "throttling", "disk", "time", "timezone",
                    "service_since"):
            assert key in data

    def test_is_json_safe(self, tmp_path):
        import json
        json.dumps(system.summary(str(tmp_path)))


class TestSystemEndpoint:
    def test_reports_health(self, client):
        body = client.get("/api/system").get_json()
        assert body["ok"] is True
        assert "hostname" in body and "version" in body

    def test_survives_a_board_without_sysfs(self, client, monkeypatch):
        monkeypatch.setattr(system, "_read", lambda path: None)
        monkeypatch.setattr(system.shutil, "which", lambda name: None)
        body = client.get("/api/system").get_json()
        assert body["ok"] is True
        assert body["cpu_temp_c"] is None


class TestLogsEndpoint:
    def test_returns_text(self, client, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(
                                a[0], 0, "frame 1/10: DSC_0001.NEF\n", ""))
        body = client.get("/api/logs").get_json()
        assert body["ok"] is True and "DSC_0001" in body["text"]

    def test_survives_a_missing_journalctl(self, client, monkeypatch):
        def boom(*args, **kwargs):
            raise FileNotFoundError("journalctl")
        monkeypatch.setattr(subprocess, "run", boom)
        body = client.get("/api/logs").get_json()
        assert body["ok"] is True
        assert "could not read" in body["text"]

    @pytest.mark.parametrize("lines,expected", [("5", "10"), ("9999", "500")])
    def test_line_count_is_clamped(self, client, monkeypatch, lines, expected):
        seen = {}

        def capture(argv, *args, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", capture)
        client.get(f"/api/logs?lines={lines}")
        assert expected in seen["argv"]


class TestRestartAndReboot:
    @pytest.fixture
    def spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(system, "restart_service", lambda: calls.append("restart"))
        monkeypatch.setattr(system, "reboot", lambda: calls.append("reboot"))
        monkeypatch.setattr(system, "shutdown", lambda: calls.append("shutdown"))
        return calls

    def test_restart_is_accepted(self, client, spy):
        assert client.post("/api/restart").status_code == 200

    def test_reboot_is_accepted(self, client, spy):
        assert client.post("/api/reboot").status_code == 200

    def test_shutdown_is_accepted(self, client, spy):
        assert client.post("/api/shutdown").status_code == 200

    def test_restart_is_refused_mid_sequence(self, client, spy, wait_for):
        from nightshoot import app as appmod
        client.post("/api/start", json={"frames": 5, "interval_s": 0.3,
                                        "start_delay_s": 0})
        assert client.post("/api/restart").status_code == 409
        assert "restart" not in spy
        appmod.sequencer.stop()
        wait_for(lambda: not appmod.sequencer.running)

    def test_reboot_is_refused_mid_sequence(self, client, spy, wait_for):
        from nightshoot import app as appmod
        client.post("/api/start", json={"frames": 5, "interval_s": 0.3,
                                        "start_delay_s": 0})
        assert client.post("/api/reboot").status_code == 409
        assert "reboot" not in spy
        appmod.sequencer.stop()
        wait_for(lambda: not appmod.sequencer.running)

    def test_these_are_cross_origin_protected(self, client, spy):
        for path in ("/api/restart", "/api/reboot", "/api/shutdown"):
            response = client.post(path, headers={"Origin": "https://evil.example"})
            assert response.status_code == 403, path
        assert spy == []


class TestMergedPanel:
    """The three panels are now one; the markup should reflect that."""

    def test_admin_panel_is_served(self, client):
        assert b'id="adminpanel"' in client.get("/").data

    def test_the_old_system_panel_is_gone(self, client):
        assert b'id="syspanel"' not in client.get("/").data

    def test_network_controls_are_in_it(self, client):
        page = client.get("/").data
        for element in (b'id="apbtn"', b'id="revertsel"', b'id="netmode"'):
            assert element in page

    def test_health_fields_are_in_it(self, client):
        page = client.get("/").data
        for element in (b'id="systemp"', b'id="systhrottle"', b'id="sysdisk"',
                        b'id="sysuptime"', b'id="sysver"'):
            assert element in page

    def test_power_controls_are_in_it(self, client):
        page = client.get("/").data
        assert b"RESTART SERVICE" in page
        assert b"REBOOT" in page
        assert b"SHUT DOWN" in page
