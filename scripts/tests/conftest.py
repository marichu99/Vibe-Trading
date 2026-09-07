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
