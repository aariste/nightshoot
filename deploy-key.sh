#!/usr/bin/env bash
# Give this Pi read-only pull access to the NightShoot repo.
#
# Run on the Pi, as the user who owns the checkout (NOT with sudo):
#
#     ./deploy-key.sh
#
# A deploy key is an SSH key GitHub trusts for one repository only. It beats a
# personal access token here: it cannot touch your other repos, it never
# expires, and nothing secret ends up in a git remote URL where `git remote -v`
# would print it.
set -euo pipefail

REPO="${REPO:-aariste/nightshoot}"
KEY="${KEY:-$HOME/.ssh/nightshoot_deploy}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

# The key has to belong to whoever runs `git pull`. Under sudo it would land in
# /root/.ssh, where the everyday user cannot read it, and the failure would only
# show up later as a confusing "Permission denied (publickey)".
if [ "$(id -u)" -eq 0 ]; then
  die "run this WITHOUT sudo, as the user that owns $SRC_DIR"
fi

command -v ssh-keygen >/dev/null || die "ssh-keygen not found: sudo apt install -y openssh-client"
command -v curl >/dev/null || die "curl not found: sudo apt install -y curl"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# ---------------------------------------------------------------- 1. the key

if [ -f "$KEY" ]; then
  say "Reusing the existing key at $KEY"
else
  say "Creating a deploy key"
  # No passphrase: the Pi must be able to pull unattended, in a field, with
  # nobody there to type one. The key is read-only and scoped to this one
  # repository, so the worst case if the SD card is stolen is that someone can
  # read code that is about to be public anyway.
  ssh-keygen -t ed25519 -N '' -C "nightshoot-pi-$(hostname)" -f "$KEY" >/dev/null
fi
chmod 600 "$KEY"
chmod 644 "$KEY.pub"

# ------------------------------------------------- 2. pin GitHub's host keys

say "Pinning GitHub's SSH host keys"
# Fetched over HTTPS from GitHub's own metadata endpoint rather than trusting
# whatever ssh-keyscan happens to be handed on first connection. Certificate
# validation does the vouching, so a hostile network cannot substitute its own.
KNOWN="$HOME/.ssh/known_hosts"
touch "$KNOWN"; chmod 600 "$KNOWN"

meta="$(mktemp)"
trap 'rm -f "$meta"' EXIT
curl -fsS --max-time 20 -o "$meta" https://api.github.com/meta \
  || die "could not reach api.github.com — check the Pi's internet connection"

# The program arrives on stdin and the file path as an argument, so neither the
# JSON nor the script has to survive being embedded inside the other.
keys="$(python3 - "$meta" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
for key in data["ssh_keys"]:
    for host in ("github.com", "ssh.github.com", "[ssh.github.com]:443"):
        print(host, key)
PY
)"
[ -n "$keys" ] || die "GitHub returned no host keys"

# Drop any entry we are about to replace, so a rotated key cannot leave a stale
# one behind to cause a "host key verification failed" months from now.
grep -vE '^(github\.com|ssh\.github\.com|\[ssh\.github\.com\]:443) ' "$KNOWN" > "$KNOWN.tmp" || true
printf '%s\n' "$keys" >> "$KNOWN.tmp"
mv "$KNOWN.tmp" "$KNOWN"
chmod 600 "$KNOWN"

# --------------------------------------------------------- 3. tell ssh to use it

say "Pointing ssh at the deploy key"
CONFIG="$HOME/.ssh/config"
touch "$CONFIG"; chmod 600 "$CONFIG"
# A marked block so re-running this script replaces its own work instead of
# stacking up duplicates.
if grep -q '# >>> nightshoot deploy key >>>' "$CONFIG"; then
  sed -i '/# >>> nightshoot deploy key >>>/,/# <<< nightshoot deploy key <<</d' "$CONFIG"
fi
cat >> "$CONFIG" <<EOF
# >>> nightshoot deploy key >>>
Host github.com
  User git
  IdentityFile $KEY
  # Offer only this key. Without it ssh works through every key it can find and
  # GitHub cuts the connection off after five with "too many authentication
  # failures" — before it ever reaches the right one.
  IdentitiesOnly yes
# <<< nightshoot deploy key <<<
EOF

