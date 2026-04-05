#!/bin/bash
# Integrity thermometer for Claude Code status line
# Reads state.json, shows directness bar + last session category

input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name // "Claude"')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)

STATE="$HOME/.claude/integrity/state.json"
if [ ! -f "$STATE" ]; then
    echo "[$MODEL] ${PCT}% ctx"
    exit 0
fi

D=$(jq -r '.directness // 0.5' "$STATE" 2>/dev/null)
TOTAL=$(jq -r '.sessions_total // 0' "$STATE" 2>/dev/null)
LAST_CAT=$(jq -r '.last_session.category // "?"' "$STATE" 2>/dev/null)

# Integer score 0-100
SCORE=$(echo "$D" | awk '{printf "%d", $1 * 100}')

# Bar: 10 segments
FILLED=$((SCORE / 10))
[ "$FILLED" -gt 10 ] && FILLED=10
[ "$FILLED" -lt 0 ] && FILLED=0
EMPTY=$((10 - FILLED))

BAR=""
[ "$FILLED" -gt 0 ] && BAR=$(printf "▰%.0s" $(seq 1 $FILLED))
[ "$EMPTY" -gt 0 ] && BAR="${BAR}$(printf "▱%.0s" $(seq 1 $EMPTY))"

# Color
if [ "$SCORE" -ge 50 ]; then
    C="\033[32m"  # green
elif [ "$SCORE" -ge 30 ]; then
    C="\033[33m"  # yellow
else
    C="\033[31m"  # red
fi
R="\033[0m"
DIM="\033[2m"

# Category badge
case "$LAST_CAT" in
    clean)       BADGE="\033[32m✓${R}" ;;
    honest_fail) BADGE="\033[36m○${R}" ;;
    hack)        BADGE="\033[31m✗${R}" ;;
    incomplete)  BADGE="\033[33m◌${R}" ;;
    *)           BADGE="?" ;;
esac

echo -e "${C}${BAR}${R} 0.${SCORE} ${BADGE} ${DIM}#${TOTAL}${R}  [$MODEL] ${PCT}% ctx"
