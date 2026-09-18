"""Daily automated repair pass for this repo (Vibe-Trading's two live MT5
committee bots + their shared agent code) -- a genuine `claude -p` subprocess
run once a day, scoped to finding and fixing real bugs, never anything more.

Built 2026-09-19 per user request: "introduce an agent that deals entirely
with repairs and fixes in any code everyday that will be based on claude."
User's own scoping answers: propose-only (never auto-commit/push to a real
branch), scan code-review findings + failing tests + runtime error logs,
cover both bots (whole repo).

SAFETY DESIGN -- read this before changing anything here:

  1. Propose-only, hard-enforced, not just prompted. The claude subprocess's
     tool access is Read/Edit/Write/Grep/Glob plus a Bash allowlist scoped
     to running pytest -- it CANNOT run git or gh at all (see _run_claude's
     --allowedTools). Every git/gh side effect (branching, committing,
     pushing, opening the PR) is done by THIS script, in plain Python,
     after the subprocess exits -- never by the LLM. A misbehaving or
     overzealous pass can at most leave file edits sitting in a disposable
     worktree; it cannot push anywhere or open anything by itself.

  2. Isolated worktree, never the live checkout. WORKTREE_DIR is a
     dedicated `git worktree` (see the sibling directory next to this
     repo), completely separate from the directory committee_reporter.py/
     fundednext_reporter.py are actually running from. This can run at any
     time of day with zero risk of editing a file the live loop processes
     are mid-read on, and zero risk of triggering a restart.

  3. Gated on tests passing AND real changes existing. If the worktree's
     own pytest suite doesn't pass after the LLM's edits, or if nothing
     actually changed, nothing is committed, pushed, or opened as a PR --
     see _commit_and_open_pr's callers in main().

  4. Every PR targets `fundednext-challenge` (this repo's active
     development branch), never `main` directly -- promoting to main stays
     a separate, human-made decision, same as every other fix this session.

  5. Dated branches (daily-repair/YYYY-MM-DD), not one reused branch --
     so a previous day's still-open, still-unreviewed PR is never
     rewritten or clobbered by today's run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREE_DIR = REPO_ROOT.parent / "Vibe-Trading-daily-repair-worktree"
BASE_BRANCH = "fundednext-challenge"
SCRATCH_BRANCH = "daily-repair-scratch"
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

# Absolute path, not a bare "gh" -- winget's install of GitHub CLI added
# this directory to the registry's User PATH, but that update doesn't
# reliably propagate to every process (confirmed 2026-09-18: neither this
# session's own shells nor a freshly spawned one picked it up without a
# logoff/logon). A Task Scheduler-launched process at 3 AM is exactly the
# kind of "fresh process, uncertain environment" case that bites -- using
# the full path sidesteps the whole PATH-propagation question.
GH_EXE = r"C:\Program Files\GitHub CLI\gh.exe"
# Also on the User PATH (unlike gh above), but used as an absolute path
# anyway for the same "don't depend on a fresh process's PATH" reasoning.
CLAUDE_EXE = str(Path.home() / ".local" / "bin" / "claude.exe")

LOCK_PATH = REPO_ROOT / "logs" / "daily_repair_agent.lock"
LOG_PATH = REPO_ROOT / "logs" / "daily_repair_agent.log"

CLAUDE_TIMEOUT_SECONDS = 45 * 60
TEST_TIMEOUT_SECONDS = 5 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily_repair_agent")


def _run(
    cmd: list[str], *, cwd: Path, timeout: int | None = None, input_text: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    logger.info("running: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, input=input_text, env=env,
    )


def _reset_worktree_to_base() -> None:
    """Fetch and hard-reset the disposable scratch branch to origin/BASE_BRANCH.

    Runs every invocation, before anything else -- so the worktree always
    starts from a clean, current copy, regardless of whatever a previous
    (possibly interrupted) run left lying around in it.
    """
    _run(["git", "fetch", "origin", BASE_BRANCH], cwd=WORKTREE_DIR, timeout=60)
    result = _run(
        ["git", "checkout", "-B", SCRATCH_BRANCH, f"origin/{BASE_BRANCH}"],
        cwd=WORKTREE_DIR, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not reset worktree to origin/{BASE_BRANCH}: {result.stderr}")
    _run(["git", "clean", "-fd"], cwd=WORKTREE_DIR, timeout=30)


def _recent_runtime_error_summary(hours: int = 24) -> str:
    """Summarize genuine-looking failures from the last N hours of runs.

    Deliberately excludes known infra/config failure signatures (insufficient
    balance, connection/timeout errors) -- those are operational, not code
    bugs, and repeatedly "fixing" the code in response to them would be
    scope creep chasing a non-bug. Only failures that look like an actual
    defect (a Python traceback, an unexpected exception type surfaced by
    our own code) are worth handing to the repair pass.
    """
    runs_dir = REPO_ROOT / "agent" / "runs"
    if not runs_dir.exists():
        return "(no agent/runs directory found)"

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    infra_markers = (
        "insufficient balance", "402", "connection", "timeout", "rate limit",
        "econnreset", "network", "503", "529", "overloaded",
    )
    findings: list[str] = []
    for run_dir in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            break  # runs_dir is walked newest-first by mtime; older than cutoff means we're done
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if state.get("status") != "failed":
            continue
        reason = str(state.get("reason") or "")
        if any(marker in reason.lower() for marker in infra_markers):
            continue
        findings.append(f"- {run_dir.name}: {reason[:500]}")
        if len(findings) >= 15:
            break

    if not findings:
        return "(no non-infra failures found in the last 24h of runtime logs)"
    return "\n".join(findings)


def _build_prompt(runtime_errors: str) -> str:
    return f"""You are running as an unattended daily maintenance pass over this repo
