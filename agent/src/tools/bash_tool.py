"""Bash tool: execute shell commands under run_dir."""

from __future__ import annotations

import json
import platform
import subprocess
from typing import Any

from src.agent.tools import BaseTool

_OUTPUT_LIMIT = 50_000
_DEFAULT_TIMEOUT = 120

# subprocess.run(..., shell=True) delegates to the OS's own default shell --
# cmd.exe on Windows, /bin/sh elsewhere -- never a POSIX bash regardless of
# this tool's name. An agent that assumes real bash burns iterations on
# predictable failures (`pwd`, `ls`, heredocs all fail on cmd.exe) before
# ever adapting; naming the actual shell up front avoids that class of
# wasted budget entirely. Computed once at import time from the host this
# process actually runs on, so it stays correct across deployments (this
# repo runs on Windows locally, but the same code may run on Linux/mac
# elsewhere) rather than hardcoding one platform's assumptions.
if platform.system() == "Windows":
    _SHELL_NOTE = (
        "Runs via Windows cmd.exe, NOT POSIX bash — despite this tool's name, ls/pwd/cat/heredocs "
        "(<<) and other Unix-only syntax will fail here; use dir/cd/type and cmd.exe-compatible "
        "syntax instead."
    )
else:
    _SHELL_NOTE = "Runs via the host's default POSIX shell (/bin/sh)."

_DESCRIPTION = (
    "Execute a shell command in the working directory. Use for installing packages, running "
    f"scripts, or inspecting files. {_SHELL_NOTE}"
)


class BashTool(BaseTool):
    """Execute shell commands in the working directory."""

    name = "bash"
    description = _DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        """Execute a shell command.

        Args:
            **kwargs: Must include command. Optional run_dir used as cwd.

        Returns:
            JSON string with stdout, stderr, and exit_code.
        """
        command = kwargs["command"]
        cwd = kwargs.get("run_dir")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
            stdout = result.stdout[:_OUTPUT_LIMIT] if len(result.stdout) > _OUTPUT_LIMIT else result.stdout
            stderr = result.stderr[:_OUTPUT_LIMIT] if len(result.stderr) > _OUTPUT_LIMIT else result.stderr
            return json.dumps({
                "status": "ok" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({
                "status": "error",
                "error": f"Command timed out after {_DEFAULT_TIMEOUT}s",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": str(exc),
            }, ensure_ascii=False)
