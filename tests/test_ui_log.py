"""The log panes, exercised as JavaScript.

The UI has no build step and no test harness, so the function is lifted out of
the shipped template and run under Node against a fake element. That keeps the
test honest — it exercises the code that actually ships, not a transcription of
it — without adding a framework for one function.

Skipped when Node is unavailable. Set NIGHTSHOOT_REQUIRE_NODE=1 (as CI does) to
turn that skip into a failure, so a runner without Node cannot quietly drop
these from the suite.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "nightshoot", "templates", "index.html")

HAVE_NODE = shutil.which("node") is not None
if not HAVE_NODE and os.environ.get("NIGHTSHOOT_REQUIRE_NODE") == "1":
    raise RuntimeError(
        "NIGHTSHOOT_REQUIRE_NODE is set but node is not installed, so the UI "
        "tests would be silently skipped")

pytestmark = pytest.mark.skipif(not HAVE_NODE, reason="node is not installed")


def extract(name: str) -> str:
    """Pull one function out of the template by brace matching."""
    with open(TEMPLATE, encoding="utf-8") as handle:
        source = handle.read()
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} is no longer in the template"
    depth, index = 0, source.index("{", start)
    for position in range(index, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start:position + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def run_js(element: dict, text: str) -> dict:
    """Apply updateLog to a fake element and report what it did."""
    script = f"""
{extract("updateLog")}

const el = {json.dumps(element)};
// scrollHeight grows with the content, exactly as a real element's does.
Object.defineProperty(el, 'scrollHeight', {{
  get() {{ return this.textContent.split('\\n').length * this._lineHeight; }}
}});
// A browser clamps scrollTop into range, which is what makes the usual
// "scrollTop = scrollHeight" idiom land at the bottom rather than past it.
// Without modelling that, this fake would flatter code that a browser rejects.
Object.defineProperty(el, 'scrollTop', {{
  get() {{ return this._scrollTop; }},
  set(value) {{
    this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
  }}
}});
el._scrollTop = {json.dumps(element["scrollTop"])};
updateLog(el, {json.dumps(text)});
console.log(JSON.stringify({{scrollTop: el.scrollTop, text: el.textContent}}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def element(lines: int, scroll_top: int, visible: int = 10, line_height: int = 10):
    """A pane showing `visible` lines of a `lines`-line log, scrolled to `scroll_top`."""
    return {
        "textContent": "\n".join(f"line {i}" for i in range(lines)),
        "scrollTop": scroll_top,
        "clientHeight": visible * line_height,
        "_lineHeight": line_height,
    }


def longer(lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


class TestFollowsTheTail:
    def test_scrolls_down_when_already_at_the_bottom(self):
        """While a sequence runs, the newest line is the one you want."""
        el = element(lines=50, scroll_top=400)      # 50*10 - 100 = 400 = bottom
        out = run_js(el, longer(60))
        assert out["scrollTop"] == 600 - 100

    def test_follows_from_an_empty_pane(self):
        el = element(lines=1, scroll_top=0)
        out = run_js(el, longer(40))
        assert out["scrollTop"] == 400 - 100

    def test_tolerates_a_few_pixels_of_slack(self):
        """Sub-pixel line heights mean 'at the bottom' is rarely exact."""
        el = element(lines=50, scroll_top=397)
        out = run_js(el, longer(60))
        assert out["scrollTop"] == 500, "3px from the bottom should still follow"


class TestLeavesAReaderAlone:
    def test_does_not_scroll_when_scrolled_up(self):
        """The bug: scrolling up to read the start, then being dragged back."""
        el = element(lines=50, scroll_top=0)
        out = run_js(el, longer(60))
        assert out["scrollTop"] == 0

    def test_does_not_scroll_from_the_middle(self):
        el = element(lines=50, scroll_top=120)
        out = run_js(el, longer(60))
        assert out["scrollTop"] == 120

    def test_unchanged_text_is_not_rewritten(self):
        """Rewriting textContent resets scrollTop to 0 even when nothing changed,
        so an idle poll would otherwise throw a reader back to the top."""
        el = element(lines=50, scroll_top=120)
        out = run_js(el, longer(50))
        assert out["scrollTop"] == 120

    def test_more_than_the_slack_is_not_at_the_bottom(self):
        el = element(lines=50, scroll_top=390)      # 10px short
        out = run_js(el, longer(60))
        assert out["scrollTop"] == 390


class TestItIsWiredUp:
    def test_the_poll_uses_the_helper(self):
        with open(TEMPLATE, encoding="utf-8") as handle:
            source = handle.read()
        assert "updateLog($('log')" in source
        # The unconditional scroll-to-bottom that caused the bug must be gone.
        assert not re.search(r"\$\('log'\)\.scrollTop\s*=", source)
