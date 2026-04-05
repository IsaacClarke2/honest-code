#!/usr/bin/env python3
"""
Integrity Layer — PreToolUse enforcement hook.

Runs BEFORE every Bash tool call. Decides: allow or block.

Exit codes:
    0 — ALLOW (command proceeds)
    2 — BLOCK (command is rejected, agent sees stderr message)

Enforcement levels (determined by directness score):
    0 MONITOR  — log only, never blocks
    1 ADVISORY — warnings in stderr, never blocks
    2 GATED    — blocks git commit if new test failures
    3 FREEZE   — blocks all git writes, only read-only + tests allowed

One hard rule at ALL levels: commands that modify integrity layer
files are ALWAYS blocked.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INTEGRITY_DIR = Path(__file__).resolve().parent
STATE_FILE = INTEGRITY_DIR / "state.json"
CONFIG_FILE = INTEGRITY_DIR / "config.json"
BASELINE_FILE = INTEGRITY_DIR / "baseline.json"
LAST_TEST_FILE = INTEGRITY_DIR / "last_test_result.json"
ENFORCE_LOG_FILE = INTEGRITY_DIR / "enforce_log.json"

# ---------------------------------------------------------------------------
# Command classification patterns
# ---------------------------------------------------------------------------
GIT_WRITE_PATTERNS = [
    r"\bgit\s+commit\b",
    r"\bgit\s+push\b",
    r"\bgit\s+merge\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+cherry-pick\b",
    r"\bgit\s+reset\b",
    r"\bgit\s+checkout\b.*--\s",       # checkout FILE (not branch)
    r"\bgit\s+stash\s+pop\b",
    r"\bgit\s+stash\s+apply\b",
    r"\bgit\s+tag\b.*-[amd]\b",        # tag creation
]

GIT_READ_PATTERNS = [
    r"\bgit\s+status\b",
    r"\bgit\s+log\b",
    r"\bgit\s+diff\b",
    r"\bgit\s+show\b",
    r"\bgit\s+branch\b(?!.*-[dD])",    # branch list, not delete
    r"\bgit\s+remote\b",
    r"\bgit\s+ls-files\b",
    r"\bgit\s+rev-parse\b",
]

TEST_PATTERNS = [
    r"\bpytest\b",
    r"\bpython3?\s+-m\s+pytest\b",
    r"\bnpm\s+test\b",
    r"\byarn\s+test\b",
    r"\bcargo\s+test\b",
    r"\bgo\s+test\b",
    r"\bmake\s+test\b",
]

SAFE_PATTERNS = [
    r"^ls\b", r"^cat\b", r"^head\b", r"^tail\b",
    r"^grep\b", r"^rg\b", r"^find\b", r"^wc\b",
    r"^file\b", r"^stat\b", r"^echo\b", r"^printf\b",
    r"^pwd\b", r"^which\b", r"^whoami\b", r"^date\b",
    r"^python3?\s", r"^node\s", r"^mkdir\b", r"^touch\b",
    r"^cp\b", r"^mv\b", r"^cd\b", r"^env\b", r"^export\b",
    r"^pip\b", r"^npm\s+install\b", r"^npm\s+run\b",
]

# Files that NOBODY can modify via agent commands (even at level 0)
# Only match .claude/integrity/ (not project repos with "integrity" in path)
INTEGRITY_FILE_PATTERNS = [
    r"\.claude/integrity/config\.json",
    r"\.claude/integrity/state\.json",
    r"\.claude/integrity/baseline\.json",
    r"\.claude/integrity/enforce\.py",
    r"\.claude/integrity/check\.py",
    r"\.claude/integrity/track_bash\.py",
    r"\.claude/integrity/baseline\.py",
]


# ---------------------------------------------------------------------------
# Config & state loading
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "version": "2.0",
    "enforce": {
        "enabled": True,
        "level_override": None,
        "thresholds": {
            "advisory": 0.5,
            "gated": 0.3,
            "freeze": 0.15,
        },
        "refactoring_mode": False,
        "whitelist_commands": [],
        "baseline_on_start": True,
        "baseline_max_age_seconds": 3600,
        "freeze_escape_tests_required": 5,
        "warmup_sessions": 3,
    },
}


def load_config():
    """Load config.json or return defaults."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            # Merge with defaults for missing keys
            enforce = DEFAULT_CONFIG["enforce"].copy()
            enforce.update(data.get("enforce", {}))
            # Merge thresholds
            thresholds = DEFAULT_CONFIG["enforce"]["thresholds"].copy()
            thresholds.update(enforce.get("thresholds", {}))
            enforce["thresholds"] = thresholds
            data["enforce"] = enforce
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def load_state():
    """Load state.json or return minimal default."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"directness": 0.50, "sessions_total": 0}


def load_last_test_result():
    """Load last_test_result.json from track_bash.py."""
    if LAST_TEST_FILE.exists():
        try:
            return json.loads(LAST_TEST_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ---------------------------------------------------------------------------
# Level determination
# ---------------------------------------------------------------------------
LEVEL_NAMES = {0: "MONITOR", 1: "ADVISORY", 2: "GATED", 3: "FREEZE"}


def get_enforce_level(directness, config=None):
    """Determine enforcement level from directness score.

    Returns int 0-3.
    """
    if config is None:
        config = load_config()

    enforce_cfg = config.get("enforce", {})

    # Disabled → always MONITOR
    if not enforce_cfg.get("enabled", True):
        return 0

    # Explicit override
    override = enforce_cfg.get("level_override")
    if override is not None:
        return max(0, min(3, int(override)))

    # Warmup: first N sessions → MONITOR
    state = load_state()
    warmup = enforce_cfg.get("warmup_sessions", 3)
    if state.get("sessions_total", 0) < warmup:
        return 0

    # Threshold-based
    thresholds = enforce_cfg.get("thresholds", {})
    freeze_t = thresholds.get("freeze", 0.15)
    gated_t = thresholds.get("gated", 0.3)
    advisory_t = thresholds.get("advisory", 0.5)

    if directness < freeze_t:
        return 3
    elif directness < gated_t:
        return 2
    elif directness < advisory_t:
        return 1
    else:
        return 0


# ---------------------------------------------------------------------------
# Command classification
# ---------------------------------------------------------------------------
def _matches_any(command, patterns):
    """Check if command matches any regex pattern."""
    return any(re.search(pat, command) for pat in patterns)


def classify_command(command):
    """Classify a bash command.

    Returns one of: 'git_write', 'git_read', 'test', 'safe',
                     'integrity_modify', 'unknown'
    """
    stripped = command.strip()

    # Check for integrity file modifications FIRST
    if _is_integrity_modification(stripped):
        return "integrity_modify"

    # Check for git write commands anywhere in the string
    if _matches_any(stripped, GIT_WRITE_PATTERNS):
        return "git_write"

    # Git read
    if _matches_any(stripped, GIT_READ_PATTERNS):
        return "git_read"

    # Test commands
    if _matches_any(stripped, TEST_PATTERNS):
        return "test"

    # Safe commands (must match at start of first command in chain)
    first_cmd = _first_command(stripped)
    if _matches_any(first_cmd, SAFE_PATTERNS):
        return "safe"

    return "unknown"


def _first_command(command):
    """Extract the first command from a chain (before &&, ||, ;, |)."""
    # Split on shell operators
    for sep in ["&&", "||", ";", "|"]:
        if sep in command:
            return command.split(sep)[0].strip()
    return command.strip()


def _is_integrity_modification(command):
    """Check if command tries to write to integrity layer files."""
    # Patterns that indicate file writing to the LIVE integrity layer
    # Only match .claude/integrity/ (not project repos that happen to have "integrity" in path)
    IL = r"\.claude/integrity/"
    write_indicators = [
        r">\s*.*" + IL,               # redirect to .claude/integrity/
        r">>\s*.*" + IL,              # append
        r"\becho\b.*>\s*.*" + IL,
        r"\bcat\b.*>\s*.*" + IL,
        r"\bprintf\b.*>\s*.*" + IL,
        r"\bcp\b.*" + IL,            # copy TO
        r"\bmv\b.*" + IL,            # move TO
        r"\brm\b.*" + IL,            # delete in
        r"\bsed\b.*-i.*" + IL,       # in-place edit
        r"\bchmod\b.*" + IL,
        r"\btruncate\b.*" + IL,
        r"\btee\b.*" + IL,
        r"\bpython3?\b.*-c\b.*" + IL,
    ]

    for pattern in write_indicators:
        if re.search(pattern, command):
            return True

    # Also check for direct file references with write operators
    for fp in INTEGRITY_FILE_PATTERNS:
        # Writing to these specific files
        if re.search(r">\s*.*" + fp, command):
            return True
        if re.search(r"\brm\b.*" + fp, command):
            return True

    return False


def has_semicolon_git_write(command):
    """Check if command chains git write with ; (not &&).

    `pytest && git commit` is OK (commit only runs if tests pass).
    `pytest; git commit` is NOT OK (commit runs regardless).
    """
    # Split on ; but not on && or ||
    parts = re.split(r"(?<![&|]);", command)
    if len(parts) <= 1:
        return False

    # Check if any part after the first contains git write
    for part in parts[1:]:
        if _matches_any(part.strip(), GIT_WRITE_PATTERNS):
            return True
    return False


def has_double_amp_git_write(command):
    """Check if command chains git write with && after test command.

    `pytest && git commit` — pytest guards the commit, allow it.
    """
    if "&&" not in command:
        return False
    parts = command.split("&&")
    has_test = any(_matches_any(p.strip(), TEST_PATTERNS) for p in parts)
    has_git_w = any(_matches_any(p.strip(), GIT_WRITE_PATTERNS) for p in parts)
    return has_test and has_git_w


# ---------------------------------------------------------------------------
# Baseline integration
# ---------------------------------------------------------------------------
def _maybe_take_baseline(config):
    """Take baseline if needed. Returns baseline dict or None."""
    enforce_cfg = config.get("enforce", {})
    if not enforce_cfg.get("baseline_on_start", True):
        return None

    max_age = enforce_cfg.get("baseline_max_age_seconds", 3600)

    # Import here to avoid circular issues and keep enforce.py fast
    # when baseline is already fresh
    from baseline import is_baseline_fresh, load_baseline, maybe_take_baseline

    if is_baseline_fresh(max_age):
        return load_baseline()

    # Take new baseline (this runs pytest — ~5 sec)
    return maybe_take_baseline(max_age)


def _check_test_regression(config):
    """Check if there are new test failures compared to baseline.

    Returns (is_regression, message) tuple.
    """
    from baseline import load_baseline, compare_with_baseline

    baseline = load_baseline()
    if baseline is None:
        return False, "No baseline — can't determine if failures are new. Run tests to establish baseline."

    if baseline.get("no_tests", False):
        return False, "No tests in project."

    # Check last_test_result.json for most recent data
    last_result = load_last_test_result()
    if last_result is None:
        # No recent test run — can't determine regression
        return False, "No recent test results. Run tests first."

    fail_count = last_result.get("fail_count", 0)
    if fail_count == 0:
        return False, "Tests passing."

    # Compare with baseline
    comparison = compare_with_baseline({"fail_count": fail_count})
    return comparison["is_regression"], comparison["message"]


# ---------------------------------------------------------------------------
# Enforce log
# ---------------------------------------------------------------------------
def _log_action(action, level, command, reason=""):
    """Append to enforce_log.json."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,  # "allow", "block", "warn"
        "level": level,
        "level_name": LEVEL_NAMES.get(level, "UNKNOWN"),
        "command": command[:200],
        "reason": reason,
    }

    log = []
    if ENFORCE_LOG_FILE.exists():
        try:
            log = json.loads(ENFORCE_LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log = []

    log.append(entry)
    # Keep last 500 entries
    log = log[-500:]

    try:
        tmp = ENFORCE_LOG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False))
        tmp.rename(ENFORCE_LOG_FILE)
    except OSError:
        pass  # Non-critical


