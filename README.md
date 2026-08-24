# NightShoot — Raspberry Pi night intervalometer for Nikon cameras

[![CI](https://github.com/aariste/nightshoot/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/nightshoot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A headless Pi that drives a Nikon over USB with gphoto2 and is controlled from
your phone over the Pi's own Wi-Fi hotspot. Built for multi-hour unattended
sequences: star trails, night timelapse, deep-sky stacking sets.

Why not just call the `gphoto2` CLI in a loop? Every invocation re-claims the USB
interface, and the Z50 is known to time out under that pattern
([libgphoto2#925](https://github.com/gphoto/libgphoto2/issues/925)). NightShoot
holds one libgphoto2 session open for the whole night and reconnects only when
the link genuinely drops.

**Scope is Nikon on purpose.** Developed and tested on a Z50, and written to suit
the Nikon range generally — widget names and shutter-speed spellings vary across
bodies, so both are handled. Other vendors are a different shape rather than a
different spelling (Canon holds the shutter open with a two-step remote-release
protocol instead of a bulb toggle), and supporting them without hardware to test
on would mean shipping guesses. See [Other cameras](#10-other-cameras).

---

## 1. Hardware

| Item | Notes |
|---|---|
| Raspberry Pi (Zero 2 W, 3, 4 or 5) | Zero 2 W is plenty — the Pi only sends PTP commands |
| USB cable | Camera end is **USB-C**. Pi 4/5 = USB-A→C. Zero 2 W = micro-USB **OTG** cable into the port marked `USB` (not `PWR`) |
| Power bank | 10 000 mAh+ runs a Zero 2 W all night |
| Camera power | See the power warning below |

**Power warning.** The Z50 draws its own power. A Pi USB port will *not* charge it
while shooting. Use either the Nikon EH-5/EP-5B dummy-battery adapter, a second
power bank into the camera's USB-C port, or simply carry two EN-EL25 batteries. A
20 s × 400 frame night will flatten one battery in the cold.

---

## 2. Flash the OS

Use **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)*. Lite matters: the
desktop images ship `gvfs`, which grabs the camera the moment it is plugged in and
leaves gphoto2 with *"Could not claim the USB device"*.

Before writing, open the gear/**Edit settings** panel and set:

- Hostname: `nightshoot`
- Enable SSH, with your public key or a password
- Username / password
- Your home Wi-Fi SSID + password and correct Wi-Fi country
- Locale and **time zone** — the intervalometer's "stop at 05:30" uses local time

Boot the Pi, then from your laptop:

```bash
ssh <user>@nightshoot.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

> If you did not set a Wi-Fi country in Imager, set it now — the field hotspot
> will not start without it: `sudo raspi-config nonint do_wifi_country BE`

---

## 3. Install NightShoot

Clone the repo onto the Pi and run the installer. If the repo is private, set up
pull access first — see [Updating the Pi](#updating-the-pi) below.

```bash
# on the Pi
git clone https://github.com/aariste/nightshoot.git ~/nightshoot
cd ~/nightshoot
sudo AP_SSID="NightShoot" AP_PASS="pick-a-good-one" ./install.sh
sudo reboot
```

Copying with `scp -r nightshoot <user>@nightshoot.local:~/` works too, but then
the Pi has no way to pull updates and no record of which version it is running.
A clone is worth the two extra minutes — and it keeps the executable bits, so
there is no `chmod` step.

> **`env: 'bash\r': No such file or directory`** means the script has Windows
> CRLF line endings — which happens when you copy from Windows rather than
> clone. Fix with `sed -i 's/\r$//' install.sh` (or `sudo apt install -y
> dos2unix && dos2unix install.sh`) and try again.

The installer:

1. installs `libgphoto2`, `gphoto2`, Python and NetworkManager,
2. purges `gvfs` and masks its camera volume monitor,
3. disables USB autosuspend via `cmdline.txt` (autosuspend silently drops the
   camera hours into a sequence),
4. creates a venv at `/opt/nightshoot/venv` with Flask + `python-gphoto2`,
5. enables the `nightshoot` systemd service (auto-restarts on crash, starts at boot),
6. creates a WPA2 hotspot that only comes up when no known Wi-Fi is in range.

### Updating the Pi

Once the Pi can reach the repo, updating is one command:

```bash
cd ~/nightshoot && ./update.sh
```

`update.sh` refuses rather than guesses. It stops if a sequence is running
(`--force` overrides), if the checkout has uncommitted changes, or if it has
diverged from the remote — a silent merge there would hide work nobody is
tracking. Otherwise it fast-forwards, reinstalls, restarts the service and
checks the web UI answers. If the service fails to come back it prints the last
30 journal lines instead of leaving you to find them.

#### Pull access for a private repo

Give the Pi a **deploy key**: an SSH key GitHub trusts for one repository only.
Better than a personal access token here — it cannot touch your other repos, it
never expires, and nothing secret ends up in a remote URL where `git remote -v`
would print it for anyone looking over your shoulder.

```bash
# on the Pi, as your normal user — NOT with sudo
cd ~/nightshoot
./deploy-key.sh
```

The script generates the key, prints it, and waits while you paste it into
`https://github.com/<owner>/<repo>/settings/keys/new`. **Leave "Allow write
access" unchecked** — the Pi only ever reads. Then it verifies the key, points
the checkout at the SSH remote and sets up branch tracking.

Details worth knowing:

- **Run it without `sudo`.** The key must belong to whoever runs `git pull`.
  Under `sudo` it lands in `/root/.ssh` and resurfaces later as a puzzling
  `Permission denied (publickey)`.
- **No passphrase.** The Pi has to pull unattended with nobody there to type
  one. The key is read-only and scoped to one repo, so a stolen SD card exposes
  nothing but code.
- **Host keys are pinned from `api.github.com/meta` over HTTPS**, not from
  whatever `ssh-keyscan` is handed on first connection. Re-running the script
  replaces rotated keys rather than leaving a stale one to fail months later.
- **Port 22 blocked?** Common on hotel, campsite and some home networks. The
  script notices, retries GitHub's port 443 endpoint, and makes that the default
  if it works.
- Set `REPO=owner/name ./deploy-key.sh` if you forked.

To undo it: delete the key at `https://github.com/<owner>/<repo>/settings/keys`,
then remove `~/.ssh/nightshoot_deploy*` and the marked block in `~/.ssh/config`.

---

## 4. Camera setup — do this once, in the light

On the Z50 itself:

| Setting | Value | Why |
|---|---|---|
| Mode dial | **M** | Anything else makes shutter/ISO/aperture read-only over PTP |
| Focus mode | **MF** | Autofocus will hunt forever on stars and block the shutter |
| Focus | manually on a bright star, live view zoomed to 100 % | |
| Long Exposure NR | **OFF** | It doubles every frame's cycle time and gaps your trails |
| High ISO NR | OFF or Low | |
| Auto ISO | **OFF** | |
| Vibration Reduction (lens) | OFF on a tripod | |
| Image review | OFF | Saves battery |
| Auto power off | Longest / disabled | A sleeping camera ignores PTP triggers |
| USB connection | Connect a cable, camera ON, **no** Snapbridge/Wi-Fi active | |

Then confirm the Pi sees it:

```bash
gphoto2 --auto-detect
gphoto2 --summary
```

---

## 5. Shooting

Join the `NightShoot` Wi-Fi from your phone and open **http://192.168.7.1:8080**
(at home, `http://nightshoot.local:8080` works over your normal network).

The UI defaults to deep red on black so it does not wreck your dark adaptation.
Tap the **☾ / ☀** button in the top-right to switch to a high-contrast light theme
for daytime use. The choice is stored in the browser on that device and survives
reloads, browser restarts and Pi reboots — so your phone stays on whichever mode
you last picked.

1. **Camera** panel — set shutter, ISO, aperture, format, then *Apply to camera*.
2. *Test frame* — the thumbnail appears under **Last frame**; tap it to open full size
   and check focus.
3. **Sequence** panel — set frames, interval, start delay, optional stop time. The
   line under the form previews how long the run takes and when it will finish, and
   flags impossible plans before you start.
4. **Start**. A sticky bar appears at the top with the frame count and a STOP button.

### Two exposure modes

**Camera shutter (bulb = off)** — the normal choice. The Z50's shutter dial covers
up to 30 s; the Pi just fires the trigger. Most accurate and most reliable.
Set *interval* to exposure + 3–5 s for card write.

**Bulb (bulb = on)** — for exposures longer than 30 s. Set the camera's shutter
speed to `Bulb` in the Camera panel first, then set *Bulb exposure* to the number
of seconds you want. The Pi opens and closes the shutter. Timing accuracy is
roughly ±0.2 s, which is irrelevant at 60 s+.

### Suggested starting points

| Subject | Shutter | ISO | Interval | Frames |
|---|---|---|---|---|
| Star trails (stack later) | 25 s | 800 | 28 s | until dawn |
| Milky Way timelapse | 15 s | 3200 | 18 s | 400 |
| Long star trails, single frames | bulb 240 s | 400 | 250 s | 60 |
| Meteors / lightning (JPEG) | 1/2000 | 3200 | 0 = fastest | until stopped |

Where to write files: leaving *Copy files to Pi* **off** keeps RAWs on the camera's
SD card, which is fastest and safest. Turn it on only if you want the files on the
Pi as well — a Z50 NEF is ~25 MB, so 400 frames needs ~10 GB free.

### Ending the night

Press **Stop**, then **Admin → Shut down** in the UI before pulling power.
Yanking a Pi mid-write is the single most common way to corrupt the SD card.

### Collapsible panels

Live view, Scripts, Last frame, Log and Admin are collapsible, and each one
remembers whether you left it open — per device, like the theme.

The UI shows, at a glance: frame count, countdown to the next frame, elapsed time,
predicted finish, camera model, battery, and **free space on the camera card**
(with an estimate of how many frames still fit). The Pi's own disk is only shown
when downloading is turned on, because otherwise it is irrelevant.

Shutter speeds are shown the way a photographer reads them — `20"`, `1/60`,
`1/4000`. The dropdown is built from the camera's own list every time it
connects, so it only ever offers speeds your body actually has; the labels are
cosmetic and the camera's own value is what gets sent back.

During a long exposure the status line counts the frame down (`exposing · 183s
left`), so a four-minute bulb frame never looks like a hang.

### While a sequence is running

A sticky bar pins itself to the top of the screen with the frame count, the
current state and **PAUSE** / **STOP**, so you never have to scroll through the
form to stop a run. Stopping asks for confirmation once frames have been
captured. Controls that would disturb the camera mid-run — camera settings, test
frame, live view, start — are disabled until the run ends.

The page also requests a screen wake lock while shooting (where the browser
allows it), and stops polling entirely when you put the phone away.

### Live view

For framing and focusing before the sequence starts. Pick a refresh rate and the
UI polls single JPEG frames from the camera.

Live view and the shutter cannot share the sensor, so it is deliberately
restricted: the control is disabled while a sequence runs, starting a run turns it
off automatically, and the server refuses preview requests with a `409` rather
than fighting an in-flight exposure. Collapsing the panel also stops the polling,
so it never quietly drains the camera battery in your bag.

### Changing other camera settings

The UI exposes shutter, ISO, aperture and format. Anything else your body offers
over PTP is reachable from the shell or the API:

```bash
gphoto2 --list-config                     # every setting this body exposes
gphoto2 --get-config whitebalance         # current value plus valid choices
curl -X POST http://192.168.7.1:8080/api/settings \
  -H 'Content-Type: application/json' -d '{"whitebalance":"Daylight"}'
```

Scripts can set any of them too — see the `set:` command below.

### Admin

One panel for everything to do with the Pi rather than the camera, grouped into
Network, Pi health, About, Service log and Power.

**Network** — which network the Pi is on, its address, the hotspot SSID and
(on request) its password, plus the switch between hotspot and Wi-Fi described
below.

**Pi health** — CPU temperature, under-voltage and throttling flags, SD card
space, memory, load and uptime. It warns about the two failures that actually
ruin nights: **under-voltage**, which is the commonest cause of mysterious Pi
misbehaviour and means a better supply or cable, and a CPU above 70 °C, since
the Pi throttles hard at 80.

**About** — hostname, NightShoot version, connected camera, and the Pi's clock.
Worth a glance: a Pi with no network loses the time across a reboot, and a wrong
clock makes "stop at 05:30" and every file timestamp wrong.

**Service log** — the last 120 journal lines, so a failed night can be diagnosed
in the field instead of over SSH the next day.

**Power** — *Restart service* reconnects the camera without rebooting, and is
the first thing to try if the camera stops responding. *Reboot* and *Shut down*
are both confirmed, and all three refuse while a sequence is running.

### Switching between hotspot and Wi-Fi

The **Admin** panel shows which network the Pi is on, its address, and the
hotspot's SSID and password, with one button to switch modes.

Be clear about what this does: **the switch cuts the connection the page is
served over.** The UI says so before it acts, and tells you exactly where to
reconnect. The change itself runs detached on the Pi, so it completes even though
the request that triggered it is killed halfway through.

Turning the hotspot **on** offers a safety net — 5, 10 or 30 minutes, after which
the Pi returns to your normal Wi-Fi by itself. Use it when testing from home: if
the hotspot never appears, or your phone will not join it, you simply wait and
the Pi comes back. Choose *no revert* once you trust it, or when you are heading
out and want the hotspot to stay up.

Turning it **off** is refused when there is no other saved network, because that
would leave the Pi with no way in at all. Any pending revert can be cancelled
from the same panel.

A running sequence is unaffected by either switch — capture happens over USB, and
the sequencer keeps shooting while the Wi-Fi is reconfigured underneath it.

For the same operations over SSH, plus prerequisite diagnostics, use
`hotspot-test.sh` (see Troubleshooting).

---

## 6. Scripts

For anything more elaborate than a fixed interval — bracketing, ISO ladders,
multi-phase nights — use a YAML script. The installer seeds five examples and
never overwrites your edits on re-run.

**Adding a script from the UI.** The Scripts panel has two buttons:

- **Upload file** — pick one or more `.yaml` files from the device you are
  holding. Works from a laptop browser or a phone.
- **Paste new** — type or paste a script straight into the page and give it a
  name. This is the practical option on a phone, where file pickers are painful.

Either way the Pi parses the script *before* saving it. A script that does not
validate is rejected with the exact error and never reaches the folder, so you
cannot accumulate files that will only blow up hours into a night. Uploading a
name that already exists asks before replacing. A newly uploaded script is
selected in the list straight away and can be run without restarting anything.

You can still copy files in over SSH if you prefer:

```bash
scp my-sequence.yaml <user>@nightshoot.local:/var/lib/nightshoot/scripts/
```

Tap **Reload list** and it appears. The panel shows the description and the frame
count (or *open-ended*), **View source** shows the file, and a script that fails
to parse is listed with a ⚠ and the exact error rather than being silently
skipped.

Scripts are purely declarative — the interpreter understands only the commands
below and never evaluates code, so a bad script can waste a night but cannot run
anything on the Pi.

### Commands

| Command | Example | Notes |
|---|---|---|
| `set` | `set: {iso: "800", shutterspeed: "25"}` | Same names as the Camera panel |
| `capture` | `capture:` / `capture: {bulb: 240, download: true}` | `bulb` is seconds |
| `burst` | `burst: {seconds: 10}` or `burst: {frames: 30}` | Fires flat out; see below |
| `wait` | `wait: 15` | Seconds |
| `wait_until` | `wait_until: "23:30"` | Next occurrence of that local time |
| `message` | `message: "pass {{i}}"` | Writes to the on-screen log |
| `repeat` | `repeat: 400` or `repeat: forever` | Needs an indented `steps:` block |
| `for_each` | `for_each: {setting: iso, values: ["400","1600"]}` | Needs `steps:`; also binds `{{value}}` |

Modifiers on `repeat`:

- `every: 28` — start each pass on a fixed cadence. Slot-aligned against the loop
  start, so a long night never accumulates drift the way `wait` does.
- `until: "05:30"` — leave the loop at that local time.
- `for: 60` — leave the loop after that many seconds. Use `for` for a stretch of
  a run ("a minute of brackets") and `until` for a time of night ("stop at
  dawn"). Both together is allowed; whichever comes first ends the loop.

Numbers in a script are checked before the run starts, so `bulb`, `every`, `for`
and the `burst` options must be literal numbers, not `{{variables}}`. You find
out a script is wrong at your desk rather than in a field at 2 a.m.

### Bursts inside a script

`burst` is the script form of what the interval runner does when the interval is
0: the shutter is fired without waiting for each file, so the camera's buffer
rather than the USB round trip sets the pace. Give it either a duration or a
frame count, not both.

```yaml
steps:
  - set: {imageformat: "JPEG Fine", shutterspeed: "1/2000", iso: "3200"}
  - burst: {seconds: 10}          # flat out for ten seconds

  - set: {imageformat: "NEF (Raw)", iso: "800"}
  - repeat: forever
    for: 60                       # a minute of brackets
    every: 10
    steps:
      - for_each: {setting: shutterspeed, values: ["1/60", "1/15", "4"]}
        steps:
          - capture:

  - set: {imageformat: "JPEG Fine", shutterspeed: "1/2000", iso: "3200"}
  - burst: {seconds: 10}          # and flat out again
```

That is `examples/scripts/burst-bracket-burst.yaml`, the pattern for meteor
showers or lightning: maximum frames while something is happening, considered
exposures in between.

Worth knowing:

- A `set:` cannot run *inside* a burst. Settings apply between phases, which is
  the point — a mid-flight change would land on frames you did not intend it for.
- There is no `bulb` and no `download` in a burst; both defeat the purpose.
- A body that cannot fire without waiting for each file makes `burst` fail with
  a clear message rather than quietly running slow captures instead.
- Changing image format between phases costs a USB round trip, and the camera
  may still be flushing the previous burst. If the first frame of the next phase
  must be prompt, keep the format the same throughout.
- A frame-counted burst adds to the script's estimated frame total; a timed one
  cannot be predicted, so the estimate becomes "unknown".

Variables: declare them under `vars:` and reference them as `{{name}}` anywhere.
`repeat` binds `{{i}}` (1-based pass number) and `for_each` binds `{{value}}`
(or `{{name}}` if you set `as: name`).

### Shutter speeds are matched by value, not by spelling

Bodies disagree on how they report the same setting. A Z50 lists shutter speeds as
`20.0000s` and `0.0166s`; other cameras use `20` and `1/60`. Scripts stay portable
because values are matched by meaning:

```yaml
set: {shutterspeed: "20"}      # matches the camera's 20.0000s
set: {shutterspeed: "1/60"}    # matches 0.0166s
set: {shutterspeed: "1/4000"}  # matches 0.0002s
set: {shutterspeed: "Bulb"}    # case-insensitive
```

Cameras truncate these decimals to four places, so `1/4000` — really 0.00025 s —
arrives as `0.0002s`. NightShoot applies the same truncation to whatever you
write before comparing, which resolves that exactly rather than guessing at the
nearest value.

Otherwise a value only matches within 1%, so a typo is never silently rounded to a
different exposure — you get `no setting equal to '22'. Closest is '20.0000s'.`

Before a script starts, every setting it will apply is checked against the camera,
so a bad value is reported immediately rather than arming the sequence and
aborting after zero frames. To see exactly what your body accepts:

```bash
gphoto2 --get-config shutterspeed
```

### A worked example

```yaml
name: Blue hour into full dark
description: Bracket while the sky drops, then settle into star trails.

vars:
  fmt: "NEF (Raw)"

steps:
  - set: {imageformat: "{{fmt}}", iso: "200"}
  - message: "blue hour brackets"
  - repeat: 12
    every: 120
    steps:
      - for_each:
          setting: shutterspeed
          values: ["1/4", "2", "8"]
        steps:
          - capture:

  - message: "switching to trails"
  - set: {iso: "800", shutterspeed: "25"}
  - repeat: forever
    every: 28
    until: "05:30"
    steps:
      - capture:
```

Pause, resume, stop, the live log, thumbnails and the frame counter all work
during a script exactly as they do for a plain interval run. Capture failures are
retried with backoff and the script is only abandoned after five in a row.

### Fast frames and bursts

Set **interval** to `0` to shoot as fast as the camera allows — for meteors,
lightning or anything that will not wait. The status panel shows the measured
**per frame** time and frames per second, which is the honest answer to how fast
your body actually goes.

With interval `0`, no bulb and no downloading, NightShoot switches to a **burst
path** that fires the shutter with libgphoto2's `trigger_capture` and collects
the files from the camera's event queue afterwards, rather than waiting for each
file to be written before firing again. That removes a full PTP round trip per
frame. The frame counter stays accurate — files are counted as the camera
reports them, and the last few are drained after the final trigger.

NightShoot also keeps its own overhead out of the way: the shutter speed is read
from a short-lived cache rather than re-queried every frame, and preview
thumbnails are skipped entirely during a burst.

**It will still not match holding the shutter button down.** That is a protocol
limit, not a tuning problem. The camera's continuous-release drive mode runs
entirely inside the camera; remote capture goes over USB PTP, which is
request/response and is not exposed to the body's burst machinery
([libgphoto2#968](https://github.com/gphoto/libgphoto2/issues/968)). Expect a few
frames per second at best, against 11 fps for the Z50's own burst.

What actually moves the needle after that is the camera:

| Setting | Effect on burst rate |
|---|---|
| **JPEG instead of RAW** | The single biggest win. A Z50 NEF is ~25 MB; the buffer fills in a few frames and then every shot waits on the card. |
| **A fast UHS-I card** | Once the buffer is full, the card is the bottleneck. |
| **Long Exposure NR off** | Otherwise the camera is busy for as long as the exposure after *every* frame. |
| **High ISO NR off or low** | In-camera processing delays the next frame. |
| **Manual focus** | No AF hunt between frames. |

If you need the camera's true burst rate, use its own drive mode and pull the
files off the card afterwards. NightShoot is for unattended sequences, and no
gphoto2-based tool can beat this limit.

The shipped `fast-burst.yaml` example is set up along these lines.

---

## 7. Troubleshooting

**"No camera found" / "Could not claim the USB device"**
```bash
gphoto2 --auto-detect                      # is it seen at all?
sudo systemctl stop nightshoot             # release our own session first
ps aux | grep -E 'gvfs|gphoto'             # something else holding it?
```
Also try a different cable — many USB-C cables are charge-only.

**Camera is detected but every setting is read-only**
The mode dial is not on M, or the camera is in playback/menu. Half-press the
shutter on the camera and reconnect from the UI.

**Bulb frames never complete**
The camera's shutter speed must be set to `Bulb` (not `Time`, and not a numeric
speed) before starting a bulb sequence.

**Your body is newer than the packaged libgphoto2**
Raspberry Pi OS Trixie ships libgphoto2 2.5.30, which knows the Z50. If a newer
body is not recognised, build the current release:
```bash
sudo apt install -y build-essential autoconf automake libtool pkg-config \
  libltdl-dev libusb-1.0-0-dev libexif-dev libjpeg-dev libgd-dev
git clone --depth 1 https://github.com/gphoto/libgphoto2
cd libgphoto2 && autoreconf -is && ./configure --prefix=/usr/local && make -j"$(nproc)"
sudo make install && sudo ldconfig
sudo /opt/nightshoot/venv/bin/pip install --no-binary :all: --force-reinstall gphoto2
sudo systemctl restart nightshoot
```

**Sequence stopped overnight**
```bash
journalctl -u nightshoot --since yesterday --no-pager | tail -50
```
Usual culprits: camera battery, camera auto-power-off, or a full SD card.

**Hotspot does not appear**

The Pi has one Wi-Fi radio, so it cannot usefully be a client and an access point
at the same time. The hotspot is configured with a *lower* autoconnect priority
than your saved networks — at home your normal Wi-Fi wins, and in the field, with
nothing else in range, the hotspot comes up on its own.

That means you cannot see the hotspot from your desk without dropping the Pi off
your Wi-Fi. Use the helper rather than doing it by hand:

```bash
sudo /opt/nightshoot/hotspot-test.sh check      # non-disruptive: prerequisites + config
sudo /opt/nightshoot/hotspot-test.sh diagnose   # why it will not start
sudo /opt/nightshoot/hotspot-test.sh start      # hotspot on, auto-reverts in 10 min
sudo /opt/nightshoot/hotspot-test.sh status     # what is active now
sudo /opt/nightshoot/hotspot-test.sh stop       # back to normal Wi-Fi immediately
```

`check` changes nothing. It verifies the four things that actually stop a hotspot
from starting, and prints the exact fix for whichever is missing:

| Requirement | Why | Fix |
|---|---|---|
| `dnsmasq-base` | NetworkManager runs it as the DHCP server for `ipv4.method shared` | `sudo apt install -y dnsmasq-base` |
| Wi-Fi country set | AP mode stays disabled until the regulatory domain is known | `sudo raspi-config nonint do_wifi_country BE` |
| Radio not rfkill-blocked | a soft block silently prevents activation | `sudo rfkill unblock wifi` |
| Radio supports AP mode | not all adapters do | use the Pi's built-in Wi-Fi |

**If `start` appears to do nothing**, the usual cause is the first row.
`dnsmasq-base` is only a *Recommends* of NetworkManager, so an install done with
`--no-install-recommends` leaves it out and activation fails instantly. Run
`check`; it will say so outright. `diagnose` goes further — it retries the
activation in the foreground and prints the real `nmcli` error plus the relevant
NetworkManager log lines.

`start` **will freeze an SSH session that runs over Wi-Fi.** That is expected, not
a failure. Reconnect by joining the `NightShoot` network from your phone or laptop
and browsing to `http://192.168.7.1:8080`, or `ssh <user>@192.168.7.1`. If the
hotspot never appears, the Pi rejoins your normal Wi-Fi automatically after the
timeout, so you cannot lock yourself out.

**Testing without dropping your SSH session:** plug in Ethernet and SSH over that
instead. Then the Wi-Fi radio is free and you can start and stop the hotspot as
often as you like with the connection unaffected. On a Pi Zero 2 W (no Ethernet),
use a USB Ethernet adapter, or just accept the reconnect — that is what `start`
is designed around.

Manual equivalents, if you prefer:

```bash
nmcli connection show                  # what profiles exist
nmcli -f NAME,TYPE,DEVICE connection show --active
nmcli connection up nightshoot-ap      # force the hotspot on
nmcli connection down nightshoot-ap    # and off again
iw list | grep -A8 "Supported interface modes"   # does the radio do AP mode?
```

**Real-world check.** The honest test is to take the rig somewhere out of range of
every saved network, power it on, and see the `NightShoot` SSID appear after
30–60 s. Before you rely on it in a field at 1am, do that once in your driveway
with your phone's hotspot turned off.

---

## 8. Layout

```
nightshoot/
├── install.sh                  one-shot Pi setup
├── deploy-key.sh               give the Pi read-only pull access to this repo
├── update.sh                   pull, reinstall, restart — with safety checks
├── hotspot-test.sh             safely test the field hotspot from home
├── pyproject.toml              packaging, pytest and ruff config
├── systemd/nightshoot.service  boot + auto-restart
├── examples/scripts/*.yaml     seeded into /var/lib/nightshoot/scripts
├── tests/                      full suite, runs without a camera
└── nightshoot/
    ├── camera.py               persistent libgphoto2 session, config tree,
    │                           bulb, live view, reconnect
    ├── network.py              hotspot / Wi-Fi switching for the Admin pane
    ├── system.py               host health, restart, reboot, shutdown
    ├── sequencer.py            interval + script jobs on a worker thread
    ├── scripts.py              YAML script parser, validator, upload and interpreter
    ├── app.py                  Flask JSON API
    └── templates/index.html    phone UI
```

The API is plain JSON if you would rather script it:

```bash
curl -X POST http://192.168.7.1:8080/api/start \
  -H 'Content-Type: application/json' \
  -d '{"frames":0,"interval_s":28,"bulb":false,"until":"05:30"}'
curl http://192.168.7.1:8080/api/status
curl -X POST http://192.168.7.1:8080/api/settings \
  -H 'Content-Type: application/json' -d '{"whitebalance":"Daylight"}'
curl http://192.168.7.1:8080/preview.jpg -o frame.jpg
curl http://192.168.7.1:8080/api/scripts
curl -F 'file=@my-sequence.yaml' http://192.168.7.1:8080/api/scripts/upload
curl -X POST http://192.168.7.1:8080/api/scripts/run \
  -H 'Content-Type: application/json' -d '{"filename":"star-trails.yaml"}'
curl http://192.168.7.1:8080/api/status
curl http://192.168.7.1:8080/api/system          # temperature, throttling, disk
curl http://192.168.7.1:8080/api/logs            # recent service log
curl http://192.168.7.1:8080/api/network
curl -X POST http://192.168.7.1:8080/api/network/hotspot \
  -H 'Content-Type: application/json' -d '{"enabled":true,"revert_after":600}'
curl -X POST http://192.168.7.1:8080/api/stop
```

Security note: the API is unauthenticated by default, so anyone who can reach
port 8080 can upload scripts, change settings and shut the Pi down. That is a
deliberate trade for a WPA2-protected field hotspot, where the Wi-Fi password
*is* the access control. Two things are enforced regardless:

- **Cross-origin requests are refused.** Without this, any web page you happened
  to open while on the Pi's network could POST to `/api/shutdown` — no CORS
  preflight is needed for a request that carries no body, so the browser would
  simply send it.
- **The hotspot password is not in the polled status.** It is served only from
  `/api/network/hotspot-password`, when the Admin panel asks.

If the Pi will ever share a network with anything you do not control, set a
shared secret:

```bash
sudo systemctl edit nightshoot
# [Service]
# Environment=NIGHTSHOOT_TOKEN=a-long-random-string
sudo systemctl restart nightshoot
```

Every request must then carry `X-NightShoot-Token: …` or `?token=…`.

The service runs as root because it needs USB device access, NetworkManager and
clean shutdown, but the unit file sandboxes it: read-only filesystem apart from
`/var/lib/nightshoot`, no access to home directories, `NoNewPrivileges`, and a
restricted set of address families.

---

## 9. Development

The whole test suite runs on any machine — no Pi, no camera, no libgphoto2:

```bash
pip install flask PyYAML pytest pytest-timeout ruff
pytest -q
ruff check nightshoot tests
```

`tests/conftest.py` substitutes a stub for the `gphoto2` module that mimics a
Nikon Z50, down to its shutter-speed spellings. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the layout and the list of mistakes the tests exist to prevent.

---

## 10. Other cameras

NightShoot is for Nikon. That is a deliberate limit, not an oversight.

### Across the Nikon range

Nikon bodies disagree among themselves, and that is handled:

- **Widget names.** A D5300 exposes `shutterspeed2`, `isospeed`, `imagequality`
  and `capturemode` where a Z50 uses `shutterspeed`, `iso`, `imageformat` and
  `expprogram`. Each setting is looked up through a list of candidates.
- **Value spelling.** Some bodies report `0.0166s`, others `1/60`. Values are
  matched by meaning, so scripts move between bodies unchanged.
- **Missing controls.** Not every Nikon exposes a bulb toggle, and it disappears
  entirely when the mode dial is off M. That is detected at connect time: the UI
  refuses to enable bulb mode and explains why, rather than arming a sequence
  that dies on its first frame.

### Why not other brands

The differences are structural, not cosmetic:

- Canon holds the shutter open by driving `eosremoterelease` through press and
  release values, after separately setting the shutter speed to `bulb`. Nikon
  uses one toggle. Different protocol, not a different name.
- Many Sony bodies refuse shutter-speed changes over PTP unless the camera is in
  PC Remote mode.
- Event models differ — the burst path depends on the camera reporting
  `FILE_ADDED`, which not every driver does the same way.

Each of those needs the hardware in hand to get right. Supporting them on paper
would produce code that looks generic and fails in a field at 2am, which is worse
than an honest limit.

### If you plug in something else

It is flagged, not blocked. NightShoot warns in the status panel that the body is
untested and carries on — much of this is plain PTP and basic capture may work
fine. Bulb, live view and burst are the parts most likely to fail.

```bash
NIGHTSHOOT_ALLOW_ANY=1        # silences the log warning; nothing else changes
```

To see what a body actually exposes:

```bash
gphoto2 --auto-detect
gphoto2 --list-config
gphoto2 --get-config shutterspeed
```

If you want another brand supported properly, that output plus a willingness to
test is what it would take. For broad multi-vendor support today, use
[INDI with Ekos](https://indilib.org/) — it has per-camera drivers maintained by
people who own the bodies.
