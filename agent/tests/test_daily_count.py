"""Tests for the per-broker daily order counter (src/live/daily_count.py).

Pure-filesystem state -- exercised through tmp_path, same pattern as
test_halt.py. No network/MT5/subprocess call is ever made.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.live import daily_count
from src.live import paths


@pytest.fixture
def live_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    return tmp_path


def test_read_zero_when_missing(live_runtime: Path) -> None:
    assert daily_count.read_daily_count("robinhood") == 0


def test_increment_persists_and_reads_back(live_runtime: Path) -> None:
    assert daily_count.increment_daily_count("robinhood") == 1
    assert daily_count.increment_daily_count("robinhood") == 2
    assert daily_count.read_daily_count("robinhood") == 2


def test_concurrent_increments_do_not_lose_updates(live_runtime: Path) -> None:
    """Real race: two near-simultaneous callers for the same broker (an
    overlapping/stuck prior run racing a fresh one) each used to read the
    same starting count and write back count+1, losing one increment --
    silently under-reporting trades against max_trades_per_day, a hard
    mandate-gate check (enforcement.py), not just a bookkeeping nit.

    Real threads (not a monkeypatched interleave) so the read-modify-write
    section's file I/O actually overlaps across the GIL boundary -- this
    reliably lost updates before the _acquire_lock fix.
    """
    n_threads = 20
    barrier = threading.Barrier(n_threads)

    def _run() -> None:
        barrier.wait()
        daily_count.increment_daily_count("robinhood")

    threads = [threading.Thread(target=_run) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert daily_count.read_daily_count("robinhood") == n_threads


def test_stale_lock_is_reclaimed(live_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock file left behind by a crashed holder must not stall every
    future call forever -- only the one call that hits the full timeout."""
    path = daily_count._counter_path("robinhood")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.lock")
    lock.touch()
    old_time = __import__("time").time() - daily_count._LOCK_STALE_SECONDS - 1
    __import__("os").utime(lock, (old_time, old_time))

    assert daily_count.increment_daily_count("robinhood") == 1
    assert not lock.exists()
