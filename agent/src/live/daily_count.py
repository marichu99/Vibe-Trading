"""Per-broker daily order counter (UTC calendar day, atomic write).

Shared by every live order path (the MCP ``LiveOrderGuardTool`` keeps its own
in-class copy for now; the direct-SDK gate uses these helpers). The counter is
advisory defense-in-depth — the broker enforces the real ceiling — so any
read failure reads as ``0`` (fail-open on the count only, never on the order).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from src.live.paths import broker_dir

_COUNTER_FILENAME = "trade_counter.json"

# increment_daily_count is read-then-write; two near-simultaneous calls for
# the same broker (e.g. an overlapping/stuck prior run racing a fresh one --
# a documented real pattern on this machine, see committee_reporter.py's
# singleton-lock incident) can both read the same starting count and each
# write back count+1, losing one increment. That under-reports today's trade
# count against max_trades_per_day, a HARD mandate-gate check
# (enforcement.py), not just a bookkeeping nit -- so the critical section is
# now guarded by an exclusive-create lock file.
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01
# A lock held this long is almost certainly a crash leftover (the critical
# section itself is a few lines of local I/O) -- removed so a dead lock
# doesn't stall every future call forever, not just the one that hits the
# timeout above.
_LOCK_STALE_SECONDS = 30.0


def _counter_path(broker: str):
    return broker_dir(broker) / _COUNTER_FILENAME


def _acquire_lock(path: Path) -> Path | None:
    """Best-effort exclusive lock for ``path``'s read-increment-write section.

    Returns the lock file path to release later, or None if the lock
    couldn't be acquired within ``_LOCK_TIMEOUT_SECONDS`` -- callers fail
    open (proceed without it) rather than ever blocking a real order on this
    advisory counter.
    """
    lock = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR))
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue  # retry immediately -- don't burn the deadline on a stale lock
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(_LOCK_POLL_SECONDS)


def _release_lock(lock: Path | None) -> None:
    if lock is None:
        return
    try:
        lock.unlink()
    except OSError:
        pass


def _utc_today() -> str:
    """Return today's UTC calendar date as ``YYYY-MM-DD``."""
    return datetime.now(timezone.utc).date().isoformat()


def read_daily_count(broker: str) -> int:
    """Return today's order count for ``broker`` (UTC rollover; 0 on any miss)."""
    path = _counter_path(broker)
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(raw, dict) or raw.get("date") != _utc_today():
        return 0
    try:
        return int(raw.get("count", 0))
    except (TypeError, ValueError):
        return 0


def increment_daily_count(broker: str) -> int:
    """Persist ``broker``'s incremented count for today (atomic). Returns new count."""
    path = _counter_path(broker)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(path)
    try:
        today = _utc_today()
        count = read_daily_count(broker) + 1
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps({"date": today, "count": count}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return count
    finally:
        _release_lock(lock)