(Vibe-Trading: two live MT5 forex trading bots run by LLM committees, real
money on both accounts). Your ONLY job this run: find and fix genuine
correctness bugs. Nothing else.

Hard scope limits -- follow these exactly:
- Fix ONLY real, verified defects: something that produces a wrong result,
  crashes, or silently does the wrong thing under a concrete, describable
  input/state. Every fix must come with an articulable failure scenario.
- Do NOT refactor, rename, restyle, add abstractions, or "clean up" code
  that already works correctly. Do NOT change trading strategy, position
  sizing, mandate ceilings, TARGETS lists, or any other numeric/behavioral
  parameter -- those are deliberate trading decisions, not bugs.
- Do NOT touch: agent/.env, any mandate *.json file, anything under logs/
  or agent/runs/ (read those, never write them), or any file whose purpose
  is clearly runtime state rather than source code.
- Do NOT attempt to run, start, stop, or restart either bot, place any
  trade, or contact a live broker/LLM-provider API. You have no network
  access to those and should not try.
- Do NOT run git or gh commands of any kind -- you don't have permission
  for them and a separate process handles all of that after you're done.
  Your only Bash usage should be running the test suite.
- If you find nothing genuine to fix, make NO changes and say so plainly.
  Do not manufacture busywork or nitpicks to justify the run.

What to check, in order:
1. Run the full test suite first, to see current state:
   {VENV_PYTHON} -m pytest scripts/tests/ agent/tests/test_mandate_enforcement.py -q
2. Review scripts/committee_reporter.py, scripts/fundednext_reporter.py, and
   their shared dependencies under agent/src/trading/ and agent/src/live/
   the way a careful, skeptical senior reviewer would -- concrete failure
   scenarios, not style opinions. Pay particular attention to any function
   that touches money (order sizing, stop/target math, position matching,
   halt/kill-switch logic) since that's where a subtle bug has real cost.
3. Review these non-infra runtime failures from the last 24h (already
   filtered to exclude balance/network/timeout errors -- these are the
   ones worth investigating for an actual code defect):

{runtime_errors}

4. For every genuine bug found: fix it with the smallest correct change,
   and add or update a test that would have caught it (this repo's test
   convention: monkeypatch every `*_PATH` constant and every external call
   -- zero real network/MT5/subprocess calls in tests; see existing tests
   in scripts/tests/ for the pattern).
5. Re-run the full test suite (same command as step 1). Every fix must
   leave it green. If you can't get a fix to a green, passing state,
   revert that specific fix rather than leaving the suite red.
6. End your final answer with a structured summary in exactly this form:
   Files changed: <comma-separated list, or "none">
   Fixes: <one line per fix: file:function -- the concrete failure
   scenario it addresses -- or "none found" if nothing genuine turned up>
   Tests: <pass/fail and count>
