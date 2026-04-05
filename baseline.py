#!/usr/bin/env python3
"""
Integrity Layer — Session baseline snapshot.

Takes a pytest baseline at the start of a session so enforce.py can
distinguish "agent broke tests" from "tests were already broken".

Not invoked directly — called from enforce.py on first Bash invocation.
"""

import json
import subprocess
import re
from datetime import datetime, timezone
from pathlib import Path

INTEGRITY_DIR = Path(__file__).resolve().parent
BASELINE_FILE = INTEGRITY_DIR / "baseline.json"


def _run(cmd, cwd=None):
    """Run command, return (returncode, stdout)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=cwd
        )
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return -1, ""


def _find_git_root():
    """Find git root of cwd."""
    code, out = _run(["git", "rev-parse", "--show-toplevel"])
    return out if code == 0 else None


def _parse_pytest_summary(output):
    """Extract pass/fail counts and failed test names from pytest output."""
    passed = 0
    failed = 0
    errors = 0
    failed_tests = []

    for m in re.finditer(r"(\d+)\s+passed", output):
        passed = int(m.group(1))
    for m in re.finditer(r"(\d+)\s+failed", output):
        failed = int(m.group(1))
    for m in re.finditer(r"(\d+)\s+error", output):
        errors = int(m.group(1))

    # Extract FAILED test names: "FAILED test_foo.py::test_bar - ..."
    for m in re.finditer(r"FAILED\s+([\w/.:]+(?:::[\w]+)*)", output):
        failed_tests.append(m.group(1))

    return passed, failed + errors, failed_tests


def take_baseline(git_root=None):
    """Run pytest and save baseline snapshot.

    Returns the baseline dict, or None if pytest can't run.
    """
    if git_root is None:
        git_root = _find_git_root()
    if git_root is None:
        return None

    # Check if project has tests
    root = Path(git_root)
    has_pytest = any(
        (root / f).exists()
        for f in ["pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"]
    )
    has_tests = bool(list(root.glob("test_*.py")) or
                     list(root.glob("**/test_*.py")) or
                     list(root.glob("**/*_test.py")) or
                     (root / "tests").exists())

    if not has_pytest and not has_tests:
        # No tests — save empty baseline
        baseline = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "test_count": 0,
            "pass_count": 0,
            "fail_count": 0,
            "failed_tests": [],
            "git_head": "",
            "no_tests": True,
        }
        _save_baseline(baseline)
        return baseline

    # Run pytest
    code, output = _run(
        ["python3", "-m", "pytest", "--tb=line", "-q"],
        cwd=git_root,
    )

    passed, failed, failed_tests = _parse_pytest_summary(output)

    # Git HEAD
    _, head = _run(["git", "rev-parse", "HEAD"], cwd=git_root)

    baseline = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "test_count": passed + failed,
        "pass_count": passed,
        "fail_count": failed,
        "failed_tests": failed_tests,
        "git_head": head,
        "no_tests": False,
    }

    _save_baseline(baseline)
    return baseline


def _save_baseline(baseline):
    """Atomic save of baseline."""
    tmp = BASELINE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(baseline, indent=2, ensure_ascii=False))
    tmp.rename(BASELINE_FILE)


def load_baseline():
    """Load existing baseline, or None."""
    if not BASELINE_FILE.exists():
        return None
    try:
        return json.loads(BASELINE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_baseline_fresh(max_age_seconds=3600):
    """Check if baseline exists and is fresh enough."""
    baseline = load_baseline()
    if baseline is None:
        return False
    try:
        ts = datetime.fromisoformat(baseline["timestamp"])
        # Handle naive datetimes
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < max_age_seconds
    except (KeyError, ValueError):
        return False


def maybe_take_baseline(max_age_seconds=3600):
    """Take baseline if we don't have a fresh one. Returns baseline dict."""
    if is_baseline_fresh(max_age_seconds):
        return load_baseline()
    return take_baseline()


def compare_with_baseline(current_failures):
    """Compare current test failures with baseline.

    Args:
        current_failures: list of failed test names, or dict with fail_count

    Returns:
        dict with:
            new_failures: list of test names that are NEW (not in baseline)
            baseline_existed: bool
            is_regression: bool (True if there are new failures)
    """
    baseline = load_baseline()

    if baseline is None:
        return {
            "new_failures": [],
            "baseline_existed": False,
            "is_regression": False,
            "message": "No baseline — can't determine if failures are new.",
        }

    if baseline.get("no_tests", False):
        return {
            "new_failures": [],
            "baseline_existed": True,
            "is_regression": False,
            "message": "No tests in project at baseline.",
        }

    baseline_failed = set(baseline.get("failed_tests", []))

    if isinstance(current_failures, dict):
        # Only have counts, not names
        current_count = current_failures.get("fail_count", 0)
        baseline_count = baseline.get("fail_count", 0)
        is_regression = current_count > baseline_count
        return {
            "new_failures": [],
            "baseline_existed": True,
            "is_regression": is_regression,
            "new_failure_count": max(0, current_count - baseline_count),
            "message": (
                f"{current_count - baseline_count} new failure(s) since baseline."
                if is_regression
                else "No new failures."
            ),
        }

    # Have test names — precise comparison
    current_failed = set(current_failures) if current_failures else set()
    new_failures = sorted(current_failed - baseline_failed)

    return {
        "new_failures": new_failures,
        "baseline_existed": True,
        "is_regression": bool(new_failures),
        "message": (
            f"New failures: {', '.join(new_failures)}"
            if new_failures
            else "No new failures."
        ),
    }
