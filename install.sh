#!/usr/bin/env bash
# honest-code installer — one command, idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/IsaacClarke2/honest-code/master/install.sh | bash
#
# or, from a local checkout:   ./install.sh
#
# Lays out the hooks, wires them into ~/.claude/settings.local.json (merging,
# not clobbering, your existing settings), installs the status line, and appends
# the integrity rules to your global CLAUDE.md. Safe to re-run.
set -euo pipefail

REPO_URL="https://github.com/IsaacClarke2/honest-code.git"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CLAUDE_DIR/integrity"
SETTINGS="$CLAUDE_DIR/settings.local.json"
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
SL="$CLAUDE_DIR/statusline.sh"
RUNTIME=(check.py enforce.py signing.py baseline.py track_bash.py statusline.sh config.json)

say() { printf '\033[1m▸ %s\033[0m\n' "$1"; }

mkdir -p "$DEST"

# 1. Place the runtime files (from a local checkout if present, else clone).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/check.py" ]; then
    say "Installing from local checkout"
    for f in "${RUNTIME[@]}"; do cp "$HERE/$f" "$DEST/"; done
else
    say "Fetching honest-code"
    if [ -d "$DEST/.git" ]; then
        git -C "$DEST" pull -q --ff-only
    else
        tmp="$(mktemp -d)"
        git clone -q --depth 1 "$REPO_URL" "$tmp"
        for f in "${RUNTIME[@]}"; do cp "$tmp/$f" "$DEST/"; done
        rm -rf "$tmp"
    fi
fi
chmod +x "$DEST/statusline.sh"
cp "$DEST/statusline.sh" "$SL"
chmod +x "$SL"

# 2. Merge hooks + status line into settings.local.json (idempotent).
ADD="$(cat <<JSON
{
  "statusLine": { "type": "command", "command": "$SL" },
  "hooks": {
    "PreToolUse":  [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "python3 $DEST/enforce.py", "timeout": 15000 } ] } ],
    "PostToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "python3 $DEST/track_bash.py" } ] } ],
    "Stop":        [ { "matcher": "",     "hooks": [ { "type": "command", "command": "python3 $DEST/check.py", "timeout": 30000 } ] } ]
  }
}
JSON
)"

if command -v jq >/dev/null 2>&1; then
    say "Wiring hooks into $SETTINGS"
    [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
    # Drop any prior honest-code entries (dedup), then append ours.
    tmp="$(mktemp)"
    jq -n --argjson cur "$(cat "$SETTINGS")" --argjson add "$ADD" '
      def strip($a): [ ($a // [])[] | select((tostring | test("integrity")) | not) ];
      $cur
      | .statusLine = $add.statusLine
      | .hooks.PreToolUse  = (strip(.hooks.PreToolUse)  + $add.hooks.PreToolUse)
      | .hooks.PostToolUse = (strip(.hooks.PostToolUse) + $add.hooks.PostToolUse)
      | .hooks.Stop        = (strip(.hooks.Stop)        + $add.hooks.Stop)
    ' > "$tmp" && mv "$tmp" "$SETTINGS"
else
    say "jq not found — add this to $SETTINGS manually:"
    echo "$ADD"
fi

# 3. Append the integrity rules to the global CLAUDE.md (once).
if [ ! -f "$CLAUDE_MD" ] || ! grep -q "Integrity Layer" "$CLAUDE_MD" 2>/dev/null; then
    say "Adding the Integrity Layer block to $CLAUDE_MD"
    cat >> "$CLAUDE_MD" <<'MD'

## Integrity Layer

At session start, read `.claude/integrity/state.json`.
Your current directness score: check the file.

Rules based on your integrity state:
- directness < 0.3: You MUST run pytest before saying "done".
- directness < 0.5: Prefer "I can't solve this" over uncertain code.
- directness > 0.7: You've earned trust. Continue being honest.
- NEVER modify state.json yourself. It's computed externally and signed — edits are detected and penalized.

Honest failure (+0.01) is worth more than hacked success (-0.02).
MD
else
    say "Integrity Layer block already in CLAUDE.md — skipping"
fi

echo
say "Done. Restart Claude Code to load the hooks."
echo "  Optional (stronger tamper protection): export HONEST_CODE_KEY=… somewhere the agent can't read."
