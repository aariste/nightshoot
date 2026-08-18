"""Admin network pane: hotspot switching and its lockout guards."""

import subprocess
import types

import pytest

from nightshoot import network as net


@pytest.fixture
def world(monkeypatch):
    """A fake NetworkManager whose state the test can drive."""
    state = {"ap_up": False, "has_ap": True, "saved": ["HomeNet"],
             "revert": False, "dnsmasq": True, "calls": [], "detached": []}

    def fake_run(args, timeout=15.0):
        state["calls"].append(list(args))
        out = ""
        if args[0] == "nmcli":
            joined = " ".join(args[1:])
            if joined == "-t -f DEVICE,TYPE device":
                out = "eth0:ethernet\nwlan0:wifi\n"
            elif joined == "-t -f NAME connection":
                names = (["nightshoot-ap"] if state["has_ap"] else []) + state["saved"]
                out = "\n".join(names) + "\n"
            elif joined == "-t -f NAME,TYPE connection":
                rows = [f"{n}:802-11-wireless" for n in state["saved"]]
                if state["has_ap"]:
                    rows.append("nightshoot-ap:802-11-wireless")
                out = "\n".join(rows) + "\n"
            elif joined == "-t -f NAME,TYPE,DEVICE connection show --active":
                if state["ap_up"]:
                    out = "nightshoot-ap:802-11-wireless:wlan0\n"
                elif state["saved"]:
                    out = f"{state['saved'][0]}:802-11-wireless:wlan0\n"
            elif joined == "-g ipv4.address connection show nightshoot-ap":
                out = "192.168.7.1/24\n"
            elif joined == "-g 802-11-wireless.ssid connection show nightshoot-ap":
                out = "NightShoot\n"
            elif joined.startswith("-s -g 802-11-wireless-security.psk"):
                out = "starrynight\n"
            elif joined.startswith("-t -f IP4.ADDRESS device show"):
                out = ("IP4.ADDRESS[1]:192.168.7.1/24\n" if state["ap_up"]
                       else "IP4.ADDRESS[1]:192.168.1.42/24\n")
        elif args[0] == "systemctl":
            if args[1] == "is-active":
                out = "active\n" if state["revert"] else "inactive\n"
            elif args[1] == "stop":
                state["revert"] = False
        elif args[0] == "systemd-run":
            state["revert"] = True
        return subprocess.CompletedProcess(args, 0, out, "")

    def fake_popen(args, **kwargs):
        state["detached"].append(args)
        script = args[-1]
        if "connection up nightshoot-ap" in script:
            state["ap_up"] = True
        if "connection down nightshoot-ap" in script:
            state["ap_up"] = False
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(net, "_run", fake_run)
    monkeypatch.setattr(net.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(net.shutil, "which",
                        lambda name: None if (name == "dnsmasq" and not state["dnsmasq"])
                        else f"/usr/bin/{name}")
    monkeypatch.setattr(net.os.path, "exists",
                        lambda path: state["dnsmasq"] if path.endswith("dnsmasq") else False)
    return state


class TestStatus:
    def test_reports_client_mode(self, world):
        status = net.status()
        assert status["mode"] == "wifi"
        assert status["connection"] == "HomeNet"
        assert status["address"] == "192.168.1.42"

    def test_reports_hotspot_mode(self, world):
        world["ap_up"] = True
        assert net.status()["mode"] == "hotspot"

    def test_exposes_the_hotspot_profile(self, world):
        hotspot = net.status()["hotspot"]
        assert hotspot["ssid"] == "NightShoot"
        assert hotspot["password"] == "starrynight"
        assert hotspot["address"] == "192.168.7.1"

    def test_lists_fallback_networks_without_the_ap(self, world):
        assert net.status()["saved_networks"] == ["HomeNet"]

    def test_detects_a_missing_profile(self, world):
        world["has_ap"] = False
        assert net.status()["hotspot_configured"] is False


class TestClientDetection:
    def test_recognises_a_client_on_the_hotspot(self, world):
        assert net.client_is_on_hotspot("192.168.7.55") is True

    def test_recognises_a_client_elsewhere(self, world):
        assert net.client_is_on_hotspot("192.168.1.20") is False

    @pytest.mark.parametrize("addr", [None, "not-an-ip", ""])
    def test_handles_nonsense(self, world, addr):
        assert net.client_is_on_hotspot(addr) is False


class TestEnable:
    def test_switches_detached(self, world):
        result = net.set_hotspot(True, revert_after=600, delay=0)
        assert result["switching_to"] == "hotspot"
        assert result["ssid"] == "NightShoot"
        assert len(world["detached"]) == 1
        assert world["ap_up"] is True

    def test_arms_the_safety_net_before_switching(self, world):
        net.set_hotspot(True, revert_after=600, delay=0)
        order = [c[0] for c in world["calls"] if c[0] == "systemd-run"]
        assert order, "no revert timer armed"
        armed = next(c for c in world["calls"] if c[0] == "systemd-run")
        assert "--on-active=600" in armed
        assert "connection down nightshoot-ap" in " ".join(armed)
        assert "connection up HomeNet" in " ".join(armed)

    def test_can_skip_the_safety_net(self, world):
        result = net.set_hotspot(True, revert_after=None, delay=0)
        assert result["revert_armed"] is False
        assert not any(c[0] == "systemd-run" for c in world["calls"])

    def test_refuses_without_dnsmasq(self, world):
        world["dnsmasq"] = False
        with pytest.raises(net.NetworkError, match="dnsmasq-base"):
            net.set_hotspot(True, delay=0)
        assert not world["detached"]

    def test_refuses_without_a_profile(self, world):
        world["has_ap"] = False
        with pytest.raises(net.NetworkError, match="no 'nightshoot-ap' profile"):
            net.set_hotspot(True, delay=0)


class TestDisable:
    def test_switches_back(self, world):
        world["ap_up"] = True
        result = net.set_hotspot(False, delay=0)
        assert result["switching_to"] == "wifi"
        assert "device connect wlan0" in world["detached"][0][-1]
        assert world["ap_up"] is False

    def test_refuses_to_strand_the_pi(self, world):
        world.update(ap_up=True, saved=[])
        with pytest.raises(net.NetworkError, match="unreachable"):
            net.set_hotspot(False, delay=0)
        assert not world["detached"]
        assert world["ap_up"] is True

    def test_clears_a_pending_revert(self, world):
        world["revert"] = True
        net.set_hotspot(False, delay=0)
        assert world["revert"] is False


class TestRevertCancellation:
    def test_cancels(self, world):
        world["revert"] = True
        assert net.status()["revert_pending"] is True
        net.cancel_revert()
        assert net.status()["revert_pending"] is False


class TestEndpoints:
    def test_get_reports_state(self, client, world):
        body = client.get("/api/network").get_json()
        assert body["ok"] and body["mode"] == "wifi"
        assert body["you_are_on_hotspot"] is False
        assert body["sequence_running"] is False

    def test_enable(self, client, world):
        body = client.post("/api/network/hotspot",
                           json={"enabled": True, "revert_after": 300}).get_json()
        assert body["ok"] and body["address"] == "192.168.7.1"
        assert body["revert_armed"] is True

    def test_disable(self, client, world):
        world["ap_up"] = True
        assert client.post("/api/network/hotspot",
                           json={"enabled": False}).status_code == 200

    def test_requires_enabled(self, client, world):
        assert client.post("/api/network/hotspot", json={}).status_code == 400

    def test_rejects_a_bad_revert_value(self, client, world):
        assert client.post("/api/network/hotspot",
                           json={"enabled": True, "revert_after": "soon"}).status_code == 400

    def test_surfaces_a_blocked_prerequisite(self, client, world):
        world["dnsmasq"] = False
        response = client.post("/api/network/hotspot", json={"enabled": True})
        assert response.status_code == 400
        assert "dnsmasq-base" in response.get_json()["error"]

    def test_cancel_revert(self, client, world):
        world["revert"] = True
        assert client.post("/api/network/cancel-revert").status_code == 200
        assert net.status()["revert_pending"] is False

    def test_capture_survives_a_switch(self, client, world, wait_for):
        from nightshoot import app as appmod
        client.post("/api/start", json={"frames": 3, "interval_s": 0.2, "start_delay_s": 0})
        client.post("/api/network/hotspot", json={"enabled": True, "revert_after": 600})
        assert wait_for(lambda: not appmod.sequencer.running)
        assert appmod.sequencer.status()["frames_done"] == 3
        assert appmod.sequencer.status()["errors"] == 0