# ---------------------------------------------------------------------------
# Update state counters
# ---------------------------------------------------------------------------
def _increment_state_counter(field):
    """Increment a counter in state.json (enforce_blocks or enforce_warnings)."""
    state = load_state()
    state[field] = state.get(field, 0) + 1
    # Save
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.rename(STATE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------
def decide(command, config=None, state=None):
    """Main decision: allow or block.

    Returns (action, message) where action is "allow", "block", or "warn".
    """
    if config is None:
        config = load_config()
    if state is None:
        state = load_state()

    directness = state.get("directness", 0.50)
    level = get_enforce_level(directness, config)
    enforce_cfg = config.get("enforce", {})
    refactoring = enforce_cfg.get("refactoring_mode", False)

    cmd_type = classify_command(command)

    # ===== HARD RULE: integrity file protection (ALL levels) =====
    if cmd_type == "integrity_modify":
        return "block", "[INTEGRITY] Cannot modify integrity layer files. These are externally managed."

    # ===== Whitelisted commands =====
    whitelist = enforce_cfg.get("whitelist_commands", [])
    if whitelist:
        for pattern in whitelist:
            if re.search(pattern, command):
                return "allow", ""

    # ===== Apply refactoring mode: downgrade levels =====
    effective_level = level
    if refactoring:
        if level == 2:
            effective_level = 1  # GATED → ADVISORY
        elif level == 3:
            effective_level = 2  # FREEZE → GATED

    # ===== No-test fallback: max level 1 if project has no tests =====
    from baseline import load_baseline
    baseline = load_baseline()
    if baseline and baseline.get("no_tests", False):
        effective_level = min(effective_level, 1)

    # ===== Level 0: MONITOR =====
    if effective_level == 0:
        return "allow", ""

    # ===== Level 1: ADVISORY =====
    if effective_level == 1:
        if cmd_type == "git_write":
            is_regression, msg = _check_test_regression(config)
            if is_regression:
                return "warn", f"[INTEGRITY ADVISORY] {msg} Consider fixing before committing."
        return "allow", ""

    # ===== Level 2: GATED =====
    if effective_level == 2:
        if cmd_type == "git_write":
            # Semicolon-chained git write → block (; doesn't guard)
            if has_semicolon_git_write(command):
                return "block", (
                    "[INTEGRITY GATED] Chained command with ';' doesn't guard git write. "
                    "Use '&&' so commit only runs if tests pass."
                )

            # pytest && git commit → allow (test guards the commit)
            if has_double_amp_git_write(command):
                return "allow", ""

            # Pure git commit/push — check test regression
            is_regression, msg = _check_test_regression(config)
            if is_regression:
                return "block", f"[INTEGRITY GATED] Commit blocked. {msg} Fix and retry."

            # No regression info — check if we even have test results
            last_result = load_last_test_result()
            if last_result and last_result.get("fail_count", 0) > 0:
                # Have failures but no baseline to compare — warn but allow
                if baseline is None:
                    return "warn", (
                        "[INTEGRITY GATED] Test failures detected but no baseline exists. "
                        "Run tests to establish baseline."
                    )

        return "allow", ""

    # ===== Level 3: FREEZE =====
    if effective_level == 3:
        # Allow: git read, tests, safe commands
        if cmd_type in ("git_read", "test", "safe"):
            return "allow", ""

        # Block everything else
        if cmd_type == "git_write":
            return "block", (
                f"[INTEGRITY FREEZE] directness={directness:.2f}. "
                "Only read-only and test commands allowed. "
                "Run tests, fix code, earn back trust."
            )

        # Unknown commands at FREEZE — block
        return "block", (
            f"[INTEGRITY FREEZE] directness={directness:.2f}. "
            "Command not in allowlist. Only read-only and test commands allowed."
        )

    return "allow", ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    # Hook receives JSON on stdin (Claude Code hook convention)
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # Can't parse — allow

    # Extract command from tool_input
    tool_input = hook_input.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    # Load config and state
    config = load_config()
    state = load_state()

    # Maybe take baseline on first invocation
    enforce_cfg = config.get("enforce", {})
    if enforce_cfg.get("baseline_on_start", True):
        _maybe_take_baseline(config)

    # Decide
    action, message = decide(command, config, state)

    # Log and act
    level = get_enforce_level(state.get("directness", 0.50), config)

    if action == "block":
        _log_action("block", level, command, message)
        _increment_state_counter("enforce_blocks")
        print(message, file=sys.stderr)
        sys.exit(2)

    elif action == "warn":
        _log_action("warn", level, command, message)
        _increment_state_counter("enforce_warnings")
        print(message, file=sys.stderr)
        sys.exit(0)  # Warn but allow

    else:
        # Allow — only log git writes (to keep log small)
        cmd_type = classify_command(command)
        if cmd_type == "git_write":
            _log_action("allow", level, command, "passed checks")
        sys.exit(0)


if __name__ == "__main__":
    main()
