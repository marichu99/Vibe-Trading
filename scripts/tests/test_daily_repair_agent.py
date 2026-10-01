"""Tests for the daily repair agent's memory of earlier proposals (2026-09-28)."""
from __future__ import annotations

import importlib.util
import shutil
import types
from pathlib import Path

import daily_repair_agent as dra


def test_extract_fixes_stops_at_tests_line() -> None:
    body = "fix: x\n\nsummary\nFixes:\n- a.py:f -- bug one\n\n- b.py:g -- bug two\nTests: pass, 10\n"
    assert dra._extract_fixes(body) == "Fixes:\n- a.py:f -- bug one\n- b.py:g -- bug two"


def test_extract_fixes_missing_section() -> None:
    assert dra._extract_fixes("fix: x\n\nno section here") == ""


def test_prompt_lists_already_proposed_and_forbids_refixing() -> None:
    prompt = dra._build_prompt("(no errors)", "[daily-repair/2026-09-27 -- still UNMERGED]\nFixes: a.py:f")
    assert "do NOT fix any of these again" in prompt and "daily-repair/2026-09-27" in prompt


def test_already_proposed_summary_reads_unmerged_branches(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "branch"]:
            out = "  origin/daily-repair/2026-09-30\n"
        elif cmd[:3] == ["git", "log", "-1"]:
            out = "fix: daily\n\nFixes:\n- x.py:f -- boom\nTests: pass\n"
        else:
            out = "- feat: something recent\n"
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(dra, "_run", fake_run)
    text = dra._already_proposed_summary()
    assert "daily-repair/2026-09-30 -- still UNMERGED" in text and "x.py:f -- boom" in text
    assert "feat: something recent" in text


def test_module_import_creates_missing_logs_dir(tmp_path) -> None:
    """logs/ is gitignored, so a freshly created `git worktree add` checkout
    (exactly what WORKTREE_DIR is) starts with no logs/ directory. Importing
    the module used to crash with FileNotFoundError from logging.FileHandler
    before main() ever got a chance to create the directory, which meant
    _tests_pass() always failed collection in a fresh worktree and the
    propose-only safety gate could never let a fix through."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dest = scripts_dir / "daily_repair_agent.py"
    shutil.copy(Path(dra.__file__), dest)
    assert not (tmp_path / "logs").exists()

    spec = importlib.util.spec_from_file_location("dra_fresh_import", dest)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert (tmp_path / "logs" / "daily_repair_agent.log").exists()
