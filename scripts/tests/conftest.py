"""sys.path setup for scripts/ tests.

committee_reporter.py is a standalone script (not a package under agent/),
so it isn't importable via the `pythonpath = ["agent"]` pytest config alone
-- this adds scripts/ itself to sys.path so `import committee_reporter` works.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

AGENT_DIR = SCRIPTS_DIR.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_strategy_tracking(monkeypatch, tmp_path):
    """Keep tests off the real decision log and the live MT5 terminal.

    run_once/run_committee now call strategy_tracking (decision logging with
    a live quote, and the H4/D1 trend gate); without this, tests appended
    rows to logs/decision_log.jsonl and queried the real broker. Tests that
    exercise these functions directly re-patch them as needed.
    """
    import strategy_tracking

    monkeypatch.setattr(strategy_tracking, "DECISION_LOG_PATH", tmp_path / "decision_log.jsonl")
    monkeypatch.setattr(strategy_tracking, "_mid_price", lambda symbol, connection: None)
    monkeypatch.setattr(strategy_tracking, "trend_gate", lambda symbol, connection: (None, "stubbed in tests"))
    monkeypatch.setattr(strategy_tracking, "openrouter_balance_usd", lambda: None)
