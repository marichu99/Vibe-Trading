"""Tests for the bash tool's shell-dialect description and basic execution.

Regression: an agent (nested PM sub-agent inside a swarm run, then
independently the top-level committee_reporter agent in the same pass)
burned its iteration budget on Unix-only commands (ls, pwd, heredocs) that
predictably fail under subprocess.run(..., shell=True) on Windows, which
invokes cmd.exe, not bash, despite the tool's name. See run_id
20260908_104207_87_2c8bb2 -- the description previously gave no warning
about this at all.
"""

from __future__ import annotations

import json
import platform

import pytest

from src.tools.bash_tool import BashTool

pytestmark = pytest.mark.unit


class TestBashToolDescription:
    def test_names_the_actual_shell_for_this_platform(self) -> None:
        description = BashTool.description
        if platform.system() == "Windows":
            assert "cmd.exe" in description
            assert "NOT POSIX bash" in description
        else:
            assert "POSIX" in description

    def test_description_still_explains_the_tool(self) -> None:
        """The shell-dialect note is an addition, not a replacement."""
        assert "Execute a shell command" in BashTool.description


class TestBashToolExecute:
    def _run(self, command: str) -> dict:
        return json.loads(BashTool().execute(command=command))

    def test_successful_command(self) -> None:
        result = self._run("echo hello")
        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    def test_failing_command_reports_nonzero_exit(self) -> None:
        result = self._run("exit 1")
        assert result["status"] == "error"
        assert result["exit_code"] == 1

    def test_output_is_truncated_past_the_limit(self, monkeypatch) -> None:
        import src.tools.bash_tool as bash_tool_module

        monkeypatch.setattr(bash_tool_module, "_OUTPUT_LIMIT", 10)
        # Windows cmd.exe needs a real command whose stdout exceeds 10 bytes.
        cmd = "echo 01234567890123456789" if platform.system() == "Windows" else "printf '%.0sX' {1..50}"
        result = self._run(cmd)
        assert len(result["stdout"]) <= 10
