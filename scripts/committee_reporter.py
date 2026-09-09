"""Runs Vibe-Trading swarm committees on a schedule and emails a report.

Runs one or more committee presets (bull/bear/risk/PM debates) against
configured targets and emails the result. A target with a `trade` entry also
has the agent check positions and conditionally place a demo order on the
PM's decision (see TARGETS below) — a target without one is research-only,
no trading. is_reportable() controls which runs are worth an email.

This is the single place that both decides and (optionally) trades — do not
also run the same instrument through the server's /scheduled-runs executor,
or two independent processes can both check-then-place around the same race
window.

Each committee run is a fresh, isolated `python -m cli run` subprocess (the
same path the `vibe-trading` CLI itself uses), so a stuck or crashed run never
takes the reporter down with it.

Usage:
    # One pass over all TARGETS, then exit (good for Windows Task Scheduler):
    .venv\\Scripts\\python.exe scripts\\committee_reporter.py --once

    # Keep running, repeating every --interval seconds:
    .venv\\Scripts\\python.exe scripts\\committee_reporter.py --loop --interval 3600

Required environment (e.g. in ~/.vibe-trading/.env, or your shell):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
Optional: EMAIL_CC (comma-separated)
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

# On Windows, stdout/stderr default to the system ANSI codepage (cp1252 on
# this machine) when redirected to a file (startup.ps1's -RedirectStandardOutput
# into logs/reporter.log), not UTF-8 -- but _status_log_summary() reads that
# file back with encoding="utf-8". Any non-ASCII character logged (e.g. the
# em-dash in "{committee} — {target} ({tag})" email subjects) then gets
# written as a single cp1252 byte that isn't valid UTF-8, decoding back as
# U+FFFD and corrupting --status/the emailed report. Force real UTF-8 here so
# what's written matches what's read. Guarded: reconfigure() can be absent/
# fail in an unusual stream (e.g. tests capturing stdout) -- never let purely
# cosmetic log encoding crash the loop.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("committee_reporter")

# --------------------------------------------------------------------------- #
# What to run each pass — add/remove targets freely.
# --------------------------------------------------------------------------- #

TARGETS: list[dict[str, object]] = [
    # PAUSED 2026-09-03 at the user's request: gold's 15m-ATR stop floor has
    # been running hot (logs/risk_cap_gap_history.jsonl: ~$6.6-$20.0 across
    # 16 passes on 2026-09-01/02) — even after raising MAX_LOSS_PER_ORDER_USD
    # to $30 same day, the user chose to pause gold entirely until it calms
    # down rather than keep trading through the volatility, and shift focus
    # to EURUSD (below) in the meantime. No mandate change needed to
    # re-enable — the committed mandate's "commodity" asset class still
    # covers XAUUSDm. Resume when gold's ATR floor settles back down
    # (logs/risk_cap_gap_history.jsonl is still being appended to every pass
    # for EURUSD/whatever's live, so there's no live gold reading to check it
    # against until this is re-enabled — check via a manual _atr_stop_floor
    # call, same pattern as scripts/diversification_roadmap.md, before
    # flipping this back on).
    # {
    #     "committee": "investment_committee", "target": "XAUUSD", "market": "commodity/forex",
    #     "trade": {"symbol": "XAUUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
    # },
    {
        # LIVE — real money (mt5-live-trade), not demo. Added 2026-09-03 at
        # the user's request, replacing gold (paused above) as the sole live
        # target while gold's volatility is elevated. Verified read-only
        # before enabling: EURUSDm contract_size=100000, 15m ATR-based stop
        # floor ~0.00076 price units vs. a ~0.03-unit budget under the $30
        # cap at 0.01 lots — comfortably inside it, unlike gold's current
        # situation. Requires the mandate's asset_classes to include "forex"
        # (scripts/commit_mt5_mandate.py) — a mandate-only "commodity"
        # authorization will fail-closed deny every order here. max_stack=1:
        # same no-pyramiding policy as gold.
        "committee": "investment_committee", "target": "EURUSD", "market": "forex",
        "trade": {"symbol": "EURUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
    },
    {
        # LIVE — added 2026-09-04 at the user's request, as a genuine
        # diversifier alongside EURUSD rather than a third same-bet pair.
        # Checked real 60-day daily-return correlation against EURUSDm before
        # adding: AUDUSD 0.61 (best of the USD-quoted candidates) vs. GBPUSD
        # 0.83 and NZDUSD 0.76 (too correlated — mostly doubling the same
        # EUR-bloc-vs-USD bet, not real diversification). USDCAD/USDJPY were
        # more negatively correlated (-0.69 / -0.52) but were NOT added: MT5
        # contract_size() returns raw units, so _max_stop_distance's
        # MAX_LOSS_PER_ORDER_USD / (contract_size * lots) formula is only
        # correct in USD terms when the QUOTE currency is USD (true for
        # AUDUSD/EURUSD/GBPUSD, false for USDCAD/USDJPY where the quote
        # currency is CAD/JPY) — using those today would silently mis-price
        # the dollar risk cap without an added currency-conversion step.
        # Verified read-only before enabling: AUDUSDm contract_size=100000,
        # 15m-ATR stop floor ~$0.54 at 0.01 lots — comfortably inside the $4
        # cap (see MAX_LOSS_PER_ORDER_USD above). max_stack=1: same
        # no-pyramiding policy as the other live targets.
        "committee": "investment_committee", "target": "AUDUSD", "market": "forex",
        "trade": {"symbol": "AUDUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
    },
    # Re-enabled 2026-09-08, at the user's request — the milestone reminder
    # (_check_silver_milestone) had been firing every pass since equity first
    # crossed $100, and the original 2026-09-01 pause rationale (a clean,
    # unconfounded gold-only observation period) is moot now that gold itself
    # was paused 2026-09-03 in favor of EURUSD/AUDUSD. No mandate change was
    # needed to re-enable this — the committed mandate's "commodity" asset
    # class already covers XAGUSDm (see src.trading.service._mt5_asset_class).
    #
    # Expect this to sit in WAIT every pass, not actually trade, until a
    # human sets it a per-symbol risk cap: verified live 2026-09-08,
    # XAGUSDm's 15m-ATR stop floor is ~0.346 price units, but the current
    # ~$4 shared per-order budget (_effective_max_loss_usd) only buys a
    # ~0.08-unit stop at 0.01 lots on silver's 5000 oz/lot contract (vs
    # gold's 100 oz) — the VOLATILITY/SPREAD FLOOR check in _build_prompt
    # will force WAIT every time until commit_mt5_mandate.py (human-only) is
    # re-run with a wider per-symbol cap for this instrument.
    {
        "committee": "investment_committee", "target": "XAGUSD", "market": "commodity/forex",
        "trade": {"symbol": "XAGUSDm", "connection": "mt5-live-trade", "lots": 0.01, "max_stack": 1},
    },
    # PAUSED 2026-08-25: an MT5 terminal can only be signed into ONE account at
    # a time. The terminal is now signed into the LIVE account (needed for the
    # gold target above), so these mt5-demo-trade targets crash every pass with
    # MT5ProfileMismatchError (demo profile guard correctly refusing to run
    # against a non-demo account). Re-enable once either the terminal is back
    # on demo (breaking live gold) or a second terminal instance is running the
    # demo account with its own terminal_path (see mt5-demo-trade profile).
    # {
    #     "committee": "investment_committee", "target": "EURUSD", "market": "forex",
    #     "trade": {"symbol": "EURUSDm", "connection": "mt5-demo-trade", "lots": 0.01},
    # },
    # {
    #     # US Tech 100 (Nasdaq) index CFD. Exness's own minimum lot for this
    #     # symbol is 0.05 (not 0.01) - verified live via symbol_info.
    #     "committee": "investment_committee", "target": "USTEC (Nasdaq 100)", "market": "US index",
    #     "trade": {"symbol": "USTECm", "connection": "mt5-demo-trade", "lots": 0.05},
    # },
    # {
    #     # S&P 500 index CFD. Exness's own minimum lot for this symbol is 0.14.
    #     "committee": "investment_committee", "target": "US500 (S&P 500)", "market": "US index",
    #     "trade": {"symbol": "US500m", "connection": "mt5-demo-trade", "lots": 0.14},
    # },
    # {"committee": "risk_committee", "target": "XAUUSD", "market": "commodity/forex"},
]

# This caps the WRAPPING agent's own loop (run_swarm once, check positions,
# maybe place an order, write the report) — a handful of iterations is
# normally enough for that. It does NOT cap each swarm sub-agent's own
# iteration budget (bull/bear/risk/PM each have their own max_iterations: 50
# in the shared investment_committee.yaml preset, used app-wide) — that's
# the bulk of the real spend (one PM task alone used 170k+ tokens across 17
# iterations of independent verification) and would need a separate,
# bigger-blast-radius edit to the shared preset to reduce.
MAX_ITER = 15
RUN_TIMEOUT_SECONDS = 3600

# Cap on concurrent SAME-direction positions per symbol. Opposite-direction
# stacking is never allowed regardless of this cap (avoids a same-symbol
# long+short conflict on the hedging-mode MT5 account). A target's own trade
# dict may override this via "max_stack" (see TARGETS — the live gold target
# uses max_stack=1, no pyramiding).
MAX_SAME_DIRECTION_POSITIONS = 4

# Connection ids that place REAL orders against real money, as opposed to a
# broker's demo/paper sandbox. Gates the circuit breaker below — it never
# touches demo trading, which has no capital at risk.
LIVE_CONNECTIONS = {"mt5-live-trade"}

# Auto-halt live trading for the rest of the UTC day if equity drops this much
# from the balance recorded at the first live check of that day. Independent
# of (in addition to) the committed mandate's own hard caps (max order/total
# notional, max leverage, max trades/day) — this is a portfolio-level backstop
# those per-order caps don't cover. Trips the SAME kill-switch sentinel the
# mandate gate checks (src.live.halt) so it also blocks any other code path
# hitting this account, and flattens the open position itself (committee_
# reporter doesn't run through the live runtime scheduler that would
# otherwise do this automatically on a halt trip).
LIVE_DRAWDOWN_HALT_PCT = 0.5
LIVE_BASELINE_PATH = REPO_ROOT / "logs" / "live_baseline.json"

# Magic number stamped on every order committee_reporter/the mt5 connector
# places (MT5Config's default `magic`, agent/src/trading/connectors/mt5/sdk.py).
# Anything else on this account — a separately-running signal-service EA,
# manual trades — carries a different magic. Used both to keep the
# same-direction position cap from double-counting someone else's position as
# ours, and to summarize that other activity as context (see
# _signal_service_activity below).
OUR_MAGIC = 20260000
SIGNAL_LOOKBACK_DAYS = 7
# Hard cap on lines in the signal-service context block, regardless of how
# active that EA actually is — an unbounded block would silently grow (and
# grow prompt cost) in direct proportion to someone else's trade frequency.
SIGNAL_ACTIVITY_MAX_LINES = 5

# Trade journal: our own past decisions + verified outcomes, fed back into the
# prompt as a short stats line so the committee has situational awareness of
# its own recent track record — no persisted memory existed before this (see
# committee_reporter.py's own docstring: every run is a fresh, isolated
# subprocess). Deliberately terse (one line, not full reasoning replay/an
# extra LLM call) — the whole point was minimum added token cost, not a
# richer feedback mechanism.
TRADE_JOURNAL_PATH = REPO_ROOT / "logs" / "trade_journal.json"
JOURNAL_SUMMARY_WINDOW = 8

# Grace period before a journal entry not found in get_positions() is
# assumed genuinely closed, rather than not-yet-visible due to a brief
# broker-side propagation delay right after order_send returns. Real
# incident 2026-09-03/04: _journal_reconcile_closed runs a second time (to
# build the emailed status report's "Track record" section) seconds after
# run_committee's own reconcile+place+record_open sequence in the SAME
# pass -- twice, a just-placed market order wasn't yet reflected in
# get_positions() that soon, so a real, hours-long position got wrongly
# marked closed/unknown 37-62ms after opening. Any entry younger than this
# is left alone; the NEXT pass's reconciliation (by which time the broker
# has caught up) resolves it correctly either way.
JOURNAL_RECONCILE_GRACE = timedelta(seconds=120)

# Excursion analysis: for every closed trade, pull the real price bars between
# open and close and check whether it moved favorably before the outcome it
# ended with — distinguishing a "clean" loss (moved against the position
# almost immediately; the stop did its job, nothing to fix) from a
# "reversal" (moved meaningfully in profit, then gave it back — the kind of
# loss a breakeven-stop rule could actually have helped). Pure historical-bar
# reads, zero LLM cost — this is how "learn from every win and loss" happens
# without an extra committee run per trade.
EXCURSION_BAR_PERIOD = "15m"
REVERSAL_THRESHOLD_FRACTION = 0.5  # favorable move >= this fraction of the stop distance

# Worst-case planned loss per order, enforced deterministically at the mandate
# gate (see scripts/commit_mt5_mandate.py — MUST match MAX_LOSS_PER_ORDER_USD
# there, these are two independently-maintained numbers). The gate denies
# outright if breached; this constant lets the prompt tell the committee the
# actual price-distance budget up front, so its stops land WITHIN the cap
# instead of just getting denied more often for no smaller-risk benefit.
#
# Raised 2026-08-27's original $10 to $20, then to $30, on 2026-09-03 at the
# user's request after reviewing why the cap exists (see
# commit_mt5_mandate.py's docstring): gold's 15m-ATR stop floor spent most
# of the prior two days above $10 (logs/risk_cap_gap_history.jsonl), so the
# cap was blocking most passes outright rather than the committee choosing
# not to trade.
#
# Dropped 2026-09-04 from $30 to $4, at the user's explicit request, to match
# a stated portfolio policy: risk 10% of account equity total, split across
# up to 3 concurrent trades ($123.74 equity at the time -> ~$4.12/trade). $30
# was ~24% of equity on a single order — far looser than intended, even
# though realized EURUSD stops had been landing at $0.7-2 in practice (the
# ATR floor, not the cap, was the binding constraint day to day). No target's
# 15m-ATR stop floor was pushed above this at the time (EURUSD ~$0.73,
# AUDUSD ~$0.54, both at 0.01 lots) — verify again with a fresh
# _atr_stop_floor call before relying on that if volatility has moved since.
# Requires the mandate to actually be re-committed (scripts/
# commit_mt5_mandate.py) for the enforced side to match — this constant
# alone only changes the prompt/pre-filter, not the gate's own limit.
MAX_LOSS_PER_ORDER_USD = 4.0

# Portfolio risk policy (2026-09-04, user's request): risk PORTFOLIO_RISK_
# FRACTION of account equity in total, spread across up to PORTFOLIO_MAX_
# CONCURRENT_TRADES concurrent positions. The mandate gate has no portfolio-
# level aggregate check (it only enforces the flat per-order ceiling above),
# so this is approximated by sizing each order's OWN budget down to
# equity * PORTFOLIO_RISK_FRACTION / PORTFOLIO_MAX_CONCURRENT_TRADES,
# recomputed live each pass (see _effective_max_loss_usd) instead of a fixed
# number that goes stale as equity moves. This can only TIGHTEN the
# per-pass budget below MAX_LOSS_PER_ORDER_USD, never loosen it past the
# mandate's own committed ceiling.
PORTFOLIO_RISK_FRACTION = 0.10
PORTFOLIO_MAX_CONCURRENT_TRADES = 3

# Volatility floor for the stop-loss: below ATR_STOP_MULTIPLE x ATR, a stop
# sits inside the instrument's normal noise band and risks getting clipped by
# ordinary fluctuation before the thesis has a chance to play out, regardless
# of whether the thesis itself was right. Real case: the 2026-08-27 -$7.00
# loss used an 8-unit stop against a 6.09-unit 15m ATR (1.31x ATR) and was
# stopped out 10 minutes after entry, before any real move happened. Checked
# deterministically (pure historical-bar math, zero LLM cost — see
# _atr_stop_floor) against MAX_LOSS_PER_ORDER_USD's own distance budget: if
# the ATR floor exceeds what the account can afford to risk, the honest
# answer is that this instrument's current volatility doesn't fit the risk
# budget at this position size, and the pass should WAIT rather than take a
# trade with an inadequate stop.
ATR_PERIOD = "15m"
ATR_LOOKBACK_BARS = 14
ATR_STOP_MULTIPLE = 1.5

# Spread floor for the stop-loss: on a tight enough stop, the live bid/ask
# spread alone (before any fill slippage) eats a large share of the planned
# risk before the trade has even had a chance to work — a committee's own
# CRO review has independently flagged this ("~10-25% drag from spread+slip
# alone" on 20-22 pip EURUSD stops) but only as prose in one pass's report,
# with nothing enforcing it on the next. MIN_STOP_TO_SPREAD_RATIO=8 caps the
# spread's own share of the stop at 1/8 = 12.5% (before slippage on top),
# the low end of that observed range, and folds into the same WAIT-if-
# infeasible mechanism as ATR_STOP_MULTIPLE above (see _build_prompt) rather
# than being left as a suggestion the committee can talk itself past.
MIN_STOP_TO_SPREAD_RATIO = 8.0

# Milestone reminder: the user asked to be told once the live account hits
# this equity, to reconsider adding silver (XAGUSDm — the nearest cousin to
# gold, least new plumbing) to the live portfolio. Auto-clears once a silver
# target actually exists in TARGETS — no separate "already notified" state
# needed; if it keeps firing, silver genuinely hasn't been added yet.
MILESTONE_SILVER_EQUITY_USD = 100.0

# LLM balance alert: added 2026-09-04 after a real incident — a pass failed
# outright with DeepSeek returning 402 "Insufficient Balance" mid-run, and
# the only visible signal was the buried "(ERROR)" tag in the emailed
# subject line, easy to miss. Threshold is a judgment call, not derived from
# spend rate: DeepSeek's own per-pass cost is a few cents, so $2 leaves
# meaningful runway (several more passes) while still firing well before
# the account actually hits zero and starts silently failing every pass.
LLM_BALANCE_ALERT_THRESHOLD_USD = 2.0
LLM_BALANCE_ALERT_STATE_PATH = REPO_ROOT / "logs" / "llm_balance_alert_state.json"

# Trade-drought alert: the user asked to be told early if a symbol goes this
# long with zero new trades, rather than only finding out at a scheduled
# review. Added 2026-09-01 alongside the ATR volatility floor (fix 8) — that
# floor correctly refuses a noise-tight stop, which is good risk behavior,
# but if the $10 MAX_LOSS_PER_ORDER_USD cap is genuinely too tight for
# current volatility, the honest failure mode is the system going quiet for
# a long stretch instead of trading. This surfaces that early instead of
# waiting out the full observation period to discover it.
NO_TRADE_ALERT_DAYS = 7
NO_TRADE_ALERT_STATE_PATH = REPO_ROOT / "logs" / "no_trade_alert_state.json"

# Cap-fit alert: the user asked (2026-09-02) to be told the moment the $10
# risk cap's stop-distance budget actually clears a symbol's own ATR noise
# floor -- a finer-grained, immediate signal than the 7-day drought alert
# above, since gold's ATR was observed closing the gap naturally (from a
# $6.90 shortfall down to $0.40 within a day) and the user wanted to know
# as soon as it crosses, not after a week of silence.
CAP_FIT_ALERT_STATE_PATH = REPO_ROOT / "logs" / "cap_fit_alert_state.json"

# Risk-cap-vs-volatility history: pure observability (no alerting, no
# trading effect) so the eventual cap-revisit decision (see the ~1-month
# gold-only review) is backed by a real logged history of how often/how
# much the ATR floor actually bound, instead of the handful of ad hoc
# snapshots taken manually during this session. Appended once per live
# symbol per pass — never truncated, never read back by the loop itself.
RISK_CAP_GAP_LOG_PATH = REPO_ROOT / "logs" / "risk_cap_gap_history.jsonl"

# Singleton lock: refuse to start a second instance. 2026-08-25: a Task
# Scheduler AtLogOn relaunch and a manual startup.ps1 run overlapped, giving
# two independent loops that both reached a live-money trading decision on
# the same symbol at the same moment — exactly the race the module docstring
# above warns about (one process's "no open position" check can't see the
# other's about-to-be-placed order). See _acquire_singleton_lock().
LOCK_PATH = REPO_ROOT / "logs" / "committee_reporter.lock"

# Matches the redirect target startup.ps1 gives -RedirectStandardOutput —
# committee_reporter.py itself only logs to stdout (logging.basicConfig
# below); this constant exists purely so --status can read the same file
# back, not to configure logging output itself.
REPORTER_LOG_PATH = REPO_ROOT / "logs" / "reporter.log"

# No-weekend-hold rule, enforced in code rather than left to an LLM to
# remember: each committee pass is an independent subprocess with no memory
# of a prior pass's reasoning (see run_committee's docstring), so a rule one
# run derives ("stay flat into the weekend") isn't binding on the next run a
# few hours later. That gap produced a real loss: ticket 1046283982 was
# opened Fri 2026-08-28 18:50 UTC, held through the weekend, and gapped
# through its stop, closing Sat 22:01 UTC at -$13.39 (planned worst case was
# ~-$5.87 to -$9.74) — four hours after a *different* pass that same Friday
# had explicitly reasoned itself flat to avoid exactly this. WEEKEND_CUTOFF_
# UTC_HOUR matches the flat-by-20:00-UTC tripwire the committee itself has
# independently derived more than once.
WEEKEND_CUTOFF_UTC_HOUR = 20
WEEKEND_STATE_PATH = REPO_ROOT / "logs" / "weekend_state.json"

# Breakeven-stop management ("dual take-profit, Option A"): 0.01 lots is
# XAUUSDm's broker-enforced minimum AND step size (confirmed live via
# symbol_info — volume_min = volume_step = 0.01), so a position at that size
# cannot be partially closed; there is nothing smaller to scale out of. This
# is the substitute — once price has moved BREAKEVEN_TRIGGER_FRACTION of the
# way from entry to the planned take-profit, the stop moves to entry, so the
# trade can no longer lose (worst case becomes flat, best case is still the
# full original target) without touching position size or risk-per-trade.
# Checked on its own fast cadence, independent of the LLM-costly committee
# cadence — waiting up to --interval seconds to react would leave a trade
# sitting past its breakeven point unprotected for no reason, since this
# check is pure API reads plus at most one SLTP modify (zero LLM cost).
BREAKEVEN_POLL_SECONDS = 300
BREAKEVEN_TRIGGER_FRACTION = 0.5

# modify_position's own validation rejects an SL sitting exactly at entry
# ("stop_loss X must be below/above entry X") -- confirmed live 2026-09-03:
# ticket 1048921466 sat retrying and failing every BREAKEVEN_POLL_SECONDS for
# 2+ hours because the candidate below was literally `entry`, never
# protecting the position at all. BREAKEVEN_BUFFER_POINTS nudges the
# candidate a handful of points past entry, on the protective side, so the
# modify actually succeeds -- worst case becomes a few points of spread-level
# loss instead of the intended flat, not a silently-failing no-op.
BREAKEVEN_BUFFER_POINTS = 20

# Early-profit trail: the same 0.01-lot/no-partial-close constraint above
# also blocks literally banking a small early profit (e.g. $6-10) and
# letting the rest ride — there's nothing smaller to scale out of. Added
# 2026-09-03 at the user's request ("take the earliest profit... rather
# than waiting for the full take") after discussing the tradeoff: a HARD
# close at $6-10 would cap every winner there while losers still run to the
# full risk cap, a bad risk/reward skew. This is the trailing alternative
# instead: once unrealized profit reaches EARLY_PROFIT_TRIGGER_USD, the stop
# starts trailing behind price (re-evaluated, and only ever ratcheted
# forward, on every BREAKEVEN_POLL_SECONDS check below) — banking a growing
# profit floor without hard-capping a trade that keeps running toward its
# full take-profit. $8 is the midpoint of the user's $6-10 range.
#
# ATR-aware, not a fixed fraction of gained distance (the original version
# of this): trails behind live price by _atr_stop_floor(symbol) -- the same
# "sit outside normal noise" distance ATR_STOP_MULTIPLE x ATR already
# computes for the INITIAL stop-loss floor (see ATR_STOP_MULTIPLE's
# comment). A fixed-fraction trail was rejected after discussing the
# tradeoff: it can sit too close to price on a genuinely volatile
# instrument (gets whipsawed out by ordinary noise the moment it arms,
# turning a real trend into a premature small win) or needlessly loose on a
# calm one. Reusing the ATR floor adapts the trailing distance to each
# instrument's actual current noise level instead of guessing one constant.
EARLY_PROFIT_TRIGGER_USD = 8.0

# Time-decay stop: closes a real gap found 2026-09-08. A PM decision routinely
# commits to a same-day "hard flat by HH:MM UTC" deadline, but nothing in
# this script ever enforced that -- only the NEXT scheduled ~2h committee
# pass could notice and close it, which can land well past the stated
# deadline (or, for a target that came back a genuine WAIT, not run again on
# that symbol for hours). Meanwhile the two rules above already ratchet a
# WINNING position's locked-in floor up as it moves favorably -- a losing
# position got no equivalent treatment and kept its full original stop
# distance live indefinitely. This is asymmetric: winners get cut early by
# nothing more than next-cycle timing luck, losers keep full risk live
# until someone gets around to closing them.
#
# MAX_HOLD_HOURS is a code-owned ceiling (not parsed from the PM's own
# prose deadline -- far more robust than trying to extract a machine-
# readable time from free text). Past TIME_DECAY_START_FRACTION of that
# window, each BREAKEVEN_POLL_SECONDS check proposes tightening the stop to
# the same ATR-floor distance Rule 2 already trails winners at (see
# _atr_stop_floor) -- gated on elapsed time instead of unrealized profit,
# so it only ever tightens (the shared "improves" check below still applies)
# and is a no-op once there's nothing left to protect. At MAX_HOLD_HOURS the
# position is flattened unconditionally regardless of P&L -- this is this
# script's first actual automated intraday time-stop; previously only the
# weekend-flatten and the equity-drawdown breaker closed anything outside a
# position's own SL/TP. 40h and 0.75 are starting points, not derived from
# data, but 40h specifically was checked against the currently-open AUDUSDm
# position's own PM-stated deadline ("hard time-stop ... Tue ~21:00 UTC",
# ~36.7h from its 2026-09-07 08:17 UTC open) before this rule went live
# 2026-09-08, so rollout doesn't retroactively cut that trade short of its
# own plan -- a tighter default (30h) would have started tightening its
# stop within minutes of deploy and flattened it ~6h before that stated
# deadline. Override per-target via a "max_hold_hours" key on that target's
# `trade` dict if a symbol needs a different window.
MAX_HOLD_HOURS = 40.0
TIME_DECAY_START_FRACTION = 0.75


def _kill_process_tree(pid: int) -> None:
    """Kill a process and every descendant it spawned (Windows-only, via taskkill /T).

    A plain Popen.kill() only signals the direct child. `cli run` is itself a
    venv-shim launch that re-execs into a second interpreter process (the same
    pattern as committee_reporter.py's own launch) — so the direct child has
    already spawned a grandchild by the time a run is underway. Killing only
    the direct child leaves that grandchild alive, still holding the stdout/
    stderr pipe's write end open, which makes the follow-up communicate()
    block forever waiting for EOF that will never come.

    This is not hypothetical: on 2026-08-31 a run hung for 7+ hours past its
    1-hour RUN_TIMEOUT_SECONDS budget with no timeout ever logged, because
    run_committee() was stuck inside communicate(), not merely slow to check
    the clock. /T kills the whole descendant tree, so the drain call right
    after this always terminates.
    """
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
    except Exception:
        logger.exception("failed to kill process tree for pid %s", pid)


def _pid_is_alive(pid: int) -> bool:
    """Windows-only liveness check via OpenProcess (no extra dependency)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _process_creation_time(pid: int) -> int | None:
    """Return `pid`'s exact process creation time (raw Windows FILETIME), or None.

    Paired with the PID in the lock file (see _acquire_singleton_lock) because
    a PID alone is not a stable process identity on Windows: PIDs are recycled
    quickly, so after a crash/reboot a dead reporter's old PID can be handed
    to an unrelated process within minutes (observed in practice: a location-
    service process inherited a dead reporter's PID moments after a reboot,
    and a liveness-only check treated it as "still running" forever after).
    Two different processes essentially never share both the same PID and the
    same to-the-100ns creation timestamp, so comparing both together — not
    PID alone — is what makes a stale lock detectable and self-healing.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        )
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _read_lock_identity() -> tuple[int, int | None] | None:
    """Parse the lock file as (pid, creation_time). None if missing/unreadable.

    ``creation_time`` is None for a pre-upgrade bare-PID lock (written before
    this identity check existed) — callers fall back to a liveness-only check
    for that one transitional read; every lock written from here on carries
    its creation time.
    """
    if not LOCK_PATH.exists():
        return None
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    pid_part, sep, created_part = raw.partition(":")
    try:
        pid = int(pid_part)
    except ValueError:
        return None
    if not sep:
        return pid, None
    try:
        return pid, int(created_part)
    except ValueError:
        return pid, None


def _lock_identity_is_alive(identity: tuple[int, int | None]) -> bool:
    """True if the lock's recorded (pid, creation_time) still matches a live process.

    Falls back to a liveness-only check when creation_time is unavailable (a
    pre-upgrade lock, or GetProcessTimes failing) — less precise, but matches
    the pre-fix behavior rather than refusing to ever reclaim the lock.
    """
    pid, created = identity
    if created is None:
        return _pid_is_alive(pid)
    current = _process_creation_time(pid)
    return current is not None and current == created


def _acquire_singleton_lock() -> bool:
    """Claim the lock, refusing to start if another instance already holds it.

    A stale lock (no live process matching its recorded pid+creation_time)
    self-heals — reclaimed automatically rather than requiring manual
    cleanup, since an abrupt kill (Stop-Process, a crash, a reboot) never
    runs an exit handler. See _process_creation_time for why the lock
    identity is pid+creation_time, not PID alone.

    Returns:
        True if the lock was acquired (safe to proceed). False if another
        instance is genuinely running — the caller must exit without doing
        anything: guessing "it's probably fine" is exactly the failure mode
        this guards against.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity = _read_lock_identity()
    if identity is not None and _lock_identity_is_alive(identity):
        return False
    my_pid = os.getpid()
    my_created = _process_creation_time(my_pid)
    created_token = "" if my_created is None else str(my_created)
    LOCK_PATH.write_text(f"{my_pid}:{created_token}", encoding="utf-8")
    return True


def _symbol_live_quote(symbol: str, connection: str) -> dict | None:
    """Live bid/ask for `symbol` from the broker actually being traded.

    A committee's own research tools (yfinance/tushare/etc.) have zero
    visibility into the broker feed an order will actually execute against,
    and routinely reference a DIFFERENT instrument entirely (e.g. GC=F
    COMEX gold futures vs. MT5's XAUUSDm CFD) that can diverge by a lot —
    discovered when a committee's stop/target levels sat $60+ away from the
    live tradable price because its research price was for a different
    instrument. Injecting this as ground truth lets the prompt tell the
    committee explicitly which price its executable levels must anchor to.

    Returns:
        {"bid": float, "ask": float} or None if the quote can't be read
        (fails open on the prompt side — the agent still has its own
        trading_quote tool to fall back on).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_quote

    try:
        result = get_quote(symbol, connection)
    except Exception:
        return None
    quote = result.get("quote") or {}
    bid, ask = quote.get("bid"), quote.get("ask")
    if not bid or not ask:
        return None
    return {"bid": float(bid), "ask": float(ask)}


def _effective_max_loss_usd(connection: str) -> float:
    """This pass's per-order risk budget: the tighter of the mandate ceiling
    and the live portfolio-policy figure (equity * PORTFOLIO_RISK_FRACTION /
    PORTFOLIO_MAX_CONCURRENT_TRADES).

    Approximates a portfolio-level aggregate cap the mandate gate doesn't
    implement (see MAX_LOSS_PER_ORDER_USD's own comment) by tightening the
    per-order budget itself when equity is small enough that the policy asks
    for less than the flat ceiling. Reads live equity via the same
    connection/get_account call _live_circuit_breaker_check already uses.
    Fails open to the static MAX_LOSS_PER_ORDER_USD ceiling on any read
    error or a non-live connection — a stale/wider prompt-side budget is
    still safe, since it can only ask for a stop up to what the gate would
    allow anyway, never past it.
    """
    if connection not in LIVE_CONNECTIONS:
        return MAX_LOSS_PER_ORDER_USD

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_account

    try:
        equity = float(get_account(connection)["account"]["equity"])
    except Exception:
        return MAX_LOSS_PER_ORDER_USD
    if equity <= 0:
        return MAX_LOSS_PER_ORDER_USD

    portfolio_budget = equity * PORTFOLIO_RISK_FRACTION / PORTFOLIO_MAX_CONCURRENT_TRADES
    return min(MAX_LOSS_PER_ORDER_USD, portfolio_budget)


def _max_stop_distance(symbol: str, lots: float, budget_usd: float = MAX_LOSS_PER_ORDER_USD) -> float | None:
    """Max stop-loss distance (price units) that stays within ``budget_usd``.

    ``budget_usd / (contract_size * lots)`` — the same formula the mandate
    gate itself uses (in reverse) to compute planned loss, applied against
    whatever risk budget the caller passes (the flat MAX_LOSS_PER_ORDER_USD
    ceiling by default, or the tighter live-equity-based figure from
    _effective_max_loss_usd). Returns None if the contract size can't be
    read (fails open on the prompt side — the gate still enforces the real
    cap regardless of what the prompt says).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        size = mt5_sdk.contract_size(symbol)
    except Exception:
        return None
    if not size or size <= 0 or lots <= 0:
        return None
    return budget_usd / (size * lots)


def _atr_stop_floor(symbol: str) -> float | None:
    """Minimum stop-loss distance (price units) to sit outside normal noise, or None if unavailable.

    ATR_STOP_MULTIPLE x ATR(ATR_LOOKBACK_BARS) on ATR_PERIOD bars, computed
    from the most recent real bars — pure historical-bar math, zero LLM cost,
    same data source and connector-default-config pattern as
    ``_max_stop_distance``. Fails open (returns None) on any read error; a
    pass this can't compute for just proceeds without the floor rather than
    blocking trading over a transient data-feed hiccup.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        bars = mt5_sdk.get_historical_bars(symbol, period=ATR_PERIOD, limit=ATR_LOOKBACK_BARS + 1)["bars"]
    except Exception:
        return None

    bars = [b for b in bars if b.get("high") is not None and b.get("low") is not None and b.get("close") is not None]
    if len(bars) < 2:
        return None

    true_ranges = [
        max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"] - bars[i - 1]["close"]),
        )
        for i in range(1, len(bars))
    ]
    atr = sum(true_ranges) / len(true_ranges)
    return atr * ATR_STOP_MULTIPLE if atr > 0 else None


def _spread_stop_floor(quote: dict | None) -> float | None:
    """Minimum stop-loss distance (price units) to keep the live spread's own
    share of that stop under 1/MIN_STOP_TO_SPREAD_RATIO, or None if unavailable.

    Takes the same ``{"bid": float, "ask": float}`` dict ``_build_prompt``
    already fetched via ``_symbol_live_quote`` for the prompt's quote_fact —
    no extra broker round trip. Returns None on a missing/non-positive
    spread (fails open, same convention as ``_atr_stop_floor``).
    """
    if not quote:
        return None
    spread = quote.get("ask", 0) - quote.get("bid", 0)
    return spread * MIN_STOP_TO_SPREAD_RATIO if spread > 0 else None


def _symbol_position_summary(symbol: str, connection: str) -> dict:
    """Verified (not LLM-reported) count/side of OUR OWN open positions in `symbol`.

    Queried directly via the connector service before the prompt is even
    built, so the cap/direction check the agent is given is ground truth,
    not something it has to count correctly itself from a tool result.
    Filtered to OUR_MAGIC — on an account also running a separate signal-
    service EA (or manual trades) on the same symbol, counting every position
    regardless of source would let someone else's position silently eat our
    own same-direction cap (or worse, look like a direction conflict that
    blocks us from ever trading). The signal service's own activity is
    reported separately, as context, by _signal_service_activity.

    Returns:
        {"count": int, "side": "buy" | "sell" | "mixed" | None}. ``"mixed"``
        only happens if positions predating this rule already conflict.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_positions

    result = get_positions(connection)
    rows = [
        p for p in result.get("positions", [])
        if p.get("symbol") == symbol and p.get("magic") == OUR_MAGIC
    ]
    if not rows:
        return {"count": 0, "side": None}
    sides = {row.get("side") for row in rows}
    side = sides.pop() if len(sides) == 1 else "mixed"
    return {"count": len(rows), "side": side}


def _signal_service_activity(symbol: str, connection: str) -> str | None:
    """Summarize a separately-running signal-service EA's activity on `symbol`.

    Anything on this MT5 account whose `magic` isn't OUR_MAGIC is presumed to
    be the user's own separately-running signal service (or a manual trade).
    Returns a plain-text summary of its currently open position(s) plus its
    most recent closed deals (capped — see SIGNAL_ACTIVITY_MAX_LINES), for the
    committee prompt — context only, per the user's own framing: the
    committee weighs it alongside its own research, it does not mirror or
    fade it automatically. Returns None when there's nothing to report (keeps
    a quiet pass's prompt clean) or if the reads fail (fails open — this is
    supplementary context, not a safety check, so a read failure should never
    block the pass).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_open_orders, get_positions

    try:
        positions = get_positions(connection).get("positions", [])
    except Exception:
        positions = []
    open_lines = [
        f"  OPEN {p.get('side', '?').upper()} {p.get('volume')} lots @ {p.get('price_open')} "
        f"(current P&L {p.get('profit')}, comment={p.get('comment') or 'none'})"
        for p in positions
        if p.get("symbol") == symbol and p.get("magic") != OUR_MAGIC
    ][:SIGNAL_ACTIVITY_MAX_LINES]

    try:
        executions = get_open_orders(connection, include_executions=True).get("executions", [])
    except Exception:
        executions = []
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=SIGNAL_LOOKBACK_DAYS)).isoformat()
    candidates = [
        d for d in executions
        if d.get("symbol") == symbol and d.get("magic") != OUR_MAGIC and (d.get("time") or "") >= cutoff_iso
    ]
    candidates.sort(key=lambda d: d.get("time") or "", reverse=True)  # most recent first
    remaining = max(0, SIGNAL_ACTIVITY_MAX_LINES - len(open_lines))
    closed_lines = [
        f"  CLOSED {d.get('side', '?').upper()} {d.get('volume')} lots @ {d.get('price')}, "
        f"P&L {d.get('profit')} on {d.get('time')} (comment={d.get('comment') or 'none'})"
        for d in candidates[:remaining]
    ]

    lines = open_lines + closed_lines
    if not lines:
        return None
    return (
        f'A separate signal service/EA is also active on this account. Its activity in "{symbol}" '
        f"(informational only — weigh it as one more data point alongside your own research; do not "
        f"simply mirror or fade it; most recent {len(lines)} shown):\n" + "\n".join(lines)
    )


def _read_journal() -> list[dict]:
    try:
        return json.loads(TRADE_JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _write_journal(entries: list[dict]) -> None:
    TRADE_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADE_JOURNAL_PATH.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def _extract_placed_order(run_id: str) -> dict | None:
    """Pull the verified trading_place_order RESULT (not the LLM's own report).

    Reads the trace's real tool_result record — the connector's own response
    (ticket/side/quantity/fill_price/stop_loss/take_profit) — rather than
    trusting the committee's free-text summary of what it did.

    Returns:
        The connector's place_order response dict on a successful placement,
        or None if this run didn't place one.
    """
    for e in _trace_entries(run_id):
        if e.get("type") != "tool_result" or e.get("tool") != "trading_place_order":
            continue
        raw = e.get("result")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "ok":
            return parsed
    return None


def _blocked_order_note(run_id: str) -> str | None:
    """Surface the REAL reason a trading_place_order attempt didn't go through.

    Complements _extract_placed_order: when the tool was called but its
    result wasn't status=="ok" (mandate/gate denial, MT5 rejection, bad
    args), this pulls the actual requested args plus the gate/error reason
    into the emailed report — including whatever quantity/order_type the
    committee actually requested, which may not match what it was told to
    use (regression: a hallucinated 1.0-lot order, 100x the intended 0.01,
    was denied by the mandate gate but was invisible in the report until this
    was added — the committee's own free-text summary doesn't reliably
    surface a code-verified reason).
    """
    entries = _trace_entries(run_id)
    call_args_by_id = {
        e.get("call_id"): e.get("args") or {}
        for e in entries
        if e.get("type") == "tool_call" and e.get("tool") == "trading_place_order"
    }
    for e in entries:
        if e.get("type") != "tool_result" or e.get("tool") != "trading_place_order":
            continue
        raw = e.get("result")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "ok":
            continue  # a real success is reported via _extract_placed_order instead
        args = call_args_by_id.get(e.get("call_id"), {})
        reason = (parsed.get("reason") or parsed.get("error") or "unknown") if isinstance(parsed, dict) else "unknown"
        size = f"{args.get('quantity')} lots" if args.get("quantity") is not None else f"notional {args.get('notional')}"
        return (
            f"\n\n[AUTOMATED CHECK] A trading_place_order call did NOT go through: requested "
            f"{args.get('side')} {size} {args.get('symbol')} ({args.get('order_type', 'market')}) "
            f"— reason: {reason}"
        )
    return None


def _journal_record_open(symbol: str, connection: str, order: dict) -> None:
    """Append a new open-trade record from a verified place_order result."""
    entries = _read_journal()
    entries.append({
        "ticket": str(order.get("order_id") or ""),
        "symbol": symbol,
        "connection": connection,
        "side": order.get("side"),
        "lots": order.get("quantity"),
        "entry_price": order.get("fill_price"),
        "stop_loss": order.get("stop_loss"),
        "take_profit": order.get("take_profit"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    })
    _write_journal(entries)


def _classify_excursion(entry: dict, deal: dict) -> dict:
    """Compute max favorable/adverse excursion for a just-closed trade, from real price bars.

    Zero LLM cost — pure historical-bar analysis. Returns {} if bars can't be
    fetched or times can't be parsed (fails open; never blocks reconciliation
    over a missing analytics extra).
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.connectors.mt5 import sdk as mt5_sdk

    try:
        opened = datetime.fromisoformat(entry["opened_at"])
        closed = datetime.fromisoformat(deal["time"])
        entry_price = float(entry["entry_price"])
        stop_loss = float(entry["stop_loss"])
        is_buy = entry["side"] == "buy"
    except (KeyError, TypeError, ValueError):
        return {}

    try:
        bars = mt5_sdk.get_historical_bars_range(
            entry["symbol"],
            opened - timedelta(minutes=15),
            closed + timedelta(minutes=15),
            period=EXCURSION_BAR_PERIOD,
        )["bars"]
    except Exception:
        return {}
    if not bars:
        return {}

    try:
        highs = [b["high"] for b in bars if b.get("high") is not None]
        lows = [b["low"] for b in bars if b.get("low") is not None]
        if not highs or not lows:
            return {}
        if is_buy:
            favorable = max(highs) - entry_price
            adverse = entry_price - min(lows)
        else:
            favorable = entry_price - min(lows)
            adverse = max(highs) - entry_price
    except (TypeError, ValueError):
        return {}

    stop_distance = abs(entry_price - stop_loss)
    outcome = entry.get("outcome")
    reversal = (
        stop_distance > 0
        and favorable >= REVERSAL_THRESHOLD_FRACTION * stop_distance
        and outcome in ("loss", "breakeven")
    )
    return {
        "max_favorable_pts": round(favorable, 3),
        "max_adverse_pts": round(adverse, 3),
        "excursion_tag": "reversal" if reversal else "clean",
    }


def _journal_reconcile_closed(symbol: str, connection: str) -> None:
    """Mark journal entries closed once their position is no longer open.

    Pure API reads — no LLM tokens spent. Matches by ticket against current
    open positions AND still-resting pending orders (either -> left alone)
    and, failing that, against the real CLOSING deal (magic == OUR_MAGIC,
    position_id == our ticket, entry != 0) to record the real exit
    price/P&L — never trusts the committee's own report of what happened.
    Fails open on any read error (never corrupts the journal over a
    transient connector failure).

    Regression: MT5's closing deal carries `order == 0` (no usable back-
    reference to the opening order); the only reliable link is `position_id`.
    Matching on order_id/deal-ticket instead (as this originally did) grabbed
    the OPENING deal — which always reports profit=0.0 — and recorded a real
    +$29 take-profit win as a false "breakeven".

    Regression: a journal entry recorded for a pending limit/stop order (see
    _journal_record_open) never appears in get_positions() until it fills —
    that's true for its entire resting lifetime, not just a brief race, so
    the JOURNAL_RECONCILE_GRACE window doesn't help. Checking only
    get_positions() marked resting orders "closed"/"unknown" as soon as the
    grace period elapsed, even while they stayed live on the broker for
    hours (tickets 1048913897 on 2026-09-05, 1050385917 on 2026-09-07 — both
    manually corrected after the fact). get_open_orders()'s own response
    already carries the resting-order list alongside `executions` (same
    call, no extra round trip) — folding it into the "still open" set fixes
    this at the source instead of hand-patching the journal again next time.
    """
    entries = _read_journal()
    open_entries = [e for e in entries if e.get("status") == "open" and e.get("symbol") == symbol]
    if not open_entries:
        return

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_open_orders, get_positions

    try:
        live_tickets = {str(p.get("ticket")) for p in get_positions(connection).get("positions", [])}
    except Exception:
        return

    try:
        orders_resp = get_open_orders(connection, include_executions=True)
    except Exception:
        orders_resp = {}
    live_tickets |= {str(o.get("order_id")) for o in orders_resp.get("open_orders", [])}
    executions = orders_resp.get("executions", [])
    our_closing_deals = [
        d for d in executions
        if d.get("magic") == OUR_MAGIC and d.get("entry") not in (0, None)
    ]
    # A position could in principle have more than one closing deal (partial
    # close); keep the latest one per position_id.
    latest_close_by_position: dict[str, dict] = {}
    for d in our_closing_deals:
        key = str(d.get("position_id") or "")
        if not key:
            continue
        existing = latest_close_by_position.get(key)
        if existing is None or (d.get("time") or "") >= (existing.get("time") or ""):
            latest_close_by_position[key] = d

    now = datetime.now(timezone.utc)
    changed = False
    for entry in open_entries:
        ticket = str(entry.get("ticket"))
        if ticket in live_tickets:
            continue  # still open
        try:
            opened_at = datetime.fromisoformat(entry["opened_at"])
        except (KeyError, TypeError, ValueError):
            opened_at = None
        if opened_at is not None and (now - opened_at) < JOURNAL_RECONCILE_GRACE:
            continue  # too soon to trust a "not open" read — see JOURNAL_RECONCILE_GRACE
        deal = latest_close_by_position.get(ticket)
        entry["status"] = "closed"
        if deal:
            # The real close time, from the broker's own deal record — NOT
            # "now" (when this reconciliation happened to run, up to 2 hours
            # after the actual close). Getting this right matters: the
            # excursion analysis below needs the real window to look at.
            entry["closed_at"] = deal.get("time") or datetime.now(timezone.utc).isoformat()
            profit = deal.get("profit")
            entry["exit_price"] = deal.get("price")
            entry["profit"] = profit
            entry["outcome"] = "win" if (profit or 0) > 0 else ("loss" if (profit or 0) < 0 else "breakeven")
            entry.update(_classify_excursion(entry, deal))
        else:
            entry["closed_at"] = datetime.now(timezone.utc).isoformat()
            entry["outcome"] = "unknown"  # closed, but couldn't match the closing deal
        changed = True

    if changed:
        _write_journal(entries)


def _journal_summary_text(symbol: str) -> str | None:
    """One terse line of our own recent track record on `symbol`, or None if empty.

    Deliberately minimal (a single stats line, not a replay of past reasoning)
    — this was an explicit "minimum added tokens" requirement.
    """
    closed = [e for e in _read_journal() if e.get("symbol") == symbol and e.get("status") == "closed"]
    if not closed:
        return None
    recent = closed[-JOURNAL_SUMMARY_WINDOW:]
    scored = [e for e in recent if e.get("outcome") in ("win", "loss")]
    wins = sum(1 for e in scored if e["outcome"] == "win")
    losses = sum(1 for e in scored if e["outcome"] == "loss")
    net = sum(float(e.get("profit") or 0) for e in scored)
    last = recent[-1]
    reversals = sum(1 for e in scored if e.get("excursion_tag") == "reversal")
    # Only mentioned when it's actually happened — keeps a clean run's prompt
    # from growing for no reason (same "minimum tokens" discipline as the
    # rest of this block).
    reversal_note = (
        f" {reversals} of these moved favorably before reversing to a loss — "
        f"a stop-management issue, not an entry-quality one."
        if reversals else ""
    )
    return (
        f"Your own recent {symbol} track record (last {len(recent)} closed): {wins}W/{losses}L, "
        f"net {net:+.2f}. Last: {str(last.get('side', '?')).upper()} {last.get('outcome', '?')} "
        f"({last.get('profit', '?')}).{reversal_note}"
    )


def _read_live_baseline() -> dict:
    try:
        return json.loads(LIVE_BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_live_baseline(data: dict) -> None:
    LIVE_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_BASELINE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _live_circuit_breaker_check(trade: dict) -> str | None:
    """Auto-halt + flatten live trading if equity drops too far today.

    Returns a message to fold into the emailed report (and to signal the
    caller to run this pass research-only, no trade attempt) when trading was
    skipped this pass, or ``None`` when normal live trading may proceed. A
    no-op for demo connections (not in LIVE_CONNECTIONS).
    """
    connection = trade["connection"]
    if connection not in LIVE_CONNECTIONS:
        return None

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set, trip_halt
    from src.trading.connectors.mt5 import sdk as mt5_sdk
    from src.trading.profiles import profile_by_id
    from src.trading.service import get_account, get_positions

    broker = "mt5"  # every mt5-live-* profile shares this mandate/halt broker key

    if halt_flag_set(broker):
        return (
            f"[LIVE CIRCUIT BREAKER] {broker} live trading is currently HALTED "
            f"(kill switch already tripped) — no live order was attempted this pass."
        )

    try:
        account = get_account(connection)
        equity = float(account["account"]["equity"])
    except Exception as exc:
        return f"[LIVE CIRCUIT BREAKER] could not read live account equity this pass ({exc}) — skipping live trading as a precaution."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    baseline = _read_live_baseline()
    if baseline.get("date") != today:
        baseline = {"date": today, "equity": equity}
        _write_live_baseline(baseline)
    baseline_equity = float(baseline.get("equity") or equity)

    if baseline_equity <= 0:
        return None
    drawdown = (baseline_equity - equity) / baseline_equity
    if drawdown < LIVE_DRAWDOWN_HALT_PCT:
        return None

    reason = f"daily equity drawdown {drawdown:.0%} (baseline ${baseline_equity:.2f} -> ${equity:.2f})"
    trip_halt(by="cli", reason=reason, broker=broker)

    # Flatten every one of OUR live positions on this connection, not just
    # trade["symbol"] (the one target whose pass happened to detect the
    # drawdown) -- an account-wide equity trip is a portfolio-level backstop
    # and must not leave other live symbols' exposure open. Same
    # all-TARGETS-on-this-connection + OUR_MAGIC pattern as
    # _weekend_flatten_and_notify.
    closed = []
    try:
        live_symbols = {
            spec["trade"]["symbol"]
            for spec in TARGETS
            if spec.get("trade") and spec["trade"]["connection"] == connection
        }
        positions = get_positions(connection).get("positions", [])
        ours = [
            p for p in positions
            if p.get("symbol") in live_symbols and p.get("magic") == OUR_MAGIC
        ]
        if ours:
            profile = profile_by_id(connection)
            config = mt5_sdk.build_config(profile.config, {})
            for pos in ours:
                ticket = pos.get("ticket")
                result = mt5_sdk.close_position(config, ticket=ticket)
                closed.append(f"{pos.get('symbol')} ticket {ticket}: {result.get('status')}")
    except Exception as exc:
        closed.append(f"flatten attempt raised: {exc}")

    return (
        f"[LIVE CIRCUIT BREAKER TRIPPED] {reason}. Live trading for {broker} is now HALTED "
        f"(kill switch) until manually cleared (src.live.halt.clear_halt). "
        f"Flatten attempt: {'; '.join(closed) if closed else 'no open position found'}."
    )


def _in_weekend_window(now: datetime) -> bool:
    """True from WEEKEND_CUTOFF_UTC_HOUR on Friday through end of Sunday (UTC)."""
    if now.weekday() in (5, 6):  # Sat, Sun
        return True
    return now.weekday() == 4 and now.hour >= WEEKEND_CUTOFF_UTC_HOUR  # Fri evening


def _read_weekend_state() -> dict:
    try:
        return json.loads(WEEKEND_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_weekend_state(data: dict) -> None:
    WEEKEND_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEEKEND_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _weekend_flatten_and_notify() -> None:
    """Deterministically flatten our own live positions heading into the
    weekend, and send exactly one status email per calendar week for it.

    Called every pass while _in_weekend_window() is true — cheap and
    idempotent (a pass with nothing open is a no-op, pure API reads plus at
    most one close_position call), so there's no harm re-checking it every
    loop interval all weekend. The email is deliberately throttled to once
    per ISO week (via WEEKEND_STATE_PATH) so an unattended weekend doesn't
    spam a report every --interval seconds; a flatten actually happening
    always gets emailed regardless of that throttle, since that's new
    information even if the week's notice already went out.
    """
    week_key = datetime.now(timezone.utc).strftime("%G-W%V")
    already_notified = _read_weekend_state().get("week") == week_key

    closed_lines: list[str] = []
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue

        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.connectors.mt5 import sdk as mt5_sdk
        from src.trading.profiles import profile_by_id
        from src.trading.service import get_positions

        try:
            positions = get_positions(trade["connection"]).get("positions", [])
        except Exception:
            logger.exception("weekend flatten: could not read positions for %s", trade["symbol"])
            continue

        ours = [
            p for p in positions
            if p.get("symbol") == trade["symbol"] and p.get("magic") == OUR_MAGIC
        ]
        if not ours:
            continue

        try:
            profile = profile_by_id(trade["connection"])
            config = mt5_sdk.build_config(profile.config, {})
        except Exception:
            logger.exception("weekend flatten: could not build config for %s", trade["connection"])
            continue

        for pos in ours:
            ticket = pos.get("ticket")
            try:
                result = mt5_sdk.close_position(config, ticket=ticket)
            except Exception as exc:
                closed_lines.append(f"FAILED to close ticket {ticket} on {trade['symbol']}: {exc}")
                logger.exception("weekend flatten: close_position raised for ticket %s", ticket)
                continue
            if result.get("status") == "ok":
                closed_lines.append(
                    f"Closed {pos.get('side', '?').upper()} {result.get('closed_volume', pos.get('volume'))} lots "
                    f"{trade['symbol']} @ {result.get('fill_price', '?')} "
                    f"(open P&L at trigger ≈ {pos.get('profit')})"
                )
                logger.info("weekend flatten: closed ticket %s on %s", ticket, trade["symbol"])
            else:
                closed_lines.append(f"FAILED to close ticket {ticket} on {trade['symbol']}: {result.get('error')}")
                logger.error("weekend flatten: close_position failed for ticket %s: %s", ticket, result.get("error"))

    if already_notified and not closed_lines:
        return

    body_lines = ["Market closed for the weekend — no committee runs until Monday (UTC)."]
    if closed_lines:
        body_lines.append("")
        body_lines.append("Positions flattened ahead of the weekend (no-weekend-hold rule, code-enforced):")
        body_lines.extend(f"  - {line}" for line in closed_lines)
    else:
        body_lines.append("No open positions of ours to flatten.")

    try:
        send_email("[Vibe-Trading] Weekend status — market closed", _status_header() + "\n".join(body_lines))
    except Exception:
        logger.exception("failed to send weekend status email")

    _write_weekend_state({"week": week_key})


def _profit_protection_check() -> None:
    """Ratchet a live position's stop toward locked-in profit, and enforce a time-stop.

    Pure code, no LLM: reads the position's own entry/SL/TP (as submitted by
    the committee's original order) and the live mark price, and issues at
    most one TRADE_ACTION_SLTP modify (or one close) per check. Three
    independent rules each propose a candidate stop; only the more
    protective of the proposed candidates (closer to the live price, on the
    favorable side) is ever applied, and only when that's actually an
    improvement over the current stop — so the modify path never loosens a
    stop, and a position already past every trigger is a no-op on every
    later check:

      1. Breakeven-at-halfway (original rule): once price is
         BREAKEVEN_TRIGGER_FRACTION of the way from entry to the planned
         take-profit, candidate stop = entry +/- BREAKEVEN_BUFFER_POINTS
         (see its comment for why not exactly entry). Worst case becomes
         near-flat.
      2. ATR-aware early-profit trail (see EARLY_PROFIT_TRIGGER_USD's
         comment): once unrealized profit reaches EARLY_PROFIT_TRIGGER_USD,
         candidate stop = price minus _atr_stop_floor(symbol) for a buy (plus,
         for a sell) — trails price at a distance sized to the instrument's
         own current noise level, instead of the flat breakeven floor above.
      3. Time-decay (see MAX_HOLD_HOURS' comment): past TIME_DECAY_START_
         FRACTION of MAX_HOLD_HOURS since the position opened (per the
         broker's own reported open time, not the journal's), candidate
         stop = the same ATR-floor distance as rule 2, gated on elapsed
         time instead of profit — a losing position's maximum remaining
         risk shrinks as its deadline nears too, not just a winner's locked-
         in floor. At MAX_HOLD_HOURS the position is flattened
         unconditionally, regardless of P&L or the other two rules.

    Positions with no SL or no TP attached are left alone for rules 1-2 and
    rule 3's tightening half (nothing to compute a halfway point from, the
    $ trigger needs a stop-derived contract size, and modifying the stop
    safely requires re-sending the existing tp unchanged — see
    modify_position's own warning) — but rule 3's unconditional flatten at
    MAX_HOLD_HOURS still applies regardless, since it only needs the
    position's own open time, and a stray naked (no-SL/TP) position is
    exactly the case that most needs a backstop. See
    BREAKEVEN_TRIGGER_FRACTION's comment for why rule 1 exists instead of a
    literal partial-close dual take-profit.
    """
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue

        sys.path.insert(0, str(AGENT_DIR))
        from src.trading.connectors.mt5 import sdk as mt5_sdk
        from src.trading.profiles import profile_by_id
        from src.trading.service import get_positions

        try:
            positions = get_positions(trade["connection"]).get("positions", [])
        except Exception:
            logger.exception("profit protection check: could not read positions for %s", trade["symbol"])
            continue

        ours = [
            p for p in positions
            if p.get("symbol") == trade["symbol"] and p.get("magic") == OUR_MAGIC
        ]
        if not ours:
            continue

        try:
            profile = profile_by_id(trade["connection"])
            config = mt5_sdk.build_config(profile.config, {})
        except Exception:
            logger.exception("profit protection check: could not build config for %s", trade["connection"])
            continue

        for pos in ours:
            # Rule 3a: unconditional flatten at MAX_HOLD_HOURS (see its own
            # comment). Checked first and independent of the entry/sl/tp/
            # price gate below -- needs only the broker's own reported open
            # time, so it still backstops a stray naked position that the
            # rest of this function can't otherwise touch.
            max_hold_hours = trade.get("max_hold_hours", MAX_HOLD_HOURS)
            elapsed_hours = None
            opened_raw = pos.get("time")
            if opened_raw:
                try:
                    elapsed_hours = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(str(opened_raw))
                    ).total_seconds() / 3600.0
                except (TypeError, ValueError):
                    elapsed_hours = None

            if elapsed_hours is not None and elapsed_hours >= max_hold_hours:
                try:
                    result = mt5_sdk.close_position(config, ticket=pos.get("ticket"))
                except Exception:
                    logger.exception(
                        "profit protection check: time-stop close_position raised for ticket %s", pos.get("ticket"),
                    )
                    continue
                if result.get("status") == "ok":
                    logger.info(
                        "profit protection check: time-stop flattened ticket %s on %s after %.1fh "
                        "(open P&L at trigger %s)",
                        pos.get("ticket"), trade["symbol"], elapsed_hours, pos.get("profit"),
                    )
                else:
                    logger.error(
                        "profit protection check: time-stop close_position failed for ticket %s: %s",
                        pos.get("ticket"), result.get("error"),
                    )
                continue  # this ticket is handled either way -- nothing else applies to it this pass

            entry, sl, tp, price = pos.get("price_open"), pos.get("stop_loss"), pos.get("take_profit"), pos.get("price_current")
            side = pos.get("side")
            if entry is None or sl is None or tp is None or price is None or side not in ("buy", "sell"):
                continue
            entry, sl, tp, price = float(entry), float(sl), float(tp), float(price)
            is_buy = side == "buy"

            # Rule 1: breakeven-at-halfway. Candidate sits BREAKEVEN_BUFFER_
            # POINTS past entry on the protective side, not exactly at entry
            # -- modify_position rejects an SL exactly at entry, see
            # BREAKEVEN_BUFFER_POINTS's comment. Fails open (skips this rule
            # only, trail below still applies) if the point-size lookup
            # fails.
            breakeven_candidate = None
            halfway = entry + (tp - entry) * BREAKEVEN_TRIGGER_FRACTION
            reached_halfway = price >= halfway if is_buy else price <= halfway
            if reached_halfway:
                try:
                    point = mt5_sdk.point_size(trade["symbol"])
                except Exception:
                    point = None
                if point and point > 0:
                    buffer = point * BREAKEVEN_BUFFER_POINTS
                    breakeven_candidate = entry - buffer if is_buy else entry + buffer

            # Rule 2: ATR-aware early-profit trail. Needs contract size to
            # convert the $ trigger into a price distance, and the ATR
            # floor for the trailing distance -- fails open (skips this
            # rule only, breakeven above still applies) if either lookup
            # fails.
            trail_candidate = None
            try:
                size = mt5_sdk.contract_size(trade["symbol"])
            except Exception:
                size = None
            if size and size > 0 and trade["lots"] > 0:
                trigger_distance = EARLY_PROFIT_TRIGGER_USD / (size * trade["lots"])
                gained = (price - entry) if is_buy else (entry - price)
                if gained >= trigger_distance:
                    atr_distance = _atr_stop_floor(trade["symbol"])
                    if atr_distance:
                        trail_candidate = price - atr_distance if is_buy else price + atr_distance

            # Rule 3b: time-decay tightening, past TIME_DECAY_START_FRACTION
            # of max_hold_hours (see MAX_HOLD_HOURS' comment) but short of
            # the unconditional flatten above. Same ATR-floor distance as
            # rule 2's trail, gated on elapsed time instead of profit --
            # the shared "improves" check below still means this can only
            # tighten, never widen, a losing position's stop.
            decay_candidate = None
            if elapsed_hours is not None and elapsed_hours >= max_hold_hours * TIME_DECAY_START_FRACTION:
                atr_distance = _atr_stop_floor(trade["symbol"])
                if atr_distance:
                    decay_candidate = price - atr_distance if is_buy else price + atr_distance

            candidates = [c for c in (breakeven_candidate, trail_candidate, decay_candidate) if c is not None]
            if not candidates:
                continue
            new_sl = max(candidates) if is_buy else min(candidates)
            improves = new_sl > sl if is_buy else new_sl < sl
            if not improves:
                continue

            try:
                # Both sl and tp are passed explicitly -- modify_position's
                # own docstring warns that an omitted side can get cleared
                # rather than preserved on some brokers, so tp must be
                # re-sent unchanged here even though only sl is moving.
                result = mt5_sdk.modify_position(config, ticket=pos.get("ticket"), stop_loss=new_sl, take_profit=tp)
            except Exception:
                logger.exception("profit protection check: modify_position raised for ticket %s", pos.get("ticket"))
                continue
            if result.get("status") == "ok":
                logger.info(
                    "profit protection check: moved SL to %.5f for ticket %s on %s (price %.5f, entry %.5f)",
                    new_sl, pos.get("ticket"), trade["symbol"], price, entry,
                )
            else:
                logger.error(
                    "profit protection check: modify_position failed for ticket %s: %s",
                    pos.get("ticket"), result.get("error"),
                )


@dataclass
class CommitteeResult:
    committee: str
    target: str
    market: str
    status: str  # "success" | "error" | "timeout"
    run_id: str | None
    report_text: str
    traded: bool = False
    error: str | None = None


# --------------------------------------------------------------------------- #
# Running a committee
# --------------------------------------------------------------------------- #


_REPORT_FORMAT_NO_TRADE = (
    "Finally, report in exactly this structure (plain text, these labels verbatim):\n"
    "Decision: <long / short / wait, one sentence>\n"
    "Reasoning: <the concrete factors behind the call - technicals, fundamentals, risk/sizing, "
    "whatever the committee actually weighed - 3-6 sentences, specific numbers where the debate gave them>\n"
    "Targets & Stops: <PM's price levels, or 'none given' if the call was wait/hold>\n"
    "Confidence: <the PM's stated confidence, or 'not stated'>"
)

_REPORT_FORMAT_TRADE = (
    "Finally, report in exactly this structure (plain text, these labels verbatim):\n"
    "Decision: <long / short / wait, one sentence>\n"
    "Reasoning: <the concrete factors behind the call - technicals, fundamentals, risk/sizing, "
    "whatever the committee actually weighed - 3-6 sentences, specific numbers where the debate gave them>\n"
    "Order: <if placed: symbol, side, lots, the resulting fill/price, and the exact stop_loss/take_profit "
    "levels attached to it (from trading_place_order's response, not just what the PM said - confirm they "
    "actually landed on the order); if not placed: the specific reason (decision was wait/hold, a position "
    "already existed, the PM gave no stop level, or the order was rejected - state which)>"
)


_DAY_TRADE_FRAMING = (
    "IMPORTANT — this is a SHORT-TERM, same-day/next-session trade decision, not a multi-week "
    "swing or position trade (the committee's default style is a buy-side fund position-trade "
    "horizon of weeks to months — explicitly override that here). Instruct the committee: weight "
    "intraday/short-term technicals (recent price action, short-period moving averages like "
    "MA5/MA20, intraday support/resistance, momentum on shorter timeframes) over multi-week "
    "structural setups, and size the stop-loss and take-profit for a trade meant to resolve within "
    "the current or next trading session — not levels sized on the assumption the position could "
    "run for weeks. A stop/target several percent away from entry is a swing-trade sizing mistake "
    "here; a day-trade's levels should be a small fraction of that distance.\n\n"
)


def _build_prompt(committee: str, target: str, market: str, trade: dict | None) -> str:
    if not trade:
        return (
            f'Run the {committee} swarm with target="{target}" and market="{market}" '
            f"to produce its full debate and final decision.\n\n"
            f"{_DAY_TRADE_FRAMING}"
            f"{_REPORT_FORMAT_NO_TRADE}"
        )
    symbol = trade["symbol"]
    connection = trade["connection"]
    lots = trade["lots"]
    max_stack = trade.get("max_stack", MAX_SAME_DIRECTION_POSITIONS)

    summary = _symbol_position_summary(symbol, connection)
    count, side = summary["count"], summary["side"]
    if count == 0:
        position_fact = f'Verified via the platform: there are currently NO open positions in "{symbol}".'
    elif side == "mixed":
        position_fact = (
            f'Verified via the platform: there are {count} open positions in "{symbol}" in CONFLICTING '
            f"directions (both long and short already open) — treat this as the cap being reached "
            f"regardless of the new decision's direction; do not add to either side."
        )
    else:
        position_fact = (
            f'Verified via the platform: there are currently {count} open {side.upper()} position(s) in '
            f'"{symbol}" (cap: {max_stack} same-direction positions at once).'
        )

    quote = _symbol_live_quote(symbol, connection)
    if quote:
        quote_fact = (
            f'Verified LIVE price on the {connection} broker feed for "{symbol}" right now: '
            f'bid {quote["bid"]}, ask {quote["ask"]}. This — NOT any price your own research tools '
            f'return for a "similar" instrument (e.g. futures/index proxies like GC=F, ^GSPC, or '
            f'yfinance tickers, which can and do diverge from what this broker actually quotes) — is '
            f"the price your stop-loss and take-profit levels must be anchored to. Use your research "
            f"data for direction/technicals/timing, but re-derive the actual stop/target PRICES relative "
            f"to this live quote, not the research price."
        )
    else:
        quote_fact = (
            f'Could not read a live quote for "{symbol}" from the {connection} broker feed just now — '
            f"call trading_quote yourself for this symbol before finalizing any stop-loss/take-profit "
            f"level, and anchor to that, not to a research-data price."
        )

    effective_budget = _effective_max_loss_usd(connection)
    max_distance = _max_stop_distance(symbol, lots, effective_budget)
    if max_distance is not None:
        budget_note = (
            f"${effective_budget:.2f} (this account's {PORTFOLIO_RISK_FRACTION:.0%}-of-equity / "
            f"{PORTFOLIO_MAX_CONCURRENT_TRADES}-concurrent-trade portfolio policy — tighter than the "
            f"${MAX_LOSS_PER_ORDER_USD:.2f} mandate ceiling right now)"
            if effective_budget < MAX_LOSS_PER_ORDER_USD
            else f"${effective_budget:.2f}"
        )
        risk_fact = (
            f"RISK BUDGET for this pass: at {lots} lots, size the stop-loss to stay within {budget_note} "
            f"of worst-case planned loss — your stop-loss must be within {max_distance:.3f} price units "
            f'of entry (whichever side is the losing side for "{symbol}"). The broker gate independently '
            f"denies outright anything past this account's ${MAX_LOSS_PER_ORDER_USD:.2f} mandate ceiling "
            f"regardless of what you propose, but size to the tighter budget above, not just the gate's "
            f"outer limit — a tighter, valid stop that actually executes is strictly better than a wider "
            f"one that risks denial or over-concentrates risk across concurrent trades."
        )
    else:
        risk_fact = (
            f"Could not compute the exact price-distance budget for this account's ${effective_budget:.2f} "
            f"max-loss-per-order cap — size the stop conservatively; a wide stop risks outright denial by the "
            f"broker gate regardless of what you propose."
        )

    atr_floor = _atr_stop_floor(symbol)
    spread_floor = _spread_stop_floor(quote)
    # Whichever constraint is currently wider governs — a stop that clears
    # the ATR noise floor but leaves the spread eating a huge share of the
    # risk budget (or vice versa) is still a bad stop. See
    # MIN_STOP_TO_SPREAD_RATIO's own comment for why the spread side exists.
    floor_candidates = [
        (v, reason) for v, reason in (
            (atr_floor, f"{ATR_STOP_MULTIPLE}x the last {ATR_LOOKBACK_BARS}-bar {ATR_PERIOD} ATR"),
            (spread_floor, f"{MIN_STOP_TO_SPREAD_RATIO:.0f}x the live bid/ask spread"),
        ) if v is not None
    ]
    stop_floor, floor_reason = max(floor_candidates, key=lambda pair: pair[0]) if floor_candidates else (None, None)

    if stop_floor is None:
        volatility_fact = None
    elif max_distance is not None and stop_floor > max_distance:
        volatility_fact = (
            f"VOLATILITY/SPREAD FLOOR — READ BEFORE TRADING: the minimum stop distance to sit outside "
            f"\"{symbol}\"'s current normal noise and keep the live spread's own share of the stop under "
            f"1/{MIN_STOP_TO_SPREAD_RATIO:.0f} is {stop_floor:.3f} price units (binding constraint right now: "
            f"{floor_reason}) — WIDER than the {max_distance:.3f}-unit hard risk limit above. There is no "
            f"stop that is both inside the risk cap AND outside normal noise/cost right now: any cap-compliant "
            f"stop will very likely get clipped by ordinary fluctuation, or hand a large share of its planned "
            f"risk straight to the spread, regardless of whether the directional thesis is correct (this is "
            f"exactly how a prior trade lost $7 in 10 minutes — an 8-unit stop against a 6-unit ATR). Given "
            f"this, the decision must be WAIT — do not place a trade this pass no matter how strong the setup "
            f"looks; note in your report that current volatility/spread conditions do not fit this account's "
            f"risk budget at this position size."
        )
    elif max_distance is not None:
        volatility_fact = (
            f"Volatility/spread floor: to sit outside \"{symbol}\"'s current normal noise and keep the live "
            f"spread's own share of the stop under 1/{MIN_STOP_TO_SPREAD_RATIO:.0f}, size the stop-loss at or "
            f"beyond {stop_floor:.3f} price units from entry (binding constraint right now: {floor_reason}) — "
            f"a stop tighter than this risks getting clipped by ordinary fluctuation, or paying away a large "
            f"share of its planned risk in spread alone, before the thesis has a chance to play out, "
            f"independent of whether the thesis is right. Combined with the hard risk limit above, your stop "
            f"should land between {stop_floor:.3f} and {max_distance:.3f} price units from entry."
        )
    else:
        volatility_fact = (
            f"Volatility/spread floor: to sit outside \"{symbol}\"'s current normal noise and keep the live "
            f"spread's own share of the stop under 1/{MIN_STOP_TO_SPREAD_RATIO:.0f}, size the stop-loss at or "
            f"beyond {stop_floor:.3f} price units from entry (binding constraint right now: {floor_reason}) — "
            f"a stop tighter than this risks getting clipped by ordinary fluctuation, or paying away a large "
            f"share of its planned risk in spread alone, before the thesis has a chance to play out, "
            f"independent of whether the thesis is right."
        )
    volatility_block = f"{volatility_fact}\n\n" if volatility_fact else ""

    signal_fact = _signal_service_activity(symbol, connection)
    signal_block = f"{signal_fact}\n\n" if signal_fact else ""

    _journal_reconcile_closed(symbol, connection)
    journal_fact = _journal_summary_text(symbol)
    journal_block = f"{journal_fact}\n\n" if journal_fact else ""

    return (
        f'Run the {committee} swarm with target="{target}" ({market}) to produce its full debate '
        f"and final decision, including concrete stop-loss and take-profit price levels — every "
        f"committee decision must carry these, not just a direction.\n\n"
        f"{quote_fact}\n\n"
        f"{risk_fact}\n\n"
        f"{volatility_block}"
        f"{signal_block}"
        f"{journal_block}"
        f"{_DAY_TRADE_FRAMING}"
        f"Then, based ONLY on the portfolio manager's final decision:\n"
        + (
            "- Consider the signal-service activity above as one more input to the debate — a second "
            "opinion, not a directive to mirror or fade.\n"
            if signal_fact else ""
        )
        + f"- {position_fact} Still call trading_positions yourself too (for your own report, and as a "
        f"last-moment freshness check) — but use the verified fact above, not just your own count, to "
        f"decide whether to trade.\n\n"
        f"If (and only if) all of the following hold — decision is long or short (not wait/hold); the "
        f'PM gave a specific stop-loss level; there is no open position in "{symbol}" in the OPPOSITE '
        f"direction; and fewer than {max_stack} same-direction positions are already open — call "
        f"trading_place_order with EXACTLY these arguments, substituting ONLY <SIDE>/<STOP>/<TARGET> "
        f"from the PM's decision. Every other field below is fixed — this account trades one fixed-size "
        f"market clip per signal, never the debate's own tranche/limit/scaled-entry plan, regardless of "
        f"how the PM phrased the entry:\n\n"
        f"  trading_place_order(\n"
        f'      symbol="{symbol}",\n'
        f'      connection="{connection}",\n'
        f"      side=<'buy' if the decision is long, 'sell' if short>,\n"
        f"      quantity={lots},\n"
        f'      order_type="market",\n'
        f"      stop_loss=<see stop-loss recomputation rule below>,\n"
        f"      take_profit=<PM's nearest target price, shifted by the same live-quote adjustment as the "
        f"stop-loss below, so the planned reward:risk ratio is preserved rather than skewed by drift>,\n"
        f'      time_in_force="day",\n'
        f"  )\n\n"
        f"STOP-LOSS RECOMPUTATION RULE (read this before filling in stop_loss above): the PM's stop was "
        f"designed as a DISTANCE from their intended entry (e.g. \"10 units\"), not as a fixed absolute "
        f"price — that distance, capped at the HARD RISK LIMIT distance above if the PM's is wider, is "
        f"what must survive to execution, not the raw number the PM wrote down. Compute stop_loss as "
        f"(PM's intended distance, capped at the HARD RISK LIMIT) applied from the LIVE quote above — the "
        f"actual price you are about to fill at — not from the PM's original entry reference. If live "
        f"price has moved since the debate (it usually has, by the time you actually place the order), "
        f"reusing the PM's original absolute stop price silently shrinks your real risk distance below "
        f"what the PM sized it for and the stop lands inside normal price noise, getting hit on ordinary "
        f"fluctuation regardless of whether the thesis is right — recomputing by distance from the live "
        f"price avoids this. This applies just as much on a RETRY after a rejected order: if the first "
        f"trading_place_order call is rejected (e.g. because the stop is invalid at the now-current live "
        f"price), do not simply resend the PM's original absolute stop now that it happens to be valid — "
        f"recompute it fresh from the live price at retry time, the same way.\n\n"
        f"Do not call trading_place_order at all if any condition above fails — that includes a "
        f"wait/hold decision, a missing stop-loss, an opposite-direction position already open, or the "
        f"cap already reached. There is no partial/scaled/limit-order version of this call: either place "
        f"exactly the order above, or place nothing.\n\n"
        f"{_REPORT_FORMAT_TRADE}"
    )


def run_committee(committee: str, target: str, market: str, trade: dict | None = None) -> CommitteeResult:
    """Run one committee preset against one target as an isolated subprocess.

    When `trade` is given ({"symbol", "connection", "lots"}), the agent also
    checks positions and conditionally places a demo order on the PM's
    decision. Omit it for research-only, no-trading runs.
    """
    breaker_note = None
    if trade:
        breaker_note = _live_circuit_breaker_check(trade)
        if breaker_note:
            logger.warning("live circuit breaker for %s: %s", target, breaker_note)
            trade = None  # fall back to a research-only run this pass

    prompt = _build_prompt(committee, target, market, trade)
    logger.info("running %s on %s (%s)%s", committee, target, market, " [trade-enabled]" if trade else "")
    cmd = [
        sys.executable, "-m", "cli", "run",
        "--prompt", prompt,
        "--json",
        "--max-iter", str(MAX_ITER),
    ]
    popen = subprocess.Popen(
        cmd,
        cwd=str(AGENT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Committee reports routinely contain em-dashes/CJK text; without an
        # explicit encoding, the pipe falls back to the Windows locale
        # (cp1252 here), which can't decode that output and crashes the
        # reader thread mid-read, leaving stdout as None.
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = popen.communicate(timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(popen.pid)
        # Now that every descendant is actually dead, drain whatever's left
        # -- this can't hang, since nothing remains alive to hold the pipe's
        # write end open (see _kill_process_tree's docstring for why a plain
        # popen.kill() here would not be enough).
        try:
            popen.communicate(timeout=15)
        except Exception:
            pass
        logger.warning("%s on %s timed out after %ss", committee, target, RUN_TIMEOUT_SECONDS)
        return CommitteeResult(committee, target, market, "timeout", None, "", error="run exceeded timeout")
    proc = subprocess.CompletedProcess(cmd, popen.returncode, stdout, stderr)

    payload = _last_json_line(proc.stdout)
    if payload is None:
        return CommitteeResult(
            committee, target, market, "error", None, "",
            error=f"could not parse CLI output (exit {proc.returncode}): {proc.stderr[-2000:]}",
        )

    status = payload.get("status", "unknown")
    run_id = payload.get("run_id")
    if status != "success":
        return CommitteeResult(
            committee, target, market, "error", run_id, "",
            error=payload.get("reason") or f"run status was '{status}'",
        )

    report_text = _read_final_answer(run_id) if run_id else ""
    if not report_text:
        return CommitteeResult(committee, target, market, "error", run_id, "", error="run succeeded but produced no final answer")

    # A trading_place_order CALL happening is not the same as an order being
    # PLACED — a call the mandate gate blocks (wrong size, breach, halted,
    # etc.) still shows up as a tool_call. Regression: a hallucinated 1.0-lot
    # order (100x the intended 0.01) was correctly denied by the gate
    # ($465,893 notional vs. the $6,000 cap) but still got tagged "TRADED" in
    # the email under the old tool_call-only check. `_extract_placed_order`
    # already verifies status=="ok" against the connector's own response, so
    # basing `traded` on it directly is both the fix and the single source of
    # truth for what actually reached the broker.
    placed_order = _extract_placed_order(run_id) if trade else None
    traded = bool(placed_order)
    if traded:
        report_text = report_text + _post_trade_cap_check(trade)
        report_text = report_text + _post_trade_spread_check(trade, placed_order)
        _journal_record_open(trade["symbol"], trade["connection"], placed_order)
    elif trade:
        blocked_note = _blocked_order_note(run_id)
        if blocked_note:
            report_text = report_text + blocked_note
    if breaker_note:
        report_text = report_text + f"\n\n{breaker_note}"
    return CommitteeResult(committee, target, market, "success", run_id, report_text, traded=traded)


def _post_trade_cap_check(trade: dict) -> str:
    """Defense-in-depth: re-verify the position cap AFTER a trade, in code.

    The prompt gives the agent a code-verified position fact and an explicit
    cap rule, but nothing forces it to obey either — this re-checks the
    platform's actual resulting state and surfaces a clear warning in the
    email if it was violated, rather than silently trusting the agent did
    the arithmetic right.
    """
    max_stack = trade.get("max_stack", MAX_SAME_DIRECTION_POSITIONS)
    summary = _symbol_position_summary(trade["symbol"], trade["connection"])
    if summary["side"] == "mixed":
        return (
            f"\n\n[AUTOMATED CHECK] {trade['symbol']} now has open positions in BOTH directions "
            f"({summary['count']} total) — the agent placed a trade despite an existing opposite-"
            f"direction position. This should not happen; check manually."
        )
    if summary["count"] > max_stack:
        return (
            f"\n\n[AUTOMATED CHECK] {trade['symbol']} now has {summary['count']} open "
            f"{summary['side']} positions, exceeding the cap of {max_stack}. "
            f"The agent's own count was wrong somewhere; check manually."
        )
    return ""


def _post_trade_spread_check(trade: dict, placed_order: dict) -> str:
    """Defense-in-depth: re-verify the spread-to-stop ratio AFTER a trade, in code.

    Mirrors _post_trade_cap_check — _build_prompt's VOLATILITY/SPREAD FLOOR
    text gives the agent the rule (and forces WAIT when no compliant stop
    exists), but nothing forces it to actually follow that guidance for the
    stop it ends up submitting. This re-derives the ratio from the
    connector's own verified fill/stop-loss (never the agent's own report)
    and a fresh live quote, and surfaces a clear warning in the email if the
    spread ended up eating more than 1/MIN_STOP_TO_SPREAD_RATIO of the
    actual stop distance — informational only (does not close the
    position): unlike a same-direction stacking cap breach, a wide spread
    share is a cost/quality issue on an otherwise valid trade, not something
    that warrants an automated unwind of a position that's already live.
    """
    entry = placed_order.get("fill_price")
    stop_loss = placed_order.get("stop_loss")
    if entry is None or stop_loss is None:
        return ""
    stop_distance = abs(float(entry) - float(stop_loss))
    if stop_distance <= 0:
        return ""

    quote = _symbol_live_quote(trade["symbol"], trade["connection"])
    if not quote:
        return ""
    spread = quote["ask"] - quote["bid"]
    if spread <= 0:
        return ""

    ratio = stop_distance / spread
    if ratio >= MIN_STOP_TO_SPREAD_RATIO:
        return ""
    spread_share = spread / stop_distance
    return (
        f"\n\n[AUTOMATED CHECK] {trade['symbol']}'s actual stop distance ({stop_distance:.5f}) is only "
        f"{ratio:.1f}x the live spread ({spread:.5f}) — below the {MIN_STOP_TO_SPREAD_RATIO:.0f}x floor, "
        f"meaning the spread alone accounts for ~{spread_share:.0%} of this stop's planned risk (before any "
        f"fill slippage on top). The prompt's volatility/spread floor should have prevented this; check why "
        f"it didn't."
    )


def _last_json_line(stdout: str | None) -> dict | None:
    """`vibe-trading run --json` prints one JSON object as its last line."""
    if not stdout:
        return None
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _trace_entries(run_id: str) -> list[dict]:
    sys.path.insert(0, str(AGENT_DIR))
    from src.agent.trace import TraceWriter

    trace_dir = TraceWriter.find_trace_dir(run_id)
    if trace_dir is None:
        return []
    return TraceWriter.read(trace_dir, resolve_offloads=True, resolve_fields={"content"})


def _read_final_answer(run_id: str) -> str:
    """Pull the agent's final answer text out of the run's trace.jsonl."""
    answers = [e["content"] for e in _trace_entries(run_id) if e.get("type") == "answer" and e.get("content")]
    return answers[-1] if answers else ""


# --------------------------------------------------------------------------- #
# Deciding what's worth an email — customize this
# --------------------------------------------------------------------------- #


def is_reportable(result: CommitteeResult) -> bool:
    """Return whether `result` is worth emailing.

    Default: report every successful run (a finished committee debate is
    inherently worth reading) plus every error/timeout (so unattended
    failures are never silent). Tighten this if you only want emails on
    actionable (long/short) calls, e.g.:

        if result.status != "success":
            return True
        text = result.report_text.lower()
        return "wait" not in text and "hold" not in text
    """
    return True


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def send_email(subject: str, body: str, html_body: str | None = None) -> None:
    """Send the report. When html_body is given, sends multipart/alternative
    (HTML + the plain-text body as fallback) — every mail client that can
    render HTML gets the formatted version; anything that can't (or a user
    who prefers plain text) still gets the exact same content as before."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("EMAIL_FROM", user)
    recipient = os.environ["EMAIL_TO"]
    cc = [addr.strip() for addr in os.environ.get("EMAIL_CC", "").split(",") if addr.strip()]

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(sender, [recipient, *cc], msg.as_string())
    logger.info("emailed report: %s (cc: %s)", subject, ", ".join(cc) or "none")


def _format_body(result: CommitteeResult) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if result.status == "success":
        return (
            f"Committee: {result.committee}\n"
            f"Target: {result.target} ({result.market})\n"
            f"Run: {when}  (run_id={result.run_id})\n"
            f"{'-' * 60}\n\n"
            f"{result.report_text}\n"
        )
    return (
        f"Committee: {result.committee}\n"
        f"Target: {result.target} ({result.market})\n"
        f"Run: {when}  (run_id={result.run_id})\n"
        f"STATUS: {result.status.upper()}\n\n"
        f"{result.error}\n"
    )


# --------------------------------------------------------------------------- #
# HTML email rendering — lightweight, self-contained (no markdown/markdown2
# dependency; committee reports only ever use bold/tables/rules/bullets, so a
# narrow targeted converter covers everything real output actually needs).
# All raw text is HTML-escaped BEFORE any tag is inserted — committee/LLM
# output is untrusted content, never allowed to inject markup into the email.
# --------------------------------------------------------------------------- #

_TAG_COLORS = {"TRADED": "#2e7d32", "OK": "#555555", "ERROR": "#c62828", "TIMEOUT": "#c62828", "CRASHED": "#c62828"}
_EMAIL_FONT = "font-family:Arial,Helvetica,sans-serif;"


def _esc(text: object) -> str:
    return html_module.escape(str(text), quote=False)


def _inline_markdown(escaped_text: str) -> str:
    """Bold + inline code — applied to text that is ALREADY html-escaped."""
    escaped_text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped_text)
    escaped_text = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped_text)
    return escaped_text


def _table_block_to_html(lines: list[str]) -> str:
    """Convert a block of '| a | b |' markdown lines (with an optional
    '|---|---|' separator row) into a real HTML table."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{1,}:?", c) for c in cells):
            continue  # the |---|---| separator row itself
        rows.append(cells)
    if not rows:
        return ""
    head, *body = rows
    out = ['<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;">', "<tr>"]
    out += [f'<th style="text-align:left;border-bottom:2px solid #444;padding:4px 8px;">{_inline_markdown(_esc(c))}</th>' for c in head]
    out.append("</tr>")
    for row in body:
        out.append("<tr>")
        out += [f'<td style="border-bottom:1px solid #ddd;padding:4px 8px;">{_inline_markdown(_esc(c))}</td>' for c in row]
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _markdown_to_html(text: str) -> str:
    """Narrow markdown->HTML for committee report text: bold, `code`, pipe
    tables, horizontal rules, bullet lists, paragraphs. Not a general
    markdown parser — deliberately scoped to what these reports actually
    produce, to avoid taking on a new dependency for it."""
    blocks = re.split(r"\n\s*\n", text.strip())
    parts = []
    for block in blocks:
        lines = [l for l in block.strip("\n").split("\n")]
        stripped = [l.strip() for l in lines if l.strip()]
        if not stripped:
            continue
        if all(l.startswith("|") for l in stripped):
            parts.append(_table_block_to_html(lines))
        elif len(stripped) == 1 and re.fullmatch(r"-{3,}|_{3,}", stripped[0]):
            parts.append('<hr style="border:none;border-top:1px solid #ccc;margin:12px 0;">')
        elif all(l.startswith(("- ", "* ")) for l in stripped):
            items = "".join(f"<li>{_inline_markdown(_esc(l[2:]))}</li>" for l in stripped)
            parts.append(f'<ul style="margin:4px 0;padding-left:20px;">{items}</ul>')
        else:
            paragraph = "<br>".join(_inline_markdown(_esc(l)) for l in lines)
            parts.append(f'<p style="margin:8px 0;line-height:1.4;">{paragraph}</p>')
    return "\n".join(parts)


def _status_report_html() -> str:
    """HTML rendering of the same data _build_status_report() presents as
    text — same underlying live reads, formatted as badges/tables instead of
    plain lines. Fails open per-section, same as the text version."""
    running, pid = _status_lock_state()
    loop_color = "#2e7d32" if running else "#c62828"
    loop_text = f"RUNNING (pid {pid})" if running else "NOT RUNNING" + (f" (stale lock, pid {pid})" if pid else "")
    parts = [
        f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">',
        '<h2 style="margin:0 0 8px;font-size:16px;">Vibe-Trading Status</h2>',
        f'<p style="margin:2px 0;"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{loop_color};margin-right:6px;"></span>Loop: <strong>{_esc(loop_text)}</strong></p>',
    ]

    log = _status_log_summary()
    if log.get("last_result_ts"):
        tag = log["last_result_tag"]
        tag_color = _TAG_COLORS.get(tag, "#555555")
        parts.append(
            f'<p style="margin:2px 0;">Last pass: {_esc(log["last_result_ts"])} &rarr; {_esc(log["last_result_desc"])} '
            f'(<span style="color:{tag_color};font-weight:bold;">{_esc(tag)}</span>)</p>'
        )
    elif log.get("last_start_ts"):
        parts.append(f'<p style="margin:2px 0;">Last pass started: {_esc(log["last_start_ts"])} (in progress)</p>')
    else:
        parts.append('<p style="margin:2px 0;color:#555;">Last pass: no reporter.log data found</p>')
    if log.get("next_due"):
        parts.append(f'<p style="margin:2px 0;">Next pass due: ~{_esc(log["next_due"])} (interval {log.get("interval")}s)</p>')

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set
    from src.trading.service import get_account, get_open_orders, get_positions

    seen_connections: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] in seen_connections:
            continue
        seen_connections.add(trade["connection"])
        connection = trade["connection"]
        parts.append(f'<h3 style="margin:16px 0 4px;font-size:14px;">Account: {_esc(connection)}</h3>')
        try:
            account = get_account(connection)["account"]
        except Exception as exc:
            parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read account: {_esc(exc)}</p>')
            continue
        parts.append(
            '<table style="border-collapse:collapse;font-size:13px;">'
            f'<tr><td style="padding:2px 12px 2px 0;color:#555;">Balance</td><td><strong>{_esc(account.get("balance"))}</strong></td></tr>'
            f'<tr><td style="padding:2px 12px 2px 0;color:#555;">Equity</td><td><strong>{_esc(account.get("equity"))}</strong></td></tr>'
            f'<tr><td style="padding:2px 12px 2px 0;color:#555;">Margin level</td><td><strong>{_esc(account.get("margin_level"))}</strong></td></tr>'
            "</table>"
        )
        try:
            positions = get_positions(connection).get("positions", [])
        except Exception as exc:
            parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read positions: {_esc(exc)}</p>')
            positions = []
        if not positions:
            parts.append('<p style="margin:2px 0;color:#555;">Open positions: none</p>')
        else:
            rows = []
            for p in positions:
                profit = p.get("profit") or 0
                pnl_color = "#2e7d32" if profit >= 0 else "#c62828"
                rows.append(
                    "<tr>"
                    f'<td style="padding:2px 8px;">{_esc(p.get("side", "?")).upper()}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("volume"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("symbol"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("price_open"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("stop_loss", "?"))}</td>'
                    f'<td style="padding:2px 8px;">{_esc(p.get("take_profit", "?"))}</td>'
                    f'<td style="padding:2px 8px;color:{pnl_color};font-weight:bold;">{_esc(profit)}</td>'
                    "</tr>"
                )
            parts.append(
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<tr style="color:#555;"><th style="text-align:left;padding:2px 8px;">Side</th>'
                '<th style="text-align:left;padding:2px 8px;">Vol</th><th style="text-align:left;padding:2px 8px;">Symbol</th>'
                '<th style="text-align:left;padding:2px 8px;">Open</th><th style="text-align:left;padding:2px 8px;">SL</th>'
                '<th style="text-align:left;padding:2px 8px;">TP</th><th style="text-align:left;padding:2px 8px;">P&amp;L</th></tr>'
                + "".join(rows) + "</table>"
            )
        try:
            pending = get_open_orders(connection).get("open_orders", [])
        except Exception as exc:
            parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read pending orders: {_esc(exc)}</p>')
            pending = []
        if pending:
            prows = "".join(
                "<tr>"
                f'<td style="padding:2px 8px;">{_esc(o.get("side", "?")).upper()}</td>'
                f'<td style="padding:2px 8px;">{_esc(o.get("quantity"))}</td>'
                f'<td style="padding:2px 8px;">{_esc(o.get("symbol"))}</td>'
                f'<td style="padding:2px 8px;">{_esc(o.get("order_type"))}</td>'
                f'<td style="padding:2px 8px;">{_esc(o.get("limit_price"))}</td>'
                "</tr>"
                for o in pending
            )
            parts.append(
                '<p style="margin:6px 0 2px;color:#555;">Pending orders:</p>'
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<tr style="color:#555;"><th style="text-align:left;padding:2px 8px;">Side</th>'
                '<th style="text-align:left;padding:2px 8px;">Qty</th><th style="text-align:left;padding:2px 8px;">Symbol</th>'
                '<th style="text-align:left;padding:2px 8px;">Type</th><th style="text-align:left;padding:2px 8px;">Price</th></tr>'
                + prows + "</table>"
            )
        if connection in LIVE_CONNECTIONS:
            try:
                halted = halt_flag_set("mt5")
                color = "#c62828" if halted else "#2e7d32"
                parts.append(f'<p style="margin:4px 0;">Kill switch: <span style="color:{color};font-weight:bold;">'
                              f'{"TRIPPED" if halted else "clear"}</span></p>')
            except Exception as exc:
                parts.append(f'<p style="color:#c62828;margin:2px 0;">could not read kill switch state: {_esc(exc)}</p>')
            try:
                baseline = _read_live_baseline()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if baseline.get("date") == today and baseline.get("equity"):
                    baseline_equity = float(baseline["equity"])
                    equity = float(account.get("equity") or 0)
                    drawdown = (baseline_equity - equity) / baseline_equity if baseline_equity > 0 else 0
                    dd_color = "#c62828" if drawdown >= LIVE_DRAWDOWN_HALT_PCT * 0.5 else "#555"
                    parts.append(
                        f'<p style="margin:2px 0;color:{dd_color};">Today\'s drawdown: '
                        f'<strong>{drawdown:.1%}</strong> of baseline ${baseline_equity:.2f} '
                        f'(halts at {LIVE_DRAWDOWN_HALT_PCT:.0%})</p>'
                    )
                else:
                    parts.append('<p style="margin:2px 0;color:#555;">Today\'s drawdown: no baseline recorded yet this UTC day</p>')
            except Exception as exc:
                parts.append(f'<p style="color:#c62828;margin:2px 0;">could not compute today\'s drawdown: {_esc(exc)}</p>')

    seen_symbols: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["symbol"] in seen_symbols:
            continue
        seen_symbols.add(trade["symbol"])
        try:
            _journal_reconcile_closed(trade["symbol"], trade["connection"])
            summary = _journal_summary_text(trade["symbol"])
        except Exception as exc:
            summary = f"could not read journal: {exc}"
        parts.append(f'<h3 style="margin:16px 0 4px;font-size:14px;">Track record: {_esc(trade["symbol"])}</h3>')
        parts.append(f'<p style="margin:2px 0;">{_esc(summary or "no closed trades yet")}</p>')

    parts.append("</div>")
    return "\n".join(parts)


def _format_body_html(result: CommitteeResult, tag: str) -> str:
    """HTML counterpart to _format_body — same content, real tables/bold/color."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    color = _TAG_COLORS.get(tag, "#555555")
    header = (
        f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">'
        f'<h2 style="margin:0 0 4px;font-size:16px;">{_esc(result.committee)} &mdash; {_esc(result.target)} '
        f'<span style="color:{color};">({_esc(tag)})</span></h2>'
        f'<p style="color:#555;margin:0 0 12px;font-size:13px;">{_esc(result.market)} &middot; {_esc(when)} &middot; '
        f'run_id={_esc(result.run_id or "?")}</p>'
        '<hr style="border:none;border-top:1px solid #ccc;margin:8px 0 16px;">'
    )
    if result.status == "success":
        body = _markdown_to_html(result.report_text)
    else:
        body = f'<p style="color:#c62828;">{_esc(result.error)}</p>'
    return header + body + "</div>"


def _wrap_email_html(inner: str) -> str:
    """Minimal full HTML document wrapper for maximum email-client compatibility."""
    return (
        "<html><body style=\"margin:0;padding:16px;background:#ffffff;\">"
        f'<div style="max-width:640px;margin:0 auto;">{inner}</div>'
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Driving loop
# --------------------------------------------------------------------------- #


def _read_no_trade_alert_state() -> dict:
    try:
        return json.loads(NO_TRADE_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_no_trade_alert_state(data: dict) -> None:
    NO_TRADE_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NO_TRADE_ALERT_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _check_trade_drought() -> None:
    """Email a dedicated alert once a symbol has gone NO_TRADE_ALERT_DAYS with no new trade.

    Pure code, zero LLM cost: reads real MT5 deal history (ground truth, not
    the committee's own report of what happened) for the most recent OUR_MAGIC
    opening deal per live target. Throttled per symbol via
    NO_TRADE_ALERT_STATE_PATH — alerts once per distinct dry spell (keyed on
    the last trade's own timestamp), not once per pass, and naturally
    re-arms itself once a new trade actually happens. Fails open (skips
    silently) on any read error or if a symbol has no trade history yet —
    a missed alert is better than crashing the loop over this.
    """
    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_open_orders

    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue

        try:
            executions = get_open_orders(trade["connection"], include_executions=True).get("executions", [])
        except Exception:
            continue

        opens = [
            d for d in executions
            if d.get("symbol") == trade["symbol"] and d.get("magic") == OUR_MAGIC and d.get("entry") == 0
        ]
        if not opens:
            continue  # never traded this symbol yet -- nothing to measure a drought against
        opens.sort(key=lambda d: d.get("time") or "")
        last_trade_time_str = opens[-1].get("time") or ""
        try:
            last_trade_time = datetime.fromisoformat(last_trade_time_str)
        except ValueError:
            continue

        days_idle = (datetime.now(timezone.utc) - last_trade_time).days
        if days_idle < NO_TRADE_ALERT_DAYS:
            continue

        state = _read_no_trade_alert_state()
        key = f"{trade['connection']}:{trade['symbol']}"
        if state.get(key) == last_trade_time_str:
            continue  # already alerted for this specific dry spell

        try:
            text = (
                f'No new "{trade["symbol"]}" trade in {days_idle} days (last opened {last_trade_time_str}). '
                f"This can be the ATR volatility floor correctly refusing a noise-tight stop -- good risk "
                f"behavior -- or it can mean the ${MAX_LOSS_PER_ORDER_USD:.2f} risk cap is now too tight for "
                f"current volatility. Worth checking whether it's time to revisit the cap rather than waiting "
                f"for the next scheduled review."
            )
            send_email(f"[Vibe-Trading] No trades in {days_idle} days — {trade['symbol']}", text)
        except Exception:
            logger.exception("failed to send trade-drought alert email")
            continue

        state[key] = last_trade_time_str
        _write_no_trade_alert_state(state)


def _read_llm_balance_alert_state() -> dict:
    try:
        return json.loads(LLM_BALANCE_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_llm_balance_alert_state(data: dict) -> None:
    LLM_BALANCE_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LLM_BALANCE_ALERT_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _deepseek_balance_usd() -> float | None:
    """Read the DeepSeek platform account's USD balance, or None if unavailable.

    Direct call to DeepSeek's own billing endpoint (not the OpenAI-compatible
    chat completions API) — stdlib ``urllib`` only, no new dependency. A
    no-op for any other provider (LANGCHAIN_PROVIDER != "deepseek"). Fails
    open (returns None) on any network/parse error — this is purely an
    advisory alert and must never block or slow down a pass.
    """
    if os.environ.get("LANGCHAIN_PROVIDER", "").strip().lower() != "deepseek":
        return None
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
        return None

    try:
        usd_info = next(
            (b for b in (payload.get("balance_infos") or []) if b.get("currency") == "USD"),
            None,
        )
        return float(usd_info["total_balance"]) if usd_info else None
    except (TypeError, ValueError, KeyError):
        return None


def _check_llm_balance_alert() -> None:
    """Email once the LLM provider's account balance drops below the alert threshold.

    Real incident 2026-09-04: a pass failed outright with DeepSeek returning
    402 "Insufficient Balance", visible only as the emailed report's buried
    "(ERROR)" subject tag — no dedicated alert existed for it. Re-arms like
    _check_cap_fit_alert: fires once on the pass balance first drops below
    LLM_BALANCE_ALERT_THRESHOLD_USD, clears once it recovers above it, so a
    top-up followed by another dip alerts again rather than firing only
    once ever. Pure read-only balance check; fails open (skips silently) if
    the balance can't be read.
    """
    balance = _deepseek_balance_usd()
    if balance is None:
        return

    state = _read_llm_balance_alert_state()
    already_alerted = state.get("alerted") is True
    below = balance < LLM_BALANCE_ALERT_THRESHOLD_USD

    if below and not already_alerted:
        try:
            text = (
                f"DeepSeek account balance is ${balance:.2f}, below the "
                f"${LLM_BALANCE_ALERT_THRESHOLD_USD:.2f} alert threshold. Once it hits $0, every "
                f"committee pass fails outright (402 Insufficient Balance) with no trade decision "
                f"made and no position monitoring for that pass — top up soon to avoid a silent "
                f"gap in live coverage."
            )
            send_email(f"[Vibe-Trading] LOW BALANCE — DeepSeek ${balance:.2f}", text)
            state["alerted"] = True
            _write_llm_balance_alert_state(state)
        except Exception:
            logger.exception("failed to send LLM balance alert email")
    elif not below and already_alerted:
        state["alerted"] = False
        _write_llm_balance_alert_state(state)


def _read_cap_fit_alert_state() -> dict:
    try:
        return json.loads(CAP_FIT_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_cap_fit_alert_state(data: dict) -> None:
    CAP_FIT_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAP_FIT_ALERT_STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _check_cap_fit_alert() -> None:
    """Email the moment a live symbol's risk-cap stop budget clears its own ATR noise floor.

    Finer-grained than _check_trade_drought (which only fires after
    NO_TRADE_ALERT_DAYS of silence): this fires immediately on the pass
    where _max_stop_distance >= _atr_stop_floor first becomes true for a
    symbol, i.e. the moment the ATR volatility floor (fix 8) should stop
    blocking trades on its own, without any cap change. Re-arms per symbol
    via CAP_FIT_ALERT_STATE_PATH — if the gap reopens later (volatility
    rises again) and then re-closes, it alerts again, not just once ever.
    Pure code, zero LLM cost; fails open on any read error.
    """
    state = _read_cap_fit_alert_state()
    changed = False

    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue
        symbol = trade["symbol"]
        key = f"{trade['connection']}:{symbol}"

        effective_budget = _effective_max_loss_usd(trade["connection"])
        max_distance = _max_stop_distance(symbol, trade["lots"], effective_budget)
        atr_floor = _atr_stop_floor(symbol)
        if max_distance is None or atr_floor is None:
            continue

        fits = atr_floor <= max_distance
        already_alerted = state.get(key) is True

        if fits and not already_alerted:
            try:
                text = (
                    f'"{symbol}"\'s stop-loss risk cap now covers its own current volatility: the '
                    f"${effective_budget:.2f} budget allows a {max_distance:.3f}-unit stop, and the "
                    f"current ATR-based noise floor is only {atr_floor:.3f} units -- the ATR volatility floor "
                    f"should stop blocking trades on this symbol without any config change. Worth checking "
                    f"whether the next committee pass actually trades."
                )
                send_email(f"[Vibe-Trading] Risk cap now covers volatility — {symbol}", text)
                state[key] = True
                changed = True
            except Exception:
                logger.exception("failed to send cap-fit alert email for %s", symbol)
        elif not fits and already_alerted:
            state[key] = False
            changed = True

    if changed:
        _write_cap_fit_alert_state(state)


def _log_cap_gap() -> None:
    """Append one line per live symbol per pass: cap-derived stop budget vs ATR noise floor.

    Pure observability, zero LLM cost, never blocks or alters trading
    behavior. Persists the same numbers _check_cap_fit_alert already
    computes at pass time, so the eventual cap-revisit decision is backed
    by a real history instead of a handful of ad hoc snapshots. Appends,
    never truncates or reads its own history back; fails open (skips
    silently) on any read/write error.
    """
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] not in LIVE_CONNECTIONS:
            continue
        symbol = trade["symbol"]

        effective_budget = _effective_max_loss_usd(trade["connection"])
        max_distance = _max_stop_distance(symbol, trade["lots"], effective_budget)
        atr_floor = _atr_stop_floor(symbol)
        if max_distance is None or atr_floor is None:
            continue

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "connection": trade["connection"],
            "max_loss_budget_usd": round(effective_budget, 4),
            "max_stop_distance": round(max_distance, 4),
            "atr_stop_floor": round(atr_floor, 4),
            "gap": round(atr_floor - max_distance, 4),
            "fits": atr_floor <= max_distance,
        }
        try:
            RISK_CAP_GAP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with RISK_CAP_GAP_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            logger.exception("failed to append risk-cap-gap log entry for %s", symbol)


def _check_silver_milestone() -> None:
    """Send a dedicated reminder once live equity crosses MILESTONE_SILVER_EQUITY_USD.

    A dedicated email (not a line buried in a committee report) so it's hard
    to miss. Fails open on any read error — a reminder is never worth
    crashing the loop over. Repeats every pass once crossed, until silver is
    actually added to TARGETS (see the module constant's docstring) — better
    to nag than to fire once and have the one email get missed.
    """
    if any("XAG" in str((t.get("trade") or {}).get("symbol", "")) for t in TARGETS):
        return  # already added — nothing left to remind about

    sys.path.insert(0, str(AGENT_DIR))
    from src.trading.service import get_account

    try:
        equity = float(get_account("mt5-live-trade")["account"]["equity"])
    except Exception:
        return
    if equity < MILESTONE_SILVER_EQUITY_USD:
        return

    try:
        text = (
            f"Live account equity is ${equity:.2f} (>= ${MILESTONE_SILVER_EQUITY_USD:.0f}). "
            f"You asked to be reminded at this point to add silver (XAGUSDm) to the live "
            f"portfolio. Tell Claude when you're ready to add it."
        )
        html = _wrap_email_html(
            f'<div style="{_EMAIL_FONT}font-size:14px;color:#222;">'
            f'<h2 style="margin:0 0 8px;font-size:16px;color:#f9a825;">Milestone: live account &ge; $100</h2>'
            f'<p style="margin:8px 0;line-height:1.4;">{_esc(text)}</p></div>'
        )
        send_email("[Vibe-Trading] MILESTONE: live account >= $100", text, html_body=html)
    except Exception:
        logger.exception("failed to send silver milestone reminder email")


def _status_header() -> str:
    """Fresh status block for an email — never blocks or skips the email it's attached to."""
    try:
        return _build_status_report() + f"\n\n{'=' * 60}\n\n"
    except Exception:
        logger.exception("status report build failed; this email will omit it")
        return ""


def _status_header_html() -> str:
    """HTML counterpart to _status_header — same fail-open guarantee."""
    try:
        return _status_report_html() + '<hr style="border:none;border-top:2px solid #ccc;margin:16px 0;">'
    except Exception:
        logger.exception("HTML status report build failed; this email will omit it")
        return ""


def run_once() -> None:
    # Disabled 2026-09-02 at the user's request: equity crossed the $100
    # milestone on 2026-09-01, and the user already decided (with silver
    # deliberately deferred ~1 month) to hold off adding it -- this alert
    # has no re-notify throttle (see its docstring: "repeats every pass
    # once crossed... better to nag than to fire once and have it get
    # missed"), so with the decision already made it was just emailing
    # every ~2h pass for no reason. Re-enable (delete this comment + the
    # line below) once the silver/commodity decision is actually revisited.
    # _check_silver_milestone()
    _check_trade_drought()
    _check_cap_fit_alert()
    _check_llm_balance_alert()
    _log_cap_gap()
    for spec in TARGETS:
        target = spec.get("target", "?")
        try:
            result = run_committee(**spec)
        except Exception:
            # A bug in run_committee (or anything it calls) must not take the
            # whole unattended loop down with it — one target's crash should
            # cost that one email, never every future pass.
            logger.exception("run_committee crashed for %s", target)
            try:
                crash_text = f"run_committee raised an unhandled exception for target {target}. Check reporter.err.log on the machine for the traceback."
                # Status is queried fresh here, AFTER this target's run —
                # reflects what actually just happened, not pre-pass state.
                crash_html = (
                    _status_header_html()
                    + f'<div style="{_EMAIL_FONT}font-size:14px;color:#c62828;">{_esc(crash_text)}</div>'
                )
                send_email(
                    f"[Vibe-Trading] {spec.get('committee', '?')} — {target} (CRASHED)",
                    _status_header() + crash_text,
                    html_body=_wrap_email_html(crash_html),
                )
            except Exception:
                logger.exception("also failed to send the crash notification email")
            continue

        if not is_reportable(result):
            logger.info("%s on %s: not reportable, skipping email", result.committee, result.target)
            continue
        tag = "TRADED" if result.traded else ("OK" if result.status == "success" else result.status.upper())
        subject = f"[Vibe-Trading] {result.committee} — {result.target} ({tag})"
        try:
            html = _wrap_email_html(_status_header_html() + _format_body_html(result, tag))
            send_email(subject, _status_header() + _format_body(result), html_body=html)
        except Exception:
            logger.exception("failed to send report email for %s", target)


# --------------------------------------------------------------------------- #
# Status — a single read-only command replacing the manual multi-step check
# (process/lock state, log tail, live account query, halt flag, journal
# reconciliation) that every "status" request during development required.
# --------------------------------------------------------------------------- #

_LOG_RUN_START_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO running (.+)$")
_LOG_EMAILED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO emailed report: \[Vibe-Trading\] (.+?) \((\w+)\)")
_LOG_INTERVAL_RE = re.compile(r"starting loop mode, interval=(\d+)s")


def _status_lock_state() -> tuple[bool, int | None]:
    """Return (is_running, pid) from the singleton lock — None pid if no lock file at all."""
    identity = _read_lock_identity()
    if identity is None:
        return False, None
    pid, _ = identity
    return _lock_identity_is_alive(identity), pid


def _status_log_summary() -> dict:
    """Parse the tail of reporter.log for the interval and the last pass's timing/outcome.

    Best-effort: returns an empty dict (never raises) if the log is missing
    or unreadable — --status must degrade gracefully, not crash, when e.g.
    the loop has never been started yet.
    """
    try:
        lines = REPORTER_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    interval = None
    last_start = None
    last_emailed = None
    for line in lines[-500:]:
        m = _LOG_INTERVAL_RE.search(line)
        if m:
            interval = int(m.group(1))
        m = _LOG_RUN_START_RE.match(line)
        if m:
            last_start = (m.group(1), m.group(2))
        m = _LOG_EMAILED_RE.match(line)
        if m:
            last_emailed = (m.group(1), m.group(2), m.group(3))

    result: dict = {"interval": interval}
    if last_start:
        result["last_start_ts"], result["last_start_desc"] = last_start
    if last_emailed:
        ts_str, desc, tag = last_emailed
        result["last_result_ts"] = ts_str
        result["last_result_desc"] = desc
        result["last_result_tag"] = tag
        if interval:
            try:
                completed = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                result["next_due"] = (completed + timedelta(seconds=interval)).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return result


def _build_status_report() -> str:
    """Build the consolidated status report as text — shared by --status and every email.

    Read-only; never raises (each section fails open with an inline error
    note so one bad read can't blank out the rest of the report, and can
    never block an actual report/crash email from going out).
    """
    lines: list[str] = ["=== Vibe-Trading committee_reporter status ==="]
    running, pid = _status_lock_state()
    lines.append(f"Loop: {'RUNNING (pid ' + str(pid) + ')' if running else 'NOT RUNNING' + (f' (stale lock, pid {pid})' if pid else '')}")

    log = _status_log_summary()
    if log.get("last_result_ts"):
        lines.append(f"Last pass: {log['last_result_ts']} -> {log['last_result_desc']} ({log['last_result_tag']})")
    elif log.get("last_start_ts"):
        lines.append(f"Last pass started: {log['last_start_ts']} (still in progress or result not yet logged)")
    else:
        lines.append("Last pass: no reporter.log data found")
    if log.get("next_due"):
        lines.append(f"Next pass due: ~{log['next_due']} (interval {log.get('interval')}s)")

    sys.path.insert(0, str(AGENT_DIR))
    from src.live.halt import halt_flag_set
    from src.trading.service import get_account, get_open_orders, get_positions

    seen_connections: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["connection"] in seen_connections:
            continue
        seen_connections.add(trade["connection"])
        connection = trade["connection"]
        lines.append(f"\nAccount ({connection}):")
        try:
            account = get_account(connection)["account"]
            lines.append(
                f"  Balance: {account.get('balance')}  Equity: {account.get('equity')}  "
                f"Margin level: {account.get('margin_level')}"
            )
        except Exception as exc:
            lines.append(f"  could not read account: {exc}")
            continue
        try:
            positions = get_positions(connection).get("positions", [])
        except Exception as exc:
            lines.append(f"  could not read positions: {exc}")
            positions = []
        if not positions:
            lines.append("  Open positions: none")
        else:
            for p in positions:
                lines.append(
                    f"  OPEN {p.get('side', '?').upper()} {p.get('volume')} {p.get('symbol')} "
                    f"@ {p.get('price_open')} SL {p.get('stop_loss', '?')} TP {p.get('take_profit', '?')} "
                    f"P&L {p.get('profit')}"
                )
        try:
            pending = get_open_orders(connection).get("open_orders", [])
        except Exception as exc:
            lines.append(f"  could not read pending orders: {exc}")
            pending = []
        if pending:
            for o in pending:
                lines.append(
                    f"  PENDING {o.get('side', '?').upper()} {o.get('quantity')} {o.get('symbol')} "
                    f"{o.get('order_type')} @ {o.get('limit_price')}"
                )
        if connection in LIVE_CONNECTIONS:
            try:
                halted = halt_flag_set("mt5")
                lines.append(f"  Kill switch: {'TRIPPED' if halted else 'clear'}")
            except Exception as exc:
                lines.append(f"  could not read kill switch state: {exc}")
            try:
                baseline = _read_live_baseline()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if baseline.get("date") == today and baseline.get("equity"):
                    baseline_equity = float(baseline["equity"])
                    equity = float(account.get("equity") or 0)
                    drawdown = (baseline_equity - equity) / baseline_equity if baseline_equity > 0 else 0
                    lines.append(
                        f"  Today's drawdown: {drawdown:.1%} of baseline ${baseline_equity:.2f} "
                        f"(halts at {LIVE_DRAWDOWN_HALT_PCT:.0%})"
                    )
                else:
                    lines.append("  Today's drawdown: no baseline recorded yet this UTC day")
            except Exception as exc:
                lines.append(f"  could not compute today's drawdown: {exc}")

    seen_symbols: set[str] = set()
    for spec in TARGETS:
        trade = spec.get("trade")
        if not trade or trade["symbol"] in seen_symbols:
            continue
        seen_symbols.add(trade["symbol"])
        try:
            _journal_reconcile_closed(trade["symbol"], trade["connection"])
            summary = _journal_summary_text(trade["symbol"])
        except Exception as exc:
            summary = f"could not read journal: {exc}"
        lines.append(f"\nTrack record ({trade['symbol']}): {summary or 'no closed trades yet'}")

    return "\n".join(lines)


def print_status() -> None:
    """Print a single consolidated status report and exit — read-only, no lock required."""
    print(_build_status_report())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one pass over TARGETS, then exit (default)")
    parser.add_argument("--loop", action="store_true", help="Keep running, repeating every --interval seconds")
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between passes in --loop mode")
    parser.add_argument("--status", action="store_true", help="Print a consolidated status report and exit (read-only, no lock)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    if not _acquire_singleton_lock():
        logger.error(
            "another committee_reporter.py instance is already running (lock at %s) — refusing to "
            "start a second one to avoid racing live trading decisions. If you're certain no instance "
            "is actually running, delete the lock file and retry.",
            LOCK_PATH,
        )
        return 1

    # Load ~/.vibe-trading/.env (falling back to agent/.env, then $CWD/.env) —
    # the same resolution order the vibe-trading app itself uses — so SMTP_*
    # and EMAIL_* set there are picked up without the caller having to export
    # them into the shell first.
    sys.path.insert(0, str(AGENT_DIR))
    from src.providers.llm import _ensure_dotenv

    _ensure_dotenv()

    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        logger.error("missing required environment variables: %s", ", ".join(missing))
        return 1

    if args.loop:
        logger.info(
            "starting loop mode, interval=%ss (profit protection checked every %ss)",
            args.interval, BREAKEVEN_POLL_SECONDS,
        )
        # Two independent cadences share this one loop: the expensive,
        # LLM-costly committee pass on args.interval, and the free,
        # code-only profit protection check (breakeven + early-profit trail)
        # on the much shorter BREAKEVEN_POLL_SECONDS -- see
        # BREAKEVEN_POLL_SECONDS's comment for why waiting for the slow
        # cadence to react would be wrong. A full
        # pass fires immediately on the first tick (last_full_pass starts
        # "due"), matching the loop's original always-run-on-start behavior.
        last_full_pass = time.monotonic() - args.interval
        while True:
            now = time.monotonic()
            if now - last_full_pass >= args.interval:
                last_full_pass = now
                # XAUUSDm (and forex generally) is closed roughly Fri evening
                # through Sun evening -- a pass during that window pays the
                # full 4-agent committee cost for a trade that cannot
                # execute, with no offsetting chance of a missed
                # opportunity. Checked every cycle (not slept-through-to-
                # Monday) so it self-corrects cleanly across restarts/DST
                # without extra scheduling logic.
                if _in_weekend_window(datetime.now(timezone.utc)):
                    logger.info("weekend (UTC) — market closed, skipping this pass")
                    try:
                        _weekend_flatten_and_notify()
                    except Exception:
                        logger.exception("weekend flatten/notify crashed; continuing after the normal interval")
                else:
                    try:
                        run_once()
                    except Exception:
                        # Defense in depth on top of run_once()'s own per-target
                        # try/except: nothing here should ever be able to kill an
                        # unattended loop that nobody is watching in real time.
                        logger.exception("run_once() crashed; continuing after the normal interval")
            else:
                try:
                    _profit_protection_check()
                except Exception:
                    logger.exception("profit protection check crashed; continuing")
            time.sleep(BREAKEVEN_POLL_SECONDS)
    else:
        run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