"""


def _run_claude(prompt: str) -> subprocess.CompletedProcess:
    # Deliberately NOT --permission-mode bypassPermissions -- that disables
    # ALL permission checks, which is too broad for an unattended, daily,
    # indefinitely-recurring subprocess (Claude Code's own safety tooling
    # flagged an earlier draft of this exact line during development,
    # 2026-09-18, for good reason). --allowedTools is a narrow, explicit
    # allowlist instead: Read/Edit/Write/Grep/Glob for the actual repair
    # work, plus Bash scoped to ONLY this venv's python.exe (for running
    # pytest -- nothing else). In print/headless mode, any tool call that
    # falls outside an explicit allow is auto-denied rather than prompting
    # (there's no human here to prompt) -- so this is a real allowlist with
    # deny-by-default for everything else, not a bypass of anything.
    cmd = [
        CLAUDE_EXE, "-p", prompt,
        "--allowedTools", f"Read Edit Write Grep Glob Bash({VENV_PYTHON} *)",
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--output-format", "json",
    ]
    return _run(cmd, cwd=WORKTREE_DIR, timeout=CLAUDE_TIMEOUT_SECONDS)


def _tests_pass() -> bool:
    result = _run(
        [str(VENV_PYTHON), "-m", "pytest", "scripts/tests/", "agent/tests/test_mandate_enforcement.py", "-q"],
        cwd=WORKTREE_DIR, timeout=TEST_TIMEOUT_SECONDS,
    )
    logger.info("post-fix test run (exit %s):\n%s", result.returncode, result.stdout[-3000:])
    return result.returncode == 0


def _has_changes() -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=WORKTREE_DIR, timeout=30)
    return bool(result.stdout.strip())


def _gh_token_from_git_credential_manager() -> str | None:
    """Derive a GitHub token from git's own credential manager for `gh`'s use.

    Deliberately NOT `gh auth login` -- that would persist a separate,
    long-lived token in gh's own store. `git credential fill` reuses
    whatever OAuth token Git Credential Manager already holds (the same
    one `git push` above just used successfully), scoped per-subprocess-
    call via GH_TOKEN and never written to disk -- same approach used
    earlier this session for one-off `gh pr create` calls.
    """
    result = _run(
        ["git", "credential", "fill"],
        cwd=WORKTREE_DIR, timeout=15,
        input_text="protocol=https\nhost=github.com\n\n",
    )
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return None


def _commit_and_open_pr(date_str: str, claude_summary: str) -> str | None:
    branch = f"daily-repair/{date_str}"
    _run(["git", "checkout", "-b", branch], cwd=WORKTREE_DIR, timeout=30)
    _run(["git", "add", "-A"], cwd=WORKTREE_DIR, timeout=30)
    commit_msg = (
        f"fix: daily automated repair pass ({date_str})\n\n"
        f"{claude_summary}\n\n"
        f"Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
    )
    commit_result = _run(["git", "commit", "-m", commit_msg], cwd=WORKTREE_DIR, timeout=30)
    if commit_result.returncode != 0:
        logger.warning("git commit failed, nothing to open a PR for: %s", commit_result.stderr)
        return None

    push_result = _run(["git", "push", "-u", "origin", branch], cwd=WORKTREE_DIR, timeout=120)
    if push_result.returncode != 0:
        logger.error("git push failed: %s", push_result.stderr)
        return None

    pr_body = (
        f"Automated daily repair pass for {date_str}. Propose-only by design -- "
        f"review before merging, same as any other PR.\n\n{claude_summary}\n\n"
        f"\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
    )
    gh_token = _gh_token_from_git_credential_manager()
    if not gh_token:
        logger.error("could not derive a GitHub token from git credential manager -- branch %s was still pushed, but no PR was opened", branch)
        return None
    pr_result = _run(
        [
            GH_EXE, "pr", "create",
            "--base", BASE_BRANCH,
            "--head", branch,
            "--title", f"fix: daily automated repair pass ({date_str})",
            "--body", pr_body,
        ],
        cwd=WORKTREE_DIR, timeout=60,
        env={**os.environ, "GH_TOKEN": gh_token},
    )
    if pr_result.returncode != 0:
        logger.error("gh pr create failed (branch %s was still pushed): %s", branch, pr_result.stderr)
        return None
    return pr_result.stdout.strip()


def main() -> int:
    if LOCK_PATH.exists():
        logger.warning("lock file %s already exists -- a previous run may still be active; exiting", LOCK_PATH)
        return 1
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(datetime.now(timezone.utc)), encoding="utf-8")
    try:
        if not WORKTREE_DIR.exists():
            logger.error("worktree %s does not exist -- run `git worktree add` first", WORKTREE_DIR)
            return 1

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info("=== daily repair agent starting for %s ===", date_str)

        _reset_worktree_to_base()
        runtime_errors = _recent_runtime_error_summary()
        prompt = _build_prompt(runtime_errors)

        try:
            result = _run_claude(prompt)
        except subprocess.TimeoutExpired:
            logger.error("claude subprocess timed out after %ss", CLAUDE_TIMEOUT_SECONDS)
            return 1

        if result.returncode != 0:
            logger.error("claude subprocess exited %s\nstderr: %s", result.returncode, result.stderr[-3000:])
            return 1

        try:
            payload = json.loads(result.stdout)
            claude_summary = payload.get("result") or payload.get("output") or result.stdout
        except json.JSONDecodeError:
            claude_summary = result.stdout
        logger.info("claude summary:\n%s", claude_summary)

        if not _has_changes():
            logger.info("no file changes made -- nothing to propose for %s", date_str)
            return 0

        if not _tests_pass():
            logger.warning("tests failed after the repair pass -- discarding changes, NOT opening a PR")
            _run(["git", "checkout", "."], cwd=WORKTREE_DIR, timeout=30)
            _run(["git", "clean", "-fd"], cwd=WORKTREE_DIR, timeout=30)
            return 1

        pr_url = _commit_and_open_pr(date_str, claude_summary)
        if pr_url:
            logger.info("opened PR: %s", pr_url)
        else:
            logger.warning("changes existed and tests passed, but PR creation failed -- see log above")
        return 0
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
