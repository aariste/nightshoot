# Contributing

## Running the tests without a camera

The whole suite runs on any machine — no Raspberry Pi, no camera, no
`libgphoto2`. `tests/conftest.py` installs a stub in place of the `gphoto2`
module that behaves like a Nikon Z50: the same widget names, the same
shutter-speed spellings (`20.0000s`, `0.0166s`), and configurable costs for the
operations that dominate real timings.

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]" --no-deps
pip install flask PyYAML pytest pytest-timeout ruff
pytest -q
ruff check nightshoot tests
```

Install `gphoto2` (the Python binding) only if you want to run against a real
camera; it needs `libgphoto2-dev` and is deliberately not required for tests.

## Layout

| Path | What it holds |
|---|---|
| `nightshoot/camera.py` | Persistent libgphoto2 session, portable value matching, bulb, live view, reconnect |
| `nightshoot/sequencer.py` | Interval and script jobs on a worker thread |
| `nightshoot/scripts.py` | YAML parser, validator, upload, interpreter |
| `nightshoot/network.py` | Hotspot / Wi-Fi switching |
| `nightshoot/app.py` | Flask JSON API |
| `nightshoot/templates/index.html` | The entire phone UI, no build step |
| `install.sh`, `hotspot-test.sh` | Raspberry Pi setup and network troubleshooting |

## Things that are easy to get wrong

These are all real bugs that were found and fixed; the tests exist to keep them
fixed.

- **Never hold the camera lock on a status path.** `capture()` owns it for the
  whole exposure, so a four-minute bulb frame would otherwise freeze the UI.
  `snapshot()` and `storage()` take the lock with a timeout and serve cached
  values marked `busy`.
- **Never add a per-frame USB round trip.** Reading the config or fetching a
  preview costs far more than a 1/2000 exposure. The shutter duration is cached
  and previews are throttled.
- **Never spawn the `gphoto2` CLI per frame.** Re-claiming the USB interface is
  the documented cause of Nikon Z50 timeouts
  ([libgphoto2#925](https://github.com/gphoto/libgphoto2/issues/925)).
- **Match setting values by meaning, not by string.** Nikon bodies disagree on
  spelling; `resolve_choice` handles it, within 1% so a typo is never silently
  rounded to a different exposure. Fast speeds are truncated to four decimals by
  the camera, so `1/4000` arrives as `0.0002s` — invert that truncation rather
  than taking the nearest value.
- **Keep the scope Nikon.** Widget-name aliases exist because Nikon bodies vary
  among themselves, not as a step toward multi-vendor support. Other brands need
  different protocols, not different names; adding them without hardware to test
  on produces code that looks generic and fails in the field.
- **Validate scripts before writing and before arming.** A bad script must fail
  at upload or pre-flight, never three hours into a night.
- **Keep every file LF.** A stray CR makes the Pi fail with
  `env: 'bash\r': No such file or directory`. CI enforces this.
- **Claim the camera atomically.** `Sequencer._arm` reserves under the lock
  before the worker thread exists; checking `running` and spawning separately
  let two requests both start a worker and fight over one USB connection.
- **Validate JSON types explicitly.** `float()`, `int()` and `bool()` on
  attacker-supplied JSON produce 500s and surprises — `bool("false")` is `True`.
  Use the `_number` / `_integer` / `_flag` helpers in `app.py`, which raise
  `BadRequest` and answer 400.
- **Quote anything interpolated into a shell.** `network.py` runs as root;
  NetworkManager profile names go through `shlex.quote`.
- **Do not put secrets in polled responses.** The hotspot PSK is served only
  from its own endpoint, on request.

## UI changes

`index.html` is deliberately a single file with no build step, so it can be
edited over SSH on the Pi itself. If you change it, keep:

- night mode readable (deep red on black, no bright surfaces),
- tap targets at roughly 44 px,
- inputs at 16 px so iOS does not zoom on focus,
- a `<label for=...>` on every control.

## Pull requests

Run `pytest -q` and `ruff check nightshoot tests` before opening one. If you fix
a bug, add the test that would have caught it.
