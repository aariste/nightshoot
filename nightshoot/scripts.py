"""Declarative YAML shooting scripts, in the spirit of digiCamControl's engine.

A script is a mapping with a ``steps:`` list. Steps are executed in order and may
nest. Nothing is ever ``eval``-ed: the interpreter only understands the fixed
command set below, so a script cannot run arbitrary code on the Pi.

    name: HDR bracket until dawn
    description: 3-frame bracket every 5 minutes
    vars:
      base_iso: "800"
    steps:
      - set: {iso: "{{base_iso}}", imageformat: "NEF (Raw)"}
      - message: "warming up"
      - wait: 10
      - repeat: forever
        every: 300
        until: "05:30"
        steps:
          - for_each:
              setting: shutterspeed
              values: ["1/60", "1/15", "4"]
            steps:
              - capture:
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from dataclasses import dataclass

import yaml

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# command -> extra keys that may sit alongside it on the same step
COMMANDS: dict[str, set[str]] = {
    "message": set(),
    "set": set(),
    "capture": set(),
    "burst": set(),
    "wait": set(),
    "wait_until": set(),
    "repeat": {"steps", "every", "until", "for"},
    "for_each": {"steps"},
}

MAX_DEPTH = 6


class ScriptError(Exception):
    """A script is malformed. The message is shown to the user verbatim."""


class ScriptStopped(Exception):
    """Raised internally when the runner is asked to stop mid-script."""


@dataclass
class Script:
    filename: str
    name: str
    description: str
    vars: dict
    steps: list
    path: str = ""
    source: str = ""
    estimated_frames: int | None = None

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "name": self.name,
            "description": self.description,
            "estimated_frames": self.estimated_frames,
            "ok": True,
        }


# --------------------------------------------------------------------- parsing

def parse_script(text: str, filename: str = "<inline>") -> Script:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScriptError(f"invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ScriptError("script must be a mapping with a 'steps:' list at the top level")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ScriptError("'steps:' must be a non-empty list")

    variables = data.get("vars") or {}
    if not isinstance(variables, dict):
        raise ScriptError("'vars:' must be a mapping of name -> value")

    _validate(steps, "steps", depth=0)

    script = Script(
        filename=filename,
        name=str(data.get("name") or os.path.splitext(filename)[0]),
        description=str(data.get("description") or ""),
        vars={str(k): v for k, v in variables.items()},
        steps=steps,
        source=text,
    )
    script.estimated_frames = estimate_frames(steps)
    return script


def _validate(steps, where: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise ScriptError(f"{where}: nested too deeply (max {MAX_DEPTH} levels)")
    if not isinstance(steps, list):
        raise ScriptError(f"{where}: expected a list of steps")

    for index, step in enumerate(steps):
        at = f"{where}[{index}]"
        if not isinstance(step, dict):
            raise ScriptError(f"{at}: each step must be a mapping, e.g. '- capture:'")

        found = [key for key in step if key in COMMANDS]
        if len(found) != 1:
            known = ", ".join(sorted(COMMANDS))
            raise ScriptError(
                f"{at}: each step needs exactly one command "
                f"(got {found or 'none'}). Known commands: {known}"
            )
        command = found[0]
        stray = set(step) - {command} - COMMANDS[command]
        if stray:
            raise ScriptError(f"{at}: unexpected key(s) {sorted(stray)} next to '{command}'")

        _validate_command(command, step, at, depth)


def _validate_command(command: str, step: dict, at: str, depth: int) -> None:
    value = step[command]

    if command == "message":
        if not isinstance(value, (str, int, float)):
            raise ScriptError(f"{at}: 'message' must be text")

    elif command == "set":
        if not isinstance(value, dict) or not value:
            raise ScriptError(f"{at}: 'set' needs a mapping, e.g. set: {{iso: \"800\"}}")

    elif command == "capture":
        if value is None:
            return
        if not isinstance(value, dict):
            raise ScriptError(f"{at}: 'capture' takes no value, or a mapping of options")
        unknown = set(value) - {"bulb", "download"}
        if unknown:
            raise ScriptError(f"{at}: unknown capture option(s) {sorted(unknown)}")
        if "bulb" in value and not _is_positive_number(value["bulb"]):
            raise ScriptError(f"{at}: 'bulb' must be a positive number of seconds")

    elif command == "burst":
        if not isinstance(value, dict) or not value:
            raise ScriptError(
                f"{at}: 'burst' needs {{seconds: N}} or {{frames: N}}")
        unknown = set(value) - {"seconds", "frames"}
        if unknown:
            raise ScriptError(f"{at}: unknown burst option(s) {sorted(unknown)}")
        if "seconds" in value and "frames" in value:
            raise ScriptError(
                f"{at}: give 'burst' either seconds or frames, not both")
        if "seconds" in value and not _is_positive_number(value["seconds"]):
            raise ScriptError(f"{at}: 'burst.seconds' must be a positive number")
        if "frames" in value:
            count = value["frames"]
            if not (isinstance(count, int) and not isinstance(count, bool) and count > 0):
                raise ScriptError(f"{at}: 'burst.frames' must be a whole number above 0")

    elif command == "wait":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ScriptError(f"{at}: 'wait' must be a number of seconds")

    elif command == "wait_until":
        _parse_clock(value, at)

    elif command == "repeat":
        bounded = (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        if not (value == "forever" or bounded):
            raise ScriptError(f"{at}: 'repeat' must be a whole number or 'forever'")
        if "steps" not in step:
            raise ScriptError(f"{at}: 'repeat' needs an indented 'steps:' block")
        if "every" in step and not _is_positive_number(step["every"]):
            raise ScriptError(f"{at}: 'every' must be a positive number of seconds")
        if "for" in step and not _is_positive_number(step["for"]):
            raise ScriptError(f"{at}: 'for' must be a positive number of seconds")
        if "until" in step:
            _parse_clock(step["until"], at)
        _validate(step["steps"], f"{at}.steps", depth + 1)

    elif command == "for_each":
        if not isinstance(value, dict):
            raise ScriptError(f"{at}: 'for_each' needs 'values:' and optionally 'setting:'")
        unknown = set(value) - {"values", "setting", "as"}
        if unknown:
            raise ScriptError(f"{at}: unknown for_each key(s) {sorted(unknown)}")
        values = value.get("values")
        if not isinstance(values, list) or not values:
            raise ScriptError(f"{at}: 'for_each.values' must be a non-empty list")
        if "steps" not in step:
            raise ScriptError(f"{at}: 'for_each' needs an indented 'steps:' block")
        _validate(step["steps"], f"{at}.steps", depth + 1)


def _is_positive_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _parse_clock(value, at: str) -> tuple[int, int]:
    match = TIME_RE.match(str(value).strip())
    if not match:
        raise ScriptError(f"{at}: expected a 24-hour time like \"05:30\", got {value!r}")
    return int(match.group(1)), int(match.group(2))


def clock_to_timestamp(value) -> float:
    """'05:30' -> unix time of the next 05:30 local."""
    hour, minute = _parse_clock(value, "time")
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target.timestamp()


# ------------------------------------------------------------------ estimation

def estimate_frames(steps) -> int | None:
    """Total frames a script will shoot, or None if it is open-ended."""
    total = 0
    for step in steps:
        if "capture" in step:
            total += 1
        elif "burst" in step:
            count = step["burst"].get("frames")
            if not count:
                return None          # a timed burst shoots an unknown number
            total += count
        elif "repeat" in step:
            inner = estimate_frames(step["steps"])
            if inner is None:
                return None
            count = step["repeat"]
            if count == "forever" or "until" in step or "for" in step:
                return None if inner else 0
            total += inner * count
        elif "for_each" in step:
            inner = estimate_frames(step["steps"])
            if inner is None:
                return None
            total += inner * len(step["for_each"]["values"])
    return total


def collect_settings(steps, env: dict | None = None) -> list[tuple[str, str]]:
    """Every (setting, value) a script will apply, for pre-flight validation.

    Values that depend on a loop variable are skipped rather than guessed.
    """
    env = env or {}
    found: list[tuple[str, str]] = []

    def resolve(value):
        try:
            return substitute(value, env)
        except ScriptError:
            return None       # depends on something only known at run time

    for step in steps:
        if "set" in step:
            settings = resolve(step["set"])
            if isinstance(settings, dict):
                for key, value in settings.items():
                    if value is not None:
                        found.append((str(key), value))
        elif "for_each" in step:
            spec = step["for_each"]
            setting = spec.get("setting")
            values = resolve(spec["values"]) or []
            if setting:
                found += [(str(setting), v) for v in values]
            found += collect_settings(step["steps"], env)
        elif "repeat" in step:
            found += collect_settings(step["steps"], env)
    return found


# ------------------------------------------------------------------ templating

def substitute(value, env: dict):
    """Replace {{name}} in strings, recursively through lists and mappings."""
    if isinstance(value, str):
        # A string that is exactly one placeholder keeps the variable's own type.
        whole = VAR_RE.fullmatch(value.strip())
        if whole:
            if whole.group(1) not in env:
                raise ScriptError(f"unknown variable '{{{{{whole.group(1)}}}}}'")
            return env[whole.group(1)]

        def replace(match):
            key = match.group(1)
            if key not in env:
                raise ScriptError(f"unknown variable '{{{{{key}}}}}'")
            return str(env[key])

        return VAR_RE.sub(replace, value)
    if isinstance(value, list):
        return [substitute(item, env) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, env) for key, item in value.items()}
    return value


# ------------------------------------------------------------------- discovery

def list_scripts(directory: str) -> list[dict]:
    """Every .yaml/.yml in the scripts folder, with parse errors surfaced."""
    entries: list[dict] = []
    if not os.path.isdir(directory):
        return entries
    for filename in sorted(os.listdir(directory)):
        if not filename.lower().endswith((".yaml", ".yml")):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                entries.append(parse_script(handle.read(), filename).summary())
        except (ScriptError, OSError, UnicodeDecodeError) as exc:
            entries.append({
                "filename": filename, "name": filename, "description": "",
                "estimated_frames": None, "ok": False, "error": str(exc),
            })
    return entries


def load_script(directory: str, filename: str) -> Script:
    path = _safe_path(directory, filename)
    if not os.path.isfile(path):
        raise ScriptError(f"no such script: {filename}")
    with open(path, encoding="utf-8") as handle:
        script = parse_script(handle.read(), filename)
    script.path = path
    return script


def _safe_path(directory: str, filename: str) -> str:
    """Reject anything that is not a plain .yaml name inside the folder."""
    if not filename or filename != os.path.basename(filename) or filename.startswith("."):
        raise ScriptError("invalid script name")
    if not filename.lower().endswith((".yaml", ".yml")):
        raise ScriptError("scripts must be .yaml or .yml files")
    return os.path.join(directory, filename)


def save_script(directory: str, filename: str, text: str,
                overwrite: bool = False) -> Script:
    """Validate then write a script. Nothing is stored unless it parses."""
    filename = os.path.basename((filename or "").strip())
    if filename and not filename.lower().endswith((".yaml", ".yml")):
        filename += ".yaml"
    path = _safe_path(directory, filename)

    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise ScriptError("that file is not UTF-8 text — is it really a script?") from None
    # Editors on Windows and phones leave CRLF and BOMs behind.
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not text.strip():
        raise ScriptError("the file is empty")

    script = parse_script(text, filename)          # raises ScriptError if bad

    if os.path.exists(path) and not overwrite:
        raise FileExistsError(filename)

    os.makedirs(directory, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)                          # atomic, never a half file
    script.path = path
    return script


# ----------------------------------------------------------------- interpreter

class Runtime:
    """What the interpreter is allowed to do. Supplied by the Sequencer."""

    def log(self, message: str) -> None: raise NotImplementedError
    def sleep(self, seconds: float) -> None: raise NotImplementedError
    def sleep_until(self, timestamp: float) -> None: raise NotImplementedError
    def sleep_until_monotonic(self, target: float) -> None: raise NotImplementedError
    def apply_settings(self, settings: dict) -> None: raise NotImplementedError
    def capture(self, bulb: float | None, download: bool) -> None: raise NotImplementedError
    def burst(self, seconds: float | None, frames: int | None) -> None:
        raise NotImplementedError
    def check_stop(self) -> None: raise NotImplementedError


def run(script: Script, rt: Runtime) -> None:
    env = dict(script.vars)
    _run_steps(script.steps, env, rt)


def _run_steps(steps, env: dict, rt: Runtime) -> None:
    for step in steps:
        rt.check_stop()

        if "message" in step:
            rt.log(str(substitute(step["message"], env)))

        elif "set" in step:
            settings = substitute(step["set"], env)
            rt.log("set " + ", ".join(f"{k}={v}" for k, v in settings.items()))
            rt.apply_settings(settings)

        elif "capture" in step:
            options = substitute(step["capture"] or {}, env)
            bulb = options.get("bulb")
            rt.capture(bulb=float(bulb) if bulb else None,
                       download=bool(options.get("download", False)))

        elif "burst" in step:
            options = substitute(step["burst"], env)
            seconds = options.get("seconds")
            frames = options.get("frames")
            rt.log("burst for {}".format(
                f"{seconds}s" if seconds else f"{frames} frame(s)"))
            rt.burst(seconds=float(seconds) if seconds else None,
                     frames=int(frames) if frames else None)

        elif "wait" in step:
            rt.sleep(float(substitute(step["wait"], env)))

        elif "wait_until" in step:
            when = clock_to_timestamp(substitute(step["wait_until"], env))
            rt.log(f"waiting until {substitute(step['wait_until'], env)}")
            rt.sleep_until(when)

        elif "for_each" in step:
            spec = step["for_each"]
            setting = spec.get("setting")
            name = spec.get("as", "value")
            for item in substitute(spec["values"], env):
                rt.check_stop()
                scope = dict(env, **{name: item})
                if setting:
                    rt.apply_settings({setting: item})
                _run_steps(step["steps"], scope, rt)

        elif "repeat" in step:
            count = step["repeat"]
            every = step.get("every")
            deadline = clock_to_timestamp(step["until"]) if "until" in step else None
            infinite = count == "forever" or count == 0
            if deadline:
                rt.log(f"looping until {step['until']}")

            index = 0
            started = time.monotonic()
            # 'for' is a duration from the moment the loop starts, and is kept
            # on the monotonic clock so an NTP step cannot cut it short.
            duration = step.get("for")
            ends_at = (started + float(substitute(duration, env))) if duration else None
            if ends_at:
                rt.log(f"looping for {substitute(duration, env)}s")

            while infinite or index < count:
                if deadline and time.time() >= deadline:
                    rt.log("reached the loop's end time")
                    break
                if ends_at and time.monotonic() >= ends_at:
                    rt.log("loop duration reached")
                    break
                rt.check_stop()
                _run_steps(step["steps"], dict(env, i=index + 1), rt)
                index += 1
                if every and (infinite or index < count):
                    # Slot-aligned so the interval never drifts over a long
                    # night; monotonic so an NTP clock step cannot bend it.
                    target = started + index * float(every)
                    # Do not sleep past the end of the loop's own window.
                    if ends_at and target >= ends_at:
                        break
                    rt.sleep_until_monotonic(target)
