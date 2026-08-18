"""YAML script parsing, validation, upload and interpretation."""

import os

import pytest
import yaml

from nightshoot import scripts as S

GOOD = "name: Test\ndescription: a test\nsteps:\n  - set: {iso: '800'}\n  - capture:\n"


class TestParsing:
    def test_minimal_script(self):
        script = S.parse_script("steps:\n - capture:\n", "t.yaml")
        assert script.estimated_frames == 1

    def test_keeps_metadata(self):
        script = S.parse_script(GOOD, "t.yaml")
        assert script.name == "Test"
        assert script.description == "a test"

    def test_name_defaults_to_the_filename(self):
        assert S.parse_script("steps:\n - capture:\n", "my-run.yaml").name == "my-run"

    @pytest.mark.parametrize("text,fragment", [
        ("- capture:", "top level"),
        ("name: x", "steps"),
        ("steps: []", "non-empty"),
        ("steps:\n - shoot:", "exactly one command"),
        ("steps:\n - capture:\n   wait: 3", "exactly one command"),
        ("steps:\n - repeat: 3", "steps"),
        ("steps:\n - repeat: -2\n   steps:\n    - capture:", "whole number"),
        ("steps:\n - wait_until: '25:00'", "24-hour"),
        ("steps:\n - wait_until: 'dawn'", "24-hour"),
        ("steps:\n - wait: -5", "number of seconds"),
        ("steps:\n - for_each: {setting: iso}\n   steps:\n    - capture:", "values"),
        ("steps:\n - for_each: {values: []}\n   steps:\n    - capture:", "non-empty"),
        ("steps:\n - capture: {flash: true}", "unknown capture option"),
        ("steps:\n - capture: {bulb: -3}", "positive"),
        ("steps:\n - message: hi\n   every: 3", "unexpected key"),
        ("steps:\n - repeat: 2\n   every: 0\n   steps:\n    - capture:", "positive"),
        ("steps:\n - set: iso", "mapping"),
        ("steps:\n - capture:\n  bad indent: [", "invalid YAML"),
    ])
    def test_rejects_malformed(self, text, fragment):
        with pytest.raises(S.ScriptError, match=fragment):
            S.parse_script(text, "t.yaml")

    def test_guards_against_deep_nesting(self):
        inner = [{"capture": None}]
        for _ in range(9):
            inner = [{"repeat": 1, "steps": inner}]
        with pytest.raises(S.ScriptError, match="deeply"):
            S.parse_script(yaml.safe_dump({"steps": inner}), "t.yaml")

    def test_allows_reasonable_nesting(self):
        inner = [{"capture": None}]
        for _ in range(3):
            inner = [{"repeat": 1, "steps": inner}]
        S.parse_script(yaml.safe_dump({"steps": inner}), "t.yaml")


class TestFrameEstimation:
    def estimate(self, text):
        return S.parse_script(text, "t.yaml").estimated_frames

    def test_counts_flat_captures(self):
        assert self.estimate("steps:\n - capture:\n - capture:\n") == 2

    def test_counts_bounded_loops(self):
        assert self.estimate("steps:\n - repeat: 5\n   steps:\n    - capture:\n") == 5

    def test_multiplies_nested_loops(self):
        text = ("steps:\n - repeat: 3\n   steps:\n    - for_each:\n"
                "       setting: iso\n       values: ['1','2','3','4']\n"
                "      steps:\n       - capture:\n")
        assert self.estimate(text) == 12

    def test_open_ended_loops_are_unknown(self):
        assert self.estimate("steps:\n - repeat: forever\n   steps:\n    - capture:\n") is None
        assert self.estimate("steps:\n - repeat: 9\n   until: '05:00'\n   steps:\n    - capture:\n") is None

    def test_no_captures(self):
        assert self.estimate("steps:\n - message: hi\n - wait: 1\n") == 0


class TestVariables:
    def test_interpolates(self):
        assert S.substitute("ISO {{iso}} now", {"iso": "800"}) == "ISO 800 now"

    def test_whole_string_keeps_its_type(self):
        assert S.substitute("{{n}}", {"n": 5}) == 5

    def test_recurses(self):
        assert S.substitute({"a": ["{{iso}}"]}, {"iso": "800"}) == {"a": ["800"]}

    def test_unknown_variable_raises(self):
        with pytest.raises(S.ScriptError, match="nope"):
            S.substitute("{{nope}}", {})


