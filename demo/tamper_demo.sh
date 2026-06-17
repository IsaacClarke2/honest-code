#!/usr/bin/env bash
# Self-contained demo of tamper detection.
#
# Spins up a throwaway $HOME with its own .claude/integrity and a tiny git
# project, then shows: honest session -> score signed -> agent secretly edits
# its own score -> the broken signature is caught, the commit is frozen, the
# score is penalized, and the status line turns red.
#
# Touches nothing outside a temp dir. Safe to run anywhere.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

export HOME="$SANDBOX"
IL="$SANDBOX/.claude/integrity"
mkdir -p "$IL"
cp "$SRC"/{check.py,enforce.py,signing.py,baseline.py,config.json,statusline.sh} "$IL/"

# A tiny project with a passing test, so sessions look real.
PROJ="$SANDBOX/project"; mkdir -p "$PROJ"; cd "$PROJ"
git init -q && git config user.email d@e.x && git config user.name demo
printf 'def test_math():\n    assert 1 + 1 == 2\n' > test_app.py
printf 'print("hello")\n' > app.py
git add -A && git commit -qm "init"

status() {
  echo '{"model":{"display_name":"Opus"},"context_window":{"used_percentage":8}}' \
    | bash "$IL/statusline.sh"
}

line() { printf '\n\033[1m%s\033[0m\n' "$1"; }

line "1. Honest session — the checker computes and SIGNS the score"
python3 "$IL/check.py" || true
printf 'status: '; status

line "2. The agent secretly rewrites its own score to 0.99"
python3 - "$IL/state.json" <<'PY'
import json, sys
p = sys.argv[1]
s = json.load(open(p))
s["directness"] = 0.99          # forge a perfect score
json.dump(s, open(p, "w"))      # signature NOT updated
print("   state.json hand-edited; signature left stale")
PY

line "3. Next command — enforce.py sees the broken signature and FREEZES"
if echo '{"tool_input":{"command":"git commit -m wip"}}' | python3 "$IL/enforce.py"; then
  echo "   (allowed — unexpected)"
else
  echo "   ^ blocked (exit 2)"
fi

line "4. The checker runs — tamper logged, score penalized"
python3 "$IL/check.py" || true
printf 'status: '; status
echo
