"""HTTP API: status, settings, sequences, script upload, live view."""

import io

GOOD = "name: Uploaded\ndescription: from a phone\nsteps:\n  - set: {iso: '800'}\n  - capture:\n"


def upload(client, name, text, overwrite=False):
    payload = text.encode() if isinstance(text, str) else text
    data = {"file": (io.BytesIO(payload), name)}
    if overwrite:
        data["overwrite"] = "1"
    return client.post("/api/scripts/upload", data=data,
                       content_type="multipart/form-data")


class TestStatus:
    def test_reports_camera_and_sequence(self, client):
        body = client.get("/api/status").get_json()
        assert body["camera"]["connected"] is True
        assert body["sequence"]["state"] == "idle"

    def test_includes_card_space(self, client):
        card = client.get("/api/status").get_json()["card"]
        assert card["free_images"] == 1180

    def test_includes_pi_disk(self, client):
        assert "disk_free_gb" in client.get("/api/status").get_json()

    def test_index_renders(self, client):
        assert b"NIGHTSHOOT" in client.get("/").data


class TestSettings:
    def test_applies_values(self, client):
        body = client.post("/api/settings", json={"iso": "1600"}).get_json()
        assert body["applied"] == ["iso"]

    def test_accepts_portable_shutter_values(self, client, camera):
        client.post("/api/settings", json={"shutterspeed": "20"})
        assert camera.get_setting("shutterspeed") == "20.0000s"

    def test_reports_failures_per_key(self, client):
        body = client.post("/api/settings", json={"iso": "nope"}).get_json()
        assert "iso" in body["failed"]

    def test_lists_choices(self, client):
        assert "Bulb" in client.get("/api/choices/shutterspeed").get_json()["choices"]


class TestTestShot:
    def test_returns_enough_for_the_ui(self, client):
        body = client.post("/api/test-shot", json={}).get_json()
        assert body["ok"] and body["name"].endswith(".NEF")
        assert body["has_thumb"] is True
        assert isinstance(body["at"], (int, float))

    def test_thumbnail_is_served(self, client):
        client.post("/api/test-shot", json={})
        assert client.get("/thumb.jpg").status_code == 200


class TestSequences:
    def test_starts_and_completes(self, client, wait_for):
        from nightshoot import app as appmod
        assert client.post("/api/start", json={
            "frames": 3, "interval_s": 0.2, "start_delay_s": 0}).status_code == 200
        assert wait_for(lambda: not appmod.sequencer.running)
        assert appmod.sequencer.status()["frames_done"] == 3

    def test_accepts_a_zero_interval(self, client, wait_for):
        from nightshoot import app as appmod
        assert client.post("/api/start", json={
            "frames": 3, "interval_s": 0, "start_delay_s": 0}).status_code == 200
        assert wait_for(lambda: not appmod.sequencer.running)

    def test_rejects_an_impossible_plan(self, client):
        assert client.post("/api/start", json={
            "frames": 3, "interval_s": 1, "bulb": True,
            "exposure_s": 5}).status_code == 400

    def test_locks_settings_while_running(self, client, wait_for):
        from nightshoot import app as appmod
        client.post("/api/start", json={"frames": 3, "interval_s": 0.3, "start_delay_s": 0})
        assert client.post("/api/settings", json={"iso": "800"}).status_code == 409
        appmod.sequencer.stop()
        wait_for(lambda: not appmod.sequencer.running)

    def test_pause_and_stop_respond(self, client):
        assert client.post("/api/pause").status_code == 200
        assert client.post("/api/stop").status_code == 200


class TestScriptEndpoints:
    def test_lists(self, client, scripts_dir):
        (scripts_dir / "a.yaml").write_text(GOOD)
        body = client.get("/api/scripts").get_json()
        assert body["ok"] and body["scripts"][0]["filename"] == "a.yaml"

    def test_returns_source(self, client, scripts_dir):
        (scripts_dir / "a.yaml").write_text(GOOD)
        assert "capture" in client.get("/api/scripts/a.yaml").get_json()["source"]

    def test_missing_is_404(self, client):
        assert client.get("/api/scripts/nope.yaml").status_code == 404

    def test_traversal_is_blocked(self, client):
        assert client.get("/api/scripts/..%2f..%2fetc%2fpasswd").status_code in (400, 404)

    def test_responses_are_not_cacheable(self, client, scripts_dir):
        """A cached reply would show the previously selected script."""
        (scripts_dir / "a.yaml").write_text(GOOD)
        response = client.get("/api/scripts/a.yaml")
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_every_api_response_is_uncacheable(self, client):
        for path in ("/api/status", "/api/scripts", "/api/system"):
            response = client.get(path)
            assert "no-store" in response.headers.get("Cache-Control", ""), path

    def test_the_page_itself_is_still_cacheable(self, client):
        """Only the API is no-store; the HTML may be cached normally."""
        assert "no-store" not in client.get("/").headers.get("Cache-Control", "")

    def test_runs_a_script(self, client, scripts_dir, wait_for):
        from nightshoot import app as appmod
        (scripts_dir / "run.yaml").write_text("name: run\nsteps:\n - capture:\n - capture:\n")
        assert client.post("/api/scripts/run",
                           json={"filename": "run.yaml"}).status_code == 200
        assert wait_for(lambda: not appmod.sequencer.running)
        assert appmod.sequencer.status()["frames_done"] == 2

    def test_refuses_a_script_this_camera_cannot_run(self, client, scripts_dir):
        (scripts_dir / "bad.yaml").write_text(
            "name: bad\nsteps:\n - set: {shutterspeed: '22'}\n - capture:\n")
        response = client.post("/api/scripts/run", json={"filename": "bad.yaml"})
        assert response.status_code == 400
        assert "Closest is" in response.get_json()["error"]


