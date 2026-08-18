"""Hardening: the issues raised in review, each with the test that proves it.

Every test here corresponds to a specific finding. They exist so the fix cannot
quietly regress.
"""

import threading

import pytest

from nightshoot import network as net
from nightshoot.sequencer import Plan


class TestConcurrentStarts:
    """Finding 2: two simultaneous starts could both spawn a capture worker.

    ``start()`` checked ``running``, armed state, then assigned ``_thread``
    later — so two requests could both pass the check and end up sharing one
    USB connection.
    """

    def test_only_one_of_two_simultaneous_starts_wins(self, sequencer, wait_for):
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt():
            barrier.wait()          # maximise the overlap
            try:
                sequencer.start(Plan(frames=3, interval_s=0.2, start_delay_s=0))
                outcomes.append("started")
            except RuntimeError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert outcomes.count("started") == 1, f"expected one winner, got {outcomes}"
        assert outcomes.count("rejected") == 1
        assert wait_for(lambda: not sequencer.running)

    def test_many_simultaneous_starts_still_yield_one_worker(self, sequencer, wait_for):
        barrier = threading.Barrier(8)
        outcomes = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            try:
                sequencer.start(Plan(frames=2, interval_s=0.2, start_delay_s=0))
                with lock:
                    outcomes.append("started")
            except RuntimeError:
                with lock:
                    outcomes.append("rejected")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert outcomes.count("started") == 1, f"expected one winner, got {outcomes}"
        assert wait_for(lambda: not sequencer.running)

    def test_a_rejected_start_does_not_disturb_the_running_one(self, sequencer,
                                                               wait_for):
        sequencer.start(Plan(frames=4, interval_s=0.2, start_delay_s=0))
        with pytest.raises(RuntimeError):
            sequencer.start(Plan(frames=99, interval_s=5, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)
        assert sequencer.status()["frames_done"] == 4

    def test_a_failed_start_releases_the_claim(self, sequencer, wait_for):
        """An invalid plan must not leave the camera reserved forever."""
        with pytest.raises(ValueError):
            sequencer.start(Plan(frames=1, interval_s=-5))
        assert sequencer.running is False
        sequencer.start(Plan(frames=1, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)

    def test_a_failed_preflight_releases_the_claim(self, sequencer, wait_for):
        from nightshoot import scripts as S
        bad = S.parse_script(
            "name: bad\nsteps:\n - set: {shutterspeed: '22'}\n - capture:\n", "b.yaml")
        with pytest.raises(ValueError):
            sequencer.start_script(bad)
        assert sequencer.running is False
        sequencer.start(Plan(frames=1, interval_s=0.2, start_delay_s=0))
        assert wait_for(lambda: not sequencer.running)


class TestMalformedInput:
    """Finding 3: bad JSON types reached float()/int() and produced a 500."""

    @pytest.mark.parametrize("payload", [
        {"exposure_s": "abc", "bulb": True},
        {"exposure_s": [], "bulb": True},
        {"exposure_s": {"a": 1}, "bulb": True},
        {"exposure_s": -5, "bulb": True},
        {"bulb": "maybe"},
    ])
    def test_test_shot_rejects_rather_than_crashes(self, client, payload):
        response = client.post("/api/test-shot", json=payload)
        assert response.status_code == 400, f"{payload} gave {response.status_code}"
        assert response.get_json()["ok"] is False

    def test_null_means_use_the_default(self, client):
        """JSON null is 'not specified', which the UI relies on for 'until'."""
        response = client.post("/api/test-shot", json={"exposure_s": None, "bulb": False})
        assert response.status_code == 200

    @pytest.mark.parametrize("payload", [
        {"frames": "lots"},
        {"frames": []},
        {"frames": 1.5},
        {"interval_s": "soon"},
        {"interval_s": None, "frames": "x"},
        {"start_delay_s": {}},
        {"bulb": "perhaps"},
        {"download": 7},
        {"until": "25:00"},
        {"until": "not a time"},
        {"until": 12345},
        {"frames": -1},
    ])
    def test_start_rejects_rather_than_crashes(self, client, payload):
        response = client.post("/api/start", json=payload)
        assert response.status_code == 400, f"{payload} gave {response.status_code}"
        assert response.get_json()["ok"] is False

    def test_a_non_object_body_is_rejected(self, client):
        response = client.post("/api/start", json=[1, 2, 3])
        assert response.status_code == 400

    def test_string_false_is_not_true(self, client, camera_state, wait_for):
        """bool('false') is True in Python, which is never what a user means."""
        from nightshoot import app as appmod
        response = client.post("/api/start", json={
            "frames": 1, "interval_s": 0.2, "start_delay_s": 0, "bulb": "false"})
        assert response.status_code == 200
        assert appmod.sequencer.plan.bulb is False
        assert wait_for(lambda: not appmod.sequencer.running)

    def test_valid_values_still_work(self, client, wait_for):
        from nightshoot import app as appmod
        response = client.post("/api/start", json={
            "frames": 2, "interval_s": 0.2, "start_delay_s": 0,
            "bulb": False, "download": False, "until": None})
        assert response.status_code == 200
        assert wait_for(lambda: not appmod.sequencer.running)


class TestRevertSafetyNet:
    """Finding 4: a requested rollback that failed to arm was ignored."""

    @pytest.mark.parametrize("value", [-5, 0.5, "soon", [], 10, 999_999])
    def test_bad_revert_values_are_rejected(self, client, world, value):
        response = client.post("/api/network/hotspot",
                               json={"enabled": True, "revert_after": value})
        assert response.status_code == 400, f"{value!r} gave {response.status_code}"

    def test_zero_means_no_safety_net_and_is_allowed(self, client, world):
        response = client.post("/api/network/hotspot",
                               json={"enabled": True, "revert_after": 0})
        assert response.status_code == 200
        assert response.get_json()["revert_armed"] is False

    def test_the_switch_is_refused_when_the_timer_cannot_be_armed(self, world,
                                                                  monkeypatch):
        """If the user asked for a safety net, do not proceed without one."""
        monkeypatch.setattr(net, "_arm_revert", lambda seconds, back_to: False)
        with pytest.raises(net.NetworkError, match="could not arm"):
            net.set_hotspot(True, revert_after=600, delay=0)
        assert not world["detached"], "the hotspot was switched anyway"

    def test_the_api_reports_that_refusal(self, client, world, monkeypatch):
        monkeypatch.setattr(net, "_arm_revert", lambda seconds, back_to: False)
        response = client.post("/api/network/hotspot",
                               json={"enabled": True, "revert_after": 600})
        assert response.status_code == 400
        assert "could not arm" in response.get_json()["error"]

    def test_no_revert_requested_still_switches(self, world, monkeypatch):
        monkeypatch.setattr(net, "_arm_revert", lambda seconds, back_to: False)
        result = net.set_hotspot(True, revert_after=None, delay=0)
        assert result["switching_to"] == "hotspot"
        assert world["detached"]


class TestShellQuoting:
    """Finding 5: profile names were interpolated into a root /bin/sh script."""

    def test_profile_names_are_quoted(self, world, monkeypatch):
        monkeypatch.setattr(net, "AP_CON", "evil; touch /tmp/pwned")
        world["has_ap"] = True
        # ap_profile checks membership in the connection list, so make it match.
        world["saved"] = ["evil; touch /tmp/pwned", "HomeNet"]
        try:
            net.set_hotspot(False, delay=0)
        except net.NetworkError:
            pass
        for argv in world["detached"]:
            script = argv[-1]
            assert "touch /tmp/pwned" not in script.replace("'evil; touch /tmp/pwned'", "")

    def test_the_revert_timer_quotes_its_arguments(self, world):
        world["saved"] = ["Home; rm -rf /"]
        net._arm_revert(600, "Home; rm -rf /")
        armed = [c for c in world["calls"] if c[0] == "systemd-run"]
        assert armed, "no timer armed"
        script = armed[0][-1]
        assert "'Home; rm -rf /'" in script, f"name was not quoted: {script}"

    def test_normal_names_still_work(self, world):
        net._arm_revert(600, "HomeNet")
        armed = [c for c in world["calls"] if c[0] == "systemd-run"]
        assert "HomeNet" in armed[0][-1]


class TestCrossOriginProtection:
    """Finding 1: a page you open elsewhere could POST to the Pi.

    /api/shutdown reads no body, so a browser sends it without a CORS preflight.
    """

    def test_a_cross_origin_post_is_refused(self, client):
        response = client.post("/api/stop", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_shutdown_is_protected(self, client):
        response = client.post("/api/shutdown",
                               headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_network_switching_is_protected(self, client, world):
        response = client.post("/api/network/hotspot",
                               json={"enabled": True},
                               headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_same_origin_is_allowed(self, client):
        response = client.post("/api/stop",
                               headers={"Origin": "http://localhost"})
        assert response.status_code == 200

    def test_no_origin_header_is_allowed(self, client):
        """curl and the app itself send no Origin; only browsers do."""
        assert client.post("/api/stop").status_code == 200

    def test_reads_are_not_blocked(self, client):
        response = client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200


class TestOptionalToken:
    """Finding 1: a shared secret for when the Pi is not on a private network."""

    @pytest.fixture
    def token_client(self, client, monkeypatch):
        from nightshoot import app as appmod
        monkeypatch.setattr(appmod, "AUTH_TOKEN", "s3cret")
        return client

    def test_requests_without_the_token_are_refused(self, token_client):
        assert token_client.get("/api/status").status_code == 401

    def test_a_wrong_token_is_refused(self, token_client):
        response = token_client.get("/api/status",
                                    headers={"X-NightShoot-Token": "nope"})
        assert response.status_code == 401

    def test_the_right_token_works(self, token_client):
        response = token_client.get("/api/status",
                                    headers={"X-NightShoot-Token": "s3cret"})
        assert response.status_code == 200

    def test_a_query_parameter_also_works(self, token_client):
        """So an image tag such as /preview.jpg can carry it."""
        assert token_client.get("/api/status?token=s3cret").status_code == 200

    def test_it_is_off_by_default(self, client):
        assert client.get("/api/status").status_code == 200


class TestSecretsInResponses:
    """Finding 1: the hotspot PSK was returned by the polled status endpoint."""

    def test_status_does_not_contain_the_psk(self, client, world):
        body = client.get("/api/network").get_data(as_text=True)
        assert "starrynight" not in body

    def test_the_password_endpoint_returns_it(self, client, world):
        body = client.get("/api/network/hotspot-password").get_json()
        assert body["ok"] is True
        assert body["password"] == "starrynight"

    def test_camera_status_carries_no_secret(self, client, world):
        body = client.get("/api/status").get_data(as_text=True)
        assert "starrynight" not in body
