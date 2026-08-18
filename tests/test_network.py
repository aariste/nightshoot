"""Admin network pane: hotspot switching and its lockout guards."""

import pytest

from nightshoot import network as net


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
        assert hotspot["address"] == "192.168.7.1"

    def test_status_does_not_leak_the_psk(self, world):
        """A stored credential should not ride along in every status poll."""
        hotspot = net.status()["hotspot"]
        assert "password" not in hotspot
        assert "starrynight" not in str(net.status())

    def test_the_psk_is_available_on_request(self, world):
        hotspot = net.ap_profile(include_secret=True)
        assert hotspot["password"] == "starrynight"

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