class TestScriptUpload:
    def test_accepts_a_valid_file(self, client, scripts_dir):
        body = upload(client, "phone.yaml", GOOD).get_json()
        assert body["ok"]
        assert body["saved"][0]["name"] == "Uploaded"
        assert (scripts_dir / "phone.yaml").exists()

    def test_rejects_an_invalid_file_without_writing_it(self, client, scripts_dir):
        response = upload(client, "broken.yaml", "steps:\n - nonsense:\n")
        assert response.status_code == 400
        assert "exactly one command" in response.get_json()["failed"]["broken.yaml"]
        assert not (scripts_dir / "broken.yaml").exists()

    def test_conflicts_need_an_explicit_overwrite(self, client):
        upload(client, "a.yaml", GOOD)
        assert upload(client, "a.yaml", GOOD).status_code == 409
        assert upload(client, "a.yaml", GOOD, overwrite=True).status_code == 200

    def test_handles_a_mixed_batch(self, client, scripts_dir):
        data = {"file": [(io.BytesIO(GOOD.encode()), "ok.yaml"),
                         (io.BytesIO(b"steps:\n - nope:\n"), "bad.yaml")]}
        body = client.post("/api/scripts/upload", data=data,
                           content_type="multipart/form-data").get_json()
        assert len(body["saved"]) == 1 and "bad.yaml" in body["failed"]
        assert (scripts_dir / "ok.yaml").exists()
        assert not (scripts_dir / "bad.yaml").exists()

    def test_accepts_pasted_text(self, client):
        body = client.post("/api/scripts/upload",
                           json={"filename": "pasted", "source": GOOD}).get_json()
        assert body["saved"][0]["filename"] == "pasted.yaml"

    def test_requires_something_to_save(self, client):
        assert client.post("/api/scripts/upload", json={}).status_code == 400

    def test_refuses_an_oversized_upload(self, client):
        huge = "name: big\nsteps:\n" + "  - message: 'x'\n" * 60000
        assert client.post("/api/scripts/upload",
                           json={"filename": "huge.yaml", "source": huge}).status_code == 413

    def test_uploaded_scripts_run_immediately(self, client, wait_for):
        from nightshoot import app as appmod
        client.post("/api/scripts/upload", json={
            "filename": "now.yaml", "source": "name: now\nsteps:\n - capture:\n"})
        assert client.post("/api/scripts/run",
                           json={"filename": "now.yaml"}).status_code == 200
        assert wait_for(lambda: not appmod.sequencer.running)


class TestLiveView:
    def test_serves_a_frame(self, client):
        response = client.get("/preview.jpg")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"

    def test_toggles(self, client, camera):
        assert client.post("/api/liveview", json={"enabled": True}).status_code == 200
        assert camera.get_setting("viewfinder") == 1

    def test_is_refused_while_shooting(self, client, wait_for):
        from nightshoot import app as appmod
        client.post("/api/start", json={"frames": 3, "interval_s": 0.3, "start_delay_s": 0})
        assert client.get("/preview.jpg").status_code == 409
        assert client.post("/api/liveview", json={"enabled": True}).status_code == 409
        appmod.sequencer.stop()
        wait_for(lambda: not appmod.sequencer.running)

    def test_starting_a_run_turns_it_off(self, client, camera, wait_for):
        from nightshoot import app as appmod
        client.post("/api/liveview", json={"enabled": True})
        client.post("/api/start", json={"frames": 1, "interval_s": 0.2, "start_delay_s": 0})
        assert camera.get_setting("viewfinder") == 0
        wait_for(lambda: not appmod.sequencer.running)

    def test_status_stays_responsive_mid_exposure(self, client, camera_state, wait_for):
        """Regression: a long exposure must not block the status endpoint."""
        import threading
        import time

        from nightshoot import app as appmod
        camera_state.cost["capture"] = 1.5
        worker = threading.Thread(
            target=lambda: appmod.camera.capture(), daemon=True)
        worker.start()
        time.sleep(0.3)
        started = time.time()
        response = client.get("/api/status")
        assert time.time() - started < 1.2
        assert response.get_json()["camera"]["connected"] is True
        worker.join(timeout=10)
