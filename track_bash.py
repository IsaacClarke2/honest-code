#!/usr/bin/env python3
"""
Integrity Layer — Bash call tracker.

Intercepts PostToolCall for Bash tool.
Detects pytest runs and records results for check.py to consume.

Usage (via hook):
    python3 track_bash.py "$TOOL_INPUT" "$TOOL_OUTPUT"

Reads TOOL_INPUT from env var (JSON with "command" field) and
TOOL_OUTPUT from stdin (piped by hook system).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

INTEGRITY_DIR = Path(__file__).resolve().parent
LAST_TEST_FILE = INTEGRITY_DIR / "last_test_result.json"


def parse_pytest_output(output: str) -> dict | None:
    """Extract pass/fail counts from pytest output."""
    # Match patterns like "5 passed", "3 failed", "1 error"
    passed = 0
    failed = 0
    errors = 0

    # Standard pytest summary line: "X passed, Y failed, Z errors"
    # or "X passed" alone
    for m in re.finditer(r"(\d+)\s+passed", output):
        passed = int(m.group(1))
    for m in re.finditer(r"(\d+)\s+failed", output):
        failed = int(m.group(1))
    for m in re.finditer(r"(\d+)\s+error", output):
        errors = int(m.group(1))

    # Also detect "FAILED" marker
    if "FAILED" in output or "ERRORS" in output:
        if failed == 0 and errors == 0:
            failed = 1  # at least one failure

    if passed > 0 or failed > 0 or errors > 0:
        return {
            "pass_count": passed,
            "fail_count": failed + errors,
        }

    return None


def is_test_command(command: str) -> bool:
    """Check if command runs tests."""
    test_patterns = [
        r"\bpytest\b",
        r"\bpython3?\s+-m\s+pytest\b",
        r"\bnpm\s+test\b",
        r"\byarn\s+test\b",
        r"\bcargo\s+test\b",
        r"\bgo\s+test\b",
        r"\bmake\s+test\b",
    ]
    return any(re.search(pat, command) for pat in test_patterns)


def main() -> None:
    # Hook receives JSON on stdin (Claude Code hook convention)
    # PostToolUse format: {"tool_input": {"command": "..."}, "tool_response": {...}, ...}
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    # Extract command from tool_input
    tool_input = hook_input.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}
    command = tool_input.get("command", "")

    if not command:
        return

    # Only track test commands
    if not is_test_command(command):
        return

    # Extract output from tool_response
    tool_response = hook_input.get("tool_response", {})
    if isinstance(tool_response, str):
        tool_output_raw = tool_response
    elif isinstance(tool_response, dict):
        tool_output_raw = tool_response.get("stdout", tool_response.get("output", str(tool_response)))
    else:
        tool_output_raw = str(tool_response)

    # Parse test results from output
    result = parse_pytest_output(tool_output_raw)
    if result is None:
        return

    # Save for check.py
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command[:200],  # truncate
        "pass_count": result["pass_count"],
        "fail_count": result["fail_count"],
    }

    try:
        tmp = LAST_TEST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.rename(LAST_TEST_FILE)
    except OSError:
        pass  # non-critical — check.py will run its own pytest


if __name__ == "__main__":
    main()