# ------------------------------------------------------ 4. hand over the key

PUB="$(cat "$KEY.pub")"
cat <<EOF

$(printf '\033[1;36m==> Add this key to GitHub\033[0m')

  1. Open  https://github.com/$REPO/settings/keys/new
  2. Title: nightshoot-pi
  3. Paste the line below into "Key"
  4. Leave "Allow write access" UNCHECKED — the Pi only ever needs to read
  5. Click "Add key"

$PUB

EOF

if [ -t 0 ]; then
  read -rp "Press Enter once the key is added (Ctrl-C to stop here) " _
else
  warn "Not running interactively — add the key, then re-run this script to verify."
  exit 0
fi

# ---------------------------------------------------------------- 5. verify

say "Checking GitHub accepts it"
attempt_auth() {
  # A successful deploy-key login still exits non-zero, because GitHub refuses
  # the shell it was asked for. The greeting is the real signal.
  ssh -o BatchMode=yes -o ConnectTimeout=15 -T "$1" 2>&1 || true
}

HOST_ALIAS="github.com"
reply="$(attempt_auth git@github.com)"

if ! printf '%s' "$reply" | grep -q 'successfully authenticated'; then
  # Port 22 is blocked on plenty of home and campsite networks. GitHub serves
  # the same SSH endpoint on 443, which almost always gets through.
  warn "Direct SSH did not work. Trying GitHub's port 443 endpoint."
  reply443="$(attempt_auth ssh://git@ssh.github.com:443)"
  if printf '%s' "$reply443" | grep -q 'successfully authenticated'; then
    say "Port 443 works — making that the default"
    sed -i '/# >>> nightshoot deploy key >>>/,/# <<< nightshoot deploy key <<</d' "$CONFIG"
    cat >> "$CONFIG" <<EOF
# >>> nightshoot deploy key >>>
Host github.com
  User git
  HostName ssh.github.com
  Port 443
  IdentityFile $KEY
  IdentitiesOnly yes
# <<< nightshoot deploy key <<<
EOF
    reply="$reply443"
  else
    printf '\n%s\n\n' "$reply"
    die "GitHub did not accept the key. Check it was pasted whole (one line,
     starting 'ssh-ed25519') at https://github.com/$REPO/settings/keys"
  fi
fi

printf '\n    %s\n' "$reply"

# A deploy key greets you with the repository it belongs to. If that is some
# other repo, the key was pasted into the wrong project's settings.
if ! printf '%s' "$reply" | grep -qi "$REPO"; then
  warn "GitHub authenticated the key but named a different repository than"
  warn "$REPO. Check which repo you added it to."
fi

# --------------------------------------------------------- 6. wire up the repo

if [ -d "$SRC_DIR/.git" ]; then
  say "Pointing $SRC_DIR at git@github.com:$REPO.git"
  git -C "$SRC_DIR" remote set-url origin "git@github.com:$REPO.git" 2>/dev/null \
    || git -C "$SRC_DIR" remote add origin "git@github.com:$REPO.git"
  git -C "$SRC_DIR" fetch origin

  branch="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)"
  if git -C "$SRC_DIR" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    # Without this, `git pull` stops with "There is no tracking information for
    # the current branch" — the checkout knows the remote but not which branch
    # of it this one follows.
    git -C "$SRC_DIR" branch --set-upstream-to="origin/$branch" "$branch" >/dev/null
    say "Branch '$branch' now tracks origin/$branch"
  else
    warn "origin has no branch '$branch'. Run: git -C $SRC_DIR checkout main"
  fi
else
  warn "$SRC_DIR is not a git checkout (it was probably copied here with scp)."
  warn "Replace it with a real clone so updates become one command:"
  warn ""
  warn "    cd ~ && mv nightshoot nightshoot.old"
  warn "    git clone git@github.com:$REPO.git nightshoot"
  warn ""
  warn "Nothing is lost: photos live in /var/lib/nightshoot and your own"
  warn "scripts in /var/lib/nightshoot/scripts, neither of which is in here."
fi

cat <<EOF

$(printf '\033[1;36m==> Done\033[0m')

From now on, updating the Pi is:

    cd $SRC_DIR && ./update.sh

The key is read-only, so a pull works and a push will be refused.

EOF
