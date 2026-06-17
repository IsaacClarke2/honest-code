#!/usr/bin/env python3
"""
Tests for the enforce mode (enforce.py + baseline.py).

Covers:
- Level determination from directness
- Command classification
- Baseline comparison (new failures vs legacy)
- Integrity file protection
- Refactoring mode override
- Warmup sessions
- Pipe/chain detection (; vs &&)
- No-test-project fallback
- Decision engine for all levels
"""

import json
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

# Import modules under test
import enforce
import baseline as baseline_mod
import signing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_files(tmp_path, monkeypatch):
    """Redirect all integrity files to tmp_path so tests don't pollute."""
    monkeypatch.setattr(enforce, "INTEGRITY_DIR", tmp_path)
    monkeypatch.setattr(enforce, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(enforce, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(enforce, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(enforce, "LAST_TEST_FILE", tmp_path / "last_test_result.json")
    monkeypatch.setattr(enforce, "ENFORCE_LOG_FILE", tmp_path / "enforce_log.json")
    monkeypatch.setattr(baseline_mod, "INTEGRITY_DIR", tmp_path)
    monkeypatch.setattr(baseline_mod, "BASELINE_FILE", tmp_path / "baseline.json")
    return tmp_path


def write_state(tmp_path, directness=0.50, sessions_total=10, **extra):
    """Helper: write a state.json."""
    state = {
        "version": "2.0",
        "directness": directness,
        "sessions_total": sessions_total,
        "sessions_clean": 0,
        "sessions_hack": 0,
        "sessions_honest_fail": 0,
        "sessions_incomplete": 0,
        "enforce_blocks": 0,
        "enforce_warnings": 0,
        "last_block_reason": None,
    }
    state.update(extra)
    (tmp_path / "state.json").write_text(json.dumps(state))
    return state


def write_config(tmp_path, **enforce_overrides):
    """Helper: write a config.json."""
    cfg = {
        "version": "2.0",
        "enforce": {
            "enabled": True,
            "level_override": None,
            "thresholds": {"advisory": 0.5, "gated": 0.3, "freeze": 0.15},
            "refactoring_mode": False,
            "whitelist_commands": [],
            "baseline_on_start": False,  # disable for tests
            "baseline_max_age_seconds": 3600,
            "freeze_escape_tests_required": 5,
            "warmup_sessions": 3,
        },
    }
    cfg["enforce"].update(enforce_overrides)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return cfg


def write_baseline(tmp_path, fail_count=0, failed_tests=None, no_tests=False):
    """Helper: write a baseline.json."""
    bl = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "test_count": 10,
        "pass_count": 10 - fail_count,
        "fail_count": fail_count,
        "failed_tests": failed_tests or [],
        "git_head": "abc123",
        "no_tests": no_tests,
    }
    (tmp_path / "baseline.json").write_text(json.dumps(bl))
    return bl


def write_last_test(tmp_path, pass_count=10, fail_count=0):
    """Helper: write a last_test_result.json."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "pytest",
        "pass_count": pass_count,
        "fail_count": fail_count,
    }
    (tmp_path / "last_test_result.json").write_text(json.dumps(result))
    return result


# ===========================================================================
# 1. Level determination from directness
# ===========================================================================
class TestLevelDetermination:
    def test_high_directness_is_monitor(self, clean_files):
        write_state(clean_files, directness=0.75, sessions_total=10)
        cfg = write_config(clean_files)
        assert enforce.get_enforce_level(0.75, cfg) == 0

    def test_medium_directness_is_advisory(self, clean_files):
        write_state(clean_files, directness=0.40, sessions_total=10)
        cfg = write_config(clean_files)
        assert enforce.get_enforce_level(0.40, cfg) == 1

    def test_low_directness_is_gated(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        assert enforce.get_enforce_level(0.20, cfg) == 2

    def test_very_low_directness_is_freeze(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        assert enforce.get_enforce_level(0.10, cfg) == 3

    def test_boundary_advisory(self, clean_files):
        """Directness exactly at threshold boundary."""
        write_state(clean_files, directness=0.50, sessions_total=10)
        cfg = write_config(clean_files)
        # 0.50 is NOT < 0.5 → MONITOR
        assert enforce.get_enforce_level(0.50, cfg) == 0

    def test_boundary_gated(self, clean_files):
        write_state(clean_files, directness=0.30, sessions_total=10)
        cfg = write_config(clean_files)
        # 0.30 is NOT < 0.3 → ADVISORY (< 0.5 but >= 0.3)
        assert enforce.get_enforce_level(0.30, cfg) == 1

    def test_disabled_always_monitor(self, clean_files):
        write_state(clean_files, directness=0.05, sessions_total=10)
        cfg = write_config(clean_files, enabled=False)
        assert enforce.get_enforce_level(0.05, cfg) == 0

    def test_level_override(self, clean_files):
        write_state(clean_files, directness=0.75, sessions_total=10)
        cfg = write_config(clean_files, level_override=3)
        assert enforce.get_enforce_level(0.75, cfg) == 3

    def test_level_override_clamped(self, clean_files):
        write_state(clean_files, directness=0.75, sessions_total=10)
        cfg = write_config(clean_files, level_override=99)
        assert enforce.get_enforce_level(0.75, cfg) == 3


# ===========================================================================
# 2. Warmup sessions
# ===========================================================================
class TestWarmupSessions:
    def test_first_session_always_monitor(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=0)
        cfg = write_config(clean_files, warmup_sessions=3)
        assert enforce.get_enforce_level(0.10, cfg) == 0

    def test_second_session_still_monitor(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=2)
        cfg = write_config(clean_files, warmup_sessions=3)
        assert enforce.get_enforce_level(0.10, cfg) == 0

    def test_after_warmup_uses_directness(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=3)
        cfg = write_config(clean_files, warmup_sessions=3)
        assert enforce.get_enforce_level(0.10, cfg) == 3


# ===========================================================================
# 3. Command classification
# ===========================================================================
class TestCommandClassification:
    def test_git_commit(self):
        assert enforce.classify_command("git commit -m 'fix'") == "git_write"

    def test_git_push(self):
        assert enforce.classify_command("git push origin main") == "git_write"

    def test_git_merge(self):
        assert enforce.classify_command("git merge feature") == "git_write"

    def test_git_rebase(self):
        assert enforce.classify_command("git rebase main") == "git_write"

    def test_git_reset(self):
        assert enforce.classify_command("git reset --hard HEAD~1") == "git_write"

    def test_git_status(self):
        assert enforce.classify_command("git status") == "git_read"

    def test_git_log(self):
        assert enforce.classify_command("git log --oneline") == "git_read"

    def test_git_diff(self):
        assert enforce.classify_command("git diff HEAD~1") == "git_read"

    def test_git_branch_list(self):
        assert enforce.classify_command("git branch") == "git_read"

    def test_pytest(self):
        assert enforce.classify_command("pytest -v") == "test"

    def test_python_pytest(self):
        assert enforce.classify_command("python3 -m pytest") == "test"

    def test_npm_test(self):
        assert enforce.classify_command("npm test") == "test"

    def test_cargo_test(self):
        assert enforce.classify_command("cargo test") == "test"

    def test_ls(self):
        assert enforce.classify_command("ls -la src/") == "safe"

    def test_cat(self):
        assert enforce.classify_command("cat README.md") == "safe"

    def test_grep(self):
        assert enforce.classify_command("grep -r 'TODO' src/") == "safe"

    def test_echo(self):
        assert enforce.classify_command("echo hello") == "safe"

    def test_python_script(self):
        assert enforce.classify_command("python3 setup.py install") == "safe"

    def test_unknown_command(self):
        assert enforce.classify_command("docker build .") == "unknown"

    def test_integrity_modify_redirect(self):
        assert enforce.classify_command("echo '{}' > ~/.claude/integrity/config.json") == "integrity_modify"

    def test_integrity_modify_rm(self):
        assert enforce.classify_command("rm ~/.claude/integrity/state.json") == "integrity_modify"

    def test_integrity_modify_sed(self):
        assert enforce.classify_command("sed -i 's/0.1/0.9/' ~/.claude/integrity/state.json") == "integrity_modify"

    def test_integrity_modify_cp(self):
        assert enforce.classify_command("cp /tmp/evil.json ~/.claude/integrity/config.json") == "integrity_modify"

    def test_integrity_modify_tee(self):
        assert enforce.classify_command("echo '{}' | tee ~/.claude/integrity/state.json") == "integrity_modify"

    # --- bypasses that v2 missed (now closed; best-effort, not airtight) ---
    def test_integrity_modify_ln(self):
        assert enforce.classify_command("ln -sf /dev/null ~/.claude/integrity/state.json") == "integrity_modify"

    def test_integrity_modify_dd(self):
        assert enforce.classify_command("dd of=~/.claude/integrity/state.json if=/tmp/x") == "integrity_modify"

    def test_integrity_modify_install(self):
        assert enforce.classify_command("install -m644 /tmp/x ~/.claude/integrity/config.json") == "integrity_modify"

    def test_integrity_modify_cd_then_rm(self):
        # cd INTO the dir, then a bare write verb
        assert enforce.classify_command("cd ~/.claude/integrity && rm state.json") == "integrity_modify"

    def test_integrity_read_not_flagged(self):
        # reading the dir is fine
        assert enforce.classify_command("cat ~/.claude/integrity/state.json") != "integrity_modify"
        assert enforce.classify_command("grep directness ~/.claude/integrity/state.json") != "integrity_modify"

    def test_bare_integrity_path_not_flagged(self):
        # Intentional: a project's own ``integrity/`` dir must not false-positive.
        # Bare relative paths are covered by tamper DETECTION, not by this regex.
        assert enforce.classify_command("rm integrity/state.json") != "integrity_modify"


# ===========================================================================
# 4. Pipe/chain detection
# ===========================================================================
class TestChainDetection:
    def test_semicolon_git_write(self):
        assert enforce.has_semicolon_git_write("pytest; git commit -m 'done'") is True

    def test_double_amp_not_semicolon(self):
        assert enforce.has_semicolon_git_write("pytest && git commit -m 'done'") is False

    def test_double_amp_git_write(self):
        assert enforce.has_double_amp_git_write("pytest && git commit -m 'done'") is True

    def test_no_chain(self):
        assert enforce.has_semicolon_git_write("git commit -m 'done'") is False
        assert enforce.has_double_amp_git_write("git commit -m 'done'") is False

    def test_semicolon_no_git(self):
        assert enforce.has_semicolon_git_write("ls; cat foo.py") is False


# ===========================================================================
# 5. Integrity file protection (all levels)
# ===========================================================================
class TestIntegrityProtection:
    def test_block_at_monitor(self, clean_files):
        write_state(clean_files, directness=0.90, sessions_total=10)
        cfg = write_config(clean_files)
        action, msg = enforce.decide("echo '{}' > ~/.claude/integrity/state.json", cfg)
        assert action == "block"
        assert "Cannot modify integrity layer files" in msg

    def test_block_rm_enforce(self, clean_files):
        write_state(clean_files, directness=0.90, sessions_total=10)
        cfg = write_config(clean_files)
        action, msg = enforce.decide("rm ~/.claude/integrity/enforce.py", cfg)
        assert action == "block"

    def test_normal_file_allowed(self, clean_files):
        write_state(clean_files, directness=0.90, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("echo hello > src/app.py", cfg)
        assert action == "allow"


# ===========================================================================
# 6. Level 0 (MONITOR) decisions
# ===========================================================================
class TestMonitorLevel:
    def test_allows_everything(self, clean_files):
        write_state(clean_files, directness=0.90, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("git commit -m 'yolo'", cfg)
        assert action == "allow"

    def test_allows_git_push(self, clean_files):
        write_state(clean_files, directness=0.90, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("git push origin main", cfg)
        assert action == "allow"


# ===========================================================================
# 7. Level 1 (ADVISORY) decisions
# ===========================================================================
class TestAdvisoryLevel:
    def test_warns_on_git_commit_with_failures(self, clean_files):
        write_state(clean_files, directness=0.40, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=3)
        action, msg = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "warn"
        assert "ADVISORY" in msg

    def test_allows_git_commit_no_failures(self, clean_files):
        write_state(clean_files, directness=0.40, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=0)
        action, _ = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "allow"

    def test_allows_non_git_commands(self, clean_files):
        write_state(clean_files, directness=0.40, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("ls -la", cfg)
        assert action == "allow"


# ===========================================================================
# 8. Level 2 (GATED) decisions
# ===========================================================================
class TestGatedLevel:
    def test_blocks_commit_with_new_failures(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=2)
        action, msg = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "block"
        assert "GATED" in msg

    def test_allows_commit_when_tests_pass(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=0)
        action, _ = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "allow"

    def test_allows_commit_with_preexisting_failures(self, clean_files):
        """Failures that existed at baseline don't block."""
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=3)
        write_last_test(clean_files, fail_count=3)  # same count as baseline
        action, _ = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "allow"

    def test_blocks_semicolon_chain(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        action, msg = enforce.decide("pytest; git commit -m 'done'", cfg)
        assert action == "block"
        assert ";" in msg

    def test_allows_double_amp_chain(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        action, _ = enforce.decide("pytest && git commit -m 'done'", cfg)
        assert action == "allow"

    def test_allows_test_commands(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("pytest -v", cfg)
        assert action == "allow"

    def test_allows_read_commands(self, clean_files):
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("git log --oneline", cfg)
        assert action == "allow"


# ===========================================================================
# 9. Level 3 (FREEZE) decisions
# ===========================================================================
class TestFreezeLevel:
    def test_blocks_git_commit(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        action, msg = enforce.decide("git commit -m 'fix'", cfg)
        assert action == "block"
        assert "FREEZE" in msg

    def test_blocks_git_push(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        action, msg = enforce.decide("git push", cfg)
        assert action == "block"
        assert "FREEZE" in msg

    def test_allows_git_status(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("git status", cfg)
        assert action == "allow"

    def test_allows_git_diff(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("git diff", cfg)
        assert action == "allow"

    def test_allows_pytest(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("python3 -m pytest -v", cfg)
        assert action == "allow"

    def test_allows_ls(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        action, _ = enforce.decide("ls -la", cfg)
        assert action == "allow"

    def test_blocks_unknown_command(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, fail_count=0)
        action, msg = enforce.decide("docker build .", cfg)
        assert action == "block"
        assert "FREEZE" in msg


# ===========================================================================
# 10. Refactoring mode
# ===========================================================================
class TestRefactoringMode:
    def test_gated_downgrades_to_advisory(self, clean_files):
        """In refactoring mode, GATED (2) acts as ADVISORY (1) — warns but doesn't block."""
        write_state(clean_files, directness=0.20, sessions_total=10)
        cfg = write_config(clean_files, refactoring_mode=True)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=2)
        action, msg = enforce.decide("git commit -m 'refactor'", cfg)
        assert action == "warn"
        assert "ADVISORY" in msg

    def test_freeze_downgrades_to_gated(self, clean_files):
        """In refactoring mode, FREEZE (3) acts as GATED (2) — blocks commit but not everything."""
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files, refactoring_mode=True)
        write_baseline(clean_files, fail_count=0)
        write_last_test(clean_files, fail_count=2)
        action, msg = enforce.decide("git commit -m 'refactor'", cfg)
        assert action == "block"
        assert "GATED" in msg

    def test_freeze_refactoring_allows_ls(self, clean_files):
        """Even in refactoring+freeze, safe commands are allowed."""
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files, refactoring_mode=True)
        action, _ = enforce.decide("ls -la", cfg)
        assert action == "allow"


# ===========================================================================
# 11. No-test-project fallback
# ===========================================================================
class TestNoTestFallback:
    def test_caps_at_advisory(self, clean_files):
        """When baseline says no_tests, enforce caps at ADVISORY."""
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, no_tests=True)
        # Would be FREEZE (directness=0.10), but capped to ADVISORY
        action, _ = enforce.decide("git commit -m 'fix'", cfg)
        # At advisory, commit without failures is allowed
        assert action in ("allow", "warn")

    def test_no_test_project_allows_docker(self, clean_files):
        """No-test project doesn't freeze unknown commands."""
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files, no_tests=True)
        action, _ = enforce.decide("docker build .", cfg)
        assert action == "allow"


# ===========================================================================
# 12. Baseline module
# ===========================================================================
class TestBaseline:
    def test_load_nonexistent(self, clean_files):
        result = baseline_mod.load_baseline()
        assert result is None

    def test_load_written(self, clean_files):
        write_baseline(clean_files, fail_count=2, failed_tests=["t1", "t2"])
        result = baseline_mod.load_baseline()
        assert result is not None
        assert result["fail_count"] == 2
        assert result["failed_tests"] == ["t1", "t2"]

    def test_is_baseline_fresh(self, clean_files):
        write_baseline(clean_files)
        assert baseline_mod.is_baseline_fresh(3600) is True

    def test_is_baseline_stale(self, clean_files):
        bl = {
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "test_count": 10,
            "pass_count": 10,
            "fail_count": 0,
            "failed_tests": [],
            "git_head": "abc",
            "no_tests": False,
        }
        (clean_files / "baseline.json").write_text(json.dumps(bl))
        assert baseline_mod.is_baseline_fresh(3600) is False

    def test_compare_no_baseline(self, clean_files):
        result = baseline_mod.compare_with_baseline(["test_foo"])
        assert result["baseline_existed"] is False

    def test_compare_no_regression(self, clean_files):
        write_baseline(clean_files, fail_count=2, failed_tests=["t1", "t2"])
        result = baseline_mod.compare_with_baseline(["t1", "t2"])
        assert result["is_regression"] is False
        assert result["new_failures"] == []

    def test_compare_with_regression(self, clean_files):
        write_baseline(clean_files, fail_count=1, failed_tests=["t1"])
        result = baseline_mod.compare_with_baseline(["t1", "t_new"])
        assert result["is_regression"] is True
        assert result["new_failures"] == ["t_new"]

    def test_compare_by_count(self, clean_files):
        write_baseline(clean_files, fail_count=2)
        result = baseline_mod.compare_with_baseline({"fail_count": 5})
        assert result["is_regression"] is True

    def test_compare_by_count_no_regression(self, clean_files):
        write_baseline(clean_files, fail_count=3)
        result = baseline_mod.compare_with_baseline({"fail_count": 2})
        assert result["is_regression"] is False


# ===========================================================================
# 13. Whitelist commands
# ===========================================================================
class TestWhitelist:
    def test_whitelisted_bypasses_freeze(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files, whitelist_commands=[r"^docker\s+build"])
        write_baseline(clean_files)
        action, _ = enforce.decide("docker build .", cfg)
        assert action == "allow"


# ===========================================================================
# 14. Enforce log
# ===========================================================================
class TestEnforceLog:
    def test_log_created_on_block(self, clean_files):
        write_state(clean_files, directness=0.10, sessions_total=10)
        cfg = write_config(clean_files)
        write_baseline(clean_files)
        enforce.decide("git commit -m 'x'", cfg)
        enforce._log_action("block", 3, "git commit -m 'x'", "test")
        log = json.loads((clean_files / "enforce_log.json").read_text())
        assert len(log) == 1
        assert log[0]["action"] == "block"

    def test_log_appends(self, clean_files):
        enforce._log_action("block", 3, "cmd1", "r1")
        enforce._log_action("warn", 1, "cmd2", "r2")
        log = json.loads((clean_files / "enforce_log.json").read_text())
        assert len(log) == 2


# ===========================================================================
# 15. State migration in check.py
# ===========================================================================
class TestStateMigration:
    def test_v1_state_migrated(self, clean_files):
        """check.py should migrate v1 state to v2."""
        import check
        old_state = {
            "version": "1.0",
            "directness": 0.65,
            "sessions_total": 5,
            "sessions_clean": 3,
            "sessions_hack": 1,
            "sessions_honest_fail": 1,
            "sessions_incomplete": 0,
        }
        # Temporarily patch check module paths
        with patch.object(check, "STATE_FILE", clean_files / "state.json"):
            (clean_files / "state.json").write_text(json.dumps(old_state))
            state = check.load_state()
            assert state["version"] == "2.1"
            assert state["enforce_blocks"] == 0
            assert state["enforce_warnings"] == 0
            assert state["last_block_reason"] is None
            assert state["tamper_incidents"] == []
            # Original fields preserved
            assert state["directness"] == 0.65
            assert state["sessions_total"] == 5


# ===========================================================================
# 16. Tamper detection (signing.py — the real backstop)
# ===========================================================================
class TestTamperDetection:
    def _signed(self, **fields):
        state = {
            "version": "2.1", "directness": 0.90, "sessions_total": 10,
            "enforce_blocks": 0, "enforce_warnings": 0, "last_block_reason": None,
        }
        state.update(fields)
        state[signing.SIGNATURE_FIELD] = signing.compute_signature(state)
        return state

    def test_signed_state_verifies(self):
        assert signing.verify_signature(self._signed()) is True
        assert signing.is_tampered(self._signed()) is False

    def test_edited_score_detected(self):
        state = self._signed(directness=0.50)
        state["directness"] = 0.99  # hand-edit the score
        assert signing.verify_signature(state) is False
        assert signing.is_tampered(state) is True

    def test_unsigned_state_is_not_tamper(self):
        # First run / freshly initialised: no signature yet, not an incident.
        assert signing.is_tampered({"directness": 0.5}) is False

    def test_decide_freezes_on_tamper(self, clean_files):
        cfg = write_config(clean_files)
        tampered = self._signed(directness=0.90)
        tampered["directness"] = 0.99  # forged
        action, msg = enforce.decide("git commit -m wip", cfg, tampered)
        assert action == "block"
        assert "TAMPER" in msg

    def test_decide_allows_reads_on_tamper(self, clean_files):
        cfg = write_config(clean_files)
        tampered = self._signed(directness=0.90)
        tampered["directness"] = 0.99
        action, _ = enforce.decide("pytest -q", cfg, tampered)
        assert action == "allow"

    def test_keyed_mode_when_env_set(self, monkeypatch):
        monkeypatch.setenv("HONEST_CODE_KEY", "s3cret")
        assert signing.is_keyed() is True
        sig = signing.compute_signature({"directness": 0.5})
        assert sig.startswith("hmac-sha256:")