class TestCollectSettings:
    def test_finds_set_values(self):
        script = S.parse_script(
            "vars: {s: '20'}\nsteps:\n - set: {shutterspeed: '{{s}}', iso: '800'}\n", "t.yaml")
        found = dict(S.collect_settings(script.steps, script.vars))
        assert found["shutterspeed"] == "20"
        assert found["iso"] == "800"

    def test_finds_for_each_values(self):
        script = S.parse_script(
            "steps:\n - for_each: {setting: iso, values: ['400','1600']}\n"
            "   steps:\n    - capture:\n", "t.yaml")
        found = S.collect_settings(script.steps, script.vars)
        assert ("iso", "400") in found and ("iso", "1600") in found

    def test_skips_values_only_known_at_run_time(self):
        script = S.parse_script(
            "steps:\n - for_each: {values: ['a','b'], as: v}\n   steps:\n"
            "    - set: {iso: '{{v}}'}\n    - capture:\n", "t.yaml")
        assert not any(k == "iso" for k, _ in S.collect_settings(script.steps, script.vars))


class TestDiscovery:
    def test_lists_only_yaml(self, scripts_dir):
        (scripts_dir / "a.yaml").write_text(GOOD)
        (scripts_dir / "notes.txt").write_text("ignore me")
        assert [e["filename"] for e in S.list_scripts(str(scripts_dir))] == ["a.yaml"]

    def test_surfaces_parse_errors_instead_of_hiding_them(self, scripts_dir):
        (scripts_dir / "bad.yaml").write_text("steps:\n - nonsense:\n")
        entry = S.list_scripts(str(scripts_dir))[0]
        assert entry["ok"] is False and "exactly one command" in entry["error"]

    @pytest.mark.parametrize("name", [
        "../../etc/passwd", "..\\secrets.yaml", "/etc/passwd", ".hidden.yaml", "x.py",
    ])
    def test_blocks_path_traversal(self, scripts_dir, name):
        with pytest.raises(S.ScriptError):
            S.load_script(str(scripts_dir), name)


class TestSaveScript:
    def test_writes_a_valid_script(self, scripts_dir):
        script = S.save_script(str(scripts_dir), "good.yaml", GOOD)
        assert (scripts_dir / "good.yaml").exists()
        assert script.name == "Test"

    @pytest.mark.parametrize("text,fragment", [
        ("steps:\n - shoot:\n", "exactly one command"),
        ("steps:\n - capture:\n  bad: [\n", "invalid YAML"),
        ("just text\n", "mapping"),
        ("   \n", "empty"),
    ])
    def test_never_writes_an_invalid_script(self, scripts_dir, text, fragment):
        with pytest.raises(S.ScriptError, match=fragment):
            S.save_script(str(scripts_dir), "bad.yaml", text)
        assert not (scripts_dir / "bad.yaml").exists()

    def test_adds_a_yaml_extension(self, scripts_dir):
        S.save_script(str(scripts_dir), "noext", GOOD)
        assert (scripts_dir / "noext.yaml").exists()

    def test_refuses_to_clobber_silently(self, scripts_dir):
        S.save_script(str(scripts_dir), "a.yaml", GOOD)
        with pytest.raises(FileExistsError):
            S.save_script(str(scripts_dir), "a.yaml", GOOD)

    def test_overwrites_when_told_to(self, scripts_dir):
        S.save_script(str(scripts_dir), "a.yaml", GOOD)
        S.save_script(str(scripts_dir), "a.yaml", GOOD.replace("Test", "Replaced"),
                      overwrite=True)
        assert "Replaced" in (scripts_dir / "a.yaml").read_text()

    def test_normalises_crlf(self, scripts_dir):
        S.save_script(str(scripts_dir), "crlf.yaml", GOOD.replace("\n", "\r\n"))
        assert b"\r" not in (scripts_dir / "crlf.yaml").read_bytes()

    def test_strips_a_byte_order_mark(self, scripts_dir):
        S.save_script(str(scripts_dir), "bom.yaml", "\ufeff" + GOOD)
        assert S.load_script(str(scripts_dir), "bom.yaml").name == "Test"

    def test_rejects_binary(self, scripts_dir):
        with pytest.raises(S.ScriptError, match="UTF-8"):
            S.save_script(str(scripts_dir), "bin.yaml", b"\xff\xfe\x00\x01binary")

    def test_accepts_utf8_bytes(self, scripts_dir):
        S.save_script(str(scripts_dir), "bytes.yaml", GOOD.encode("utf-8"))
        assert (scripts_dir / "bytes.yaml").read_text().endswith("\n")

    @pytest.mark.parametrize("name", ["../escape.yaml", "/etc/cron.yaml", ".hidden.yaml"])
    def test_cannot_escape_the_folder(self, scripts_dir, name):
        try:
            S.save_script(str(scripts_dir), name, GOOD)
        except S.ScriptError:
            return
        assert not (scripts_dir.parent / "escape.yaml").exists()


class TestShippedExamples:
    def test_all_parse(self, examples_dir):
        names = [n for n in os.listdir(examples_dir) if n.endswith(".yaml")]
        assert names, "no example scripts found"
        for name in names:
            with open(os.path.join(examples_dir, name), encoding="utf-8") as handle:
                S.parse_script(handle.read(), name)
