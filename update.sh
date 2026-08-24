#!/usr/bin/env bash
# Pull the latest NightShoot and reinstall it. Run on the Pi:
#
#     cd ~/nightshoot && ./update.sh
#
# Needs pull access first — see ./deploy-key.sh.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${NIGHTSHOOT_PORT:-8080}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

cd "$SRC_DIR"
[ -d .git ] || die "$SRC_DIR is not a git checkout. Run ./deploy-key.sh first."

# ------------------------------------------------- don't interrupt a shoot

# Restarting the service mid-exposure loses the frame and, worse, the rest of
# the night if nobody is there to notice. Ask the running instance first.
STATUS_JSON="$(mktemp)"
trap 'rm -f "$STATUS_JSON"' EXIT
if curl -fsS --max-time 3 -o "$STATUS_JSON" "http://127.0.0.1:$PORT/api/status" 2>/dev/null; then
  # Parsed rather than grepped: 'running' sits inside the "sequence" object and
  # a flat text match would also trip over anything else named the same. Both
  # the program and the JSON travel as files, so neither has to survive being
  # embedded inside the other.
  busy="$(python3 - "$STATUS_JSON" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        seq = json.load(handle).get("sequence") or {}
except (ValueError, OSError, IndexError):
    sys.exit(0)
if seq.get("running"):
    print("{} {}".format(seq.get("state", "?"), seq.get("frames_done", 0)))
PY
)"
  if [ -n "$busy" ]; then
    warn "A sequence is running right now (state ${busy% *}, ${busy##* } frames so far)."
    if [ "$FORCE" -eq 1 ]; then
      warn "--force given, carrying on and ending that run."
    else
      die "Stop it from the web UI first, or re-run with: ./update.sh --force"
    fi
  fi
fi

# ------------------------------------------------------------ local changes

if [ -n "$(git status --porcelain)" ]; then
  warn "This checkout has uncommitted changes:"
  git --no-pager status --short
  warn ""
  die "Save or discard them first. To throw them away:  git reset --hard && git clean -fd"
fi

# ------------------------------------------------------------------- pull

say "Fetching"
git fetch --prune origin

branch="$(git rev-parse --abbrev-ref HEAD)"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ -n "$upstream" ] || die "branch '$branch' tracks nothing. Run ./deploy-key.sh."

if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$upstream")" ]; then
  say "Already up to date ($(git rev-parse --short HEAD))"
  # Still worth reinstalling if the service is dead — that is usually why
  # somebody ran this in the first place.
  if systemctl is-active --quiet nightshoot; then
    printf '\n    Service is running. Nothing to do.\n\n'
    exit 0
  fi
  warn "The service is not running, so reinstalling anyway."
else
  say "Incoming"
  git --no-pager log --oneline --no-decorate HEAD.."$upstream" | sed 's/^/    /'
  # Fast-forward only. A merge commit here would mean the Pi has work of its
  # own that nobody is tracking, and silently merging it hides that.
  git merge --ff-only "$upstream" \
    || die "cannot fast-forward: this checkout has diverged from $upstream.
     Fix with:  git reset --hard $upstream   (discards local commits)"
fi

# -------------------------------------------------------------- reinstall

say "Reinstalling to /opt/nightshoot"
sudo ./install.sh

say "Restarting the service"
sudo systemctl restart nightshoot
sleep 2

if systemctl is-active --quiet nightshoot; then
  say "Running $(git rev-parse --short HEAD)"
  curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 \
    && printf '\n    Web UI is answering on port %s.\n\n' "$PORT" \
    || warn "Service is up but not answering yet — give it a few seconds."
else
  warn "The service did not come back. Last 30 log lines:"
  sudo journalctl -u nightshoot -n 30 --no-pager
  exit 1
fi
