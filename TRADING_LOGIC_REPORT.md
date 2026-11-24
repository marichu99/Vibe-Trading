# Trading Logic Report — Vibe-Trading (fundednext-challenge branch)

Read-only audit. All citations are `relative/path:line` on branch `fundednext-challenge`
at the time of this audit (2026-11-10). Tags: **VERIFIED** (traced in code),
**INFERRED** (likely, not fully traced), **CLAIMED** (only in docs/comments, not
confirmed in code).

No secret values are reproduced anywhere below.

---

## 1. Overview

**System type: mixed.** This is not a single strategy — it is a deterministic,
code-level risk/gating pipeline wrapped around an **LLM-judgment layer** (a
4-agent debate) that only ever operates inside a narrow, hand-coded
**trend-continuation / level-rejection / range-breakout / news-momentum**
taxonomy. The LLM does not compute indicators; indicators, trend, regime,
pullback structure, and every hard risk number are computed in plain Python
and *handed to* the LLM as "verified facts" it is told not to recompute
(`scripts/market_data_pack.py`, `scripts/strategy_tracking.py`). The LLM's job
is to pick a direction/edge from a fixed checklist, propose stop/target
distances, and say PASS most of the time. VERIFIED.

**Two near-identical bots:**
- `scripts/committee_reporter.py` → Exness account (`mt5-live-trade`), symbols
  EURUSD/GBPUSD (gold/silver/AUDUSD commented out in `TARGETS`,
  `scripts/committee_reporter.py:88-254`). VERIFIED.
- `scripts/fundednext_reporter.py` → FundedNext prop-firm challenge account
  (`mt5fn-live-trade`, $6,000 starting balance, 10% target), symbols
  EURUSD/AUDUSD/GBPUSD (gold commented out,
  `scripts/fundednext_reporter.py:128-225`). VERIFIED.

Both share `scripts/strategy_tracking.py`, `scripts/market_data_pack.py`,
`scripts/fundednext_news_calendar.py` (despite the "fundednext" name, it is
the shared news-blackout module for both bots —
`scripts/committee_reporter.py:49`), and the swarm preset
`agent/src/swarm/presets/fx_commodity_day_desk.yaml`. VERIFIED.

**Markets/timeframes:** FX majors (EUR/GBP/AUD vs USD) and, when uncommented,
XAUUSD/XAGUSD. Data pack pulls M15/H1/H4/D1 (`market_data_pack.py:32`); the
decision is explicitly framed as "day trade" — resolve within the current or
next session, not multi-week (`committee_reporter.py:2096-2106`,
`_DAY_TRADE_FRAMING`). VERIFIED.

**One full cycle, new data → decision (committee_reporter.py, trade-enabled
pass):**
1. `run_once`/scheduler fires per session boundary → `run_committee(...)`
   (`committee_reporter.py:2429`).
2. Code-level gates run **before any LLM call**: live circuit breaker
   (`_live_circuit_breaker_check`), exclusive-symbol-group conflict
   (`_exclusive_group_conflict`), news blackout (`fn_news.is_news_blackout`).
   Any hit forces `research_only=True` and a canned `DECISION: PASS` with
   `CONFIDENCE: 0` is returned with **no LLM invocation at all**
   (`committee_reporter.py:2443-2491`). VERIFIED.
3. If none hit: hard trend gate (`strategy_tracking.trend_gate`) restricts
   allowed side when H4+D1 agree; volatility regime classifier
   (`strategy_tracking.regime_for_symbol`) can again short-circuit to a canned
   PASS with no LLM call if CALM/EXTREME (`committee_reporter.py:2493-2566`).
   VERIFIED.
4. `_build_prompt` builds the verified-facts prompt, including a hard gate: if
   a fresh MT5-sourced data pack cannot be built, the pass is a hard PASS and
   the LLM is **never invoked** (`_build_prompt` returns `None` →
   `committee_reporter.py:2152-2155, 2572-2591`). VERIFIED.
5. The swarm runs as an isolated subprocess (`cli run`) through
   `fx_commodity_day_desk.yaml`'s four agents (long/short/risk/head trader),
   produces a machine-parsed decision block. VERIFIED.
6. If `DECISION: LONG/SHORT` and all template conditions hold, the agent calls
   `trading_place_order` with a fixed lot size and a recomputed (distance-
   based) stop/target (`committee_reporter.py:2334-2399`). VERIFIED.
7. The order passes through the mandate gate
   (`agent/src/live/enforcement.py:check_mandate`) before reaching the broker.
   VERIFIED.
8. Post-fill, a chain of deterministic code checks can widen/tighten the stop,
   correct reward:risk, enforce the fixed spec, or outright close the
   position and trip the kill switch (`committee_reporter.py:2801-3337`).
   VERIFIED.

Confidence for this section: **high**.

---

## 2. Market data used

- Source: the broker's **own MT5 feed** via `agent/src/trading/connectors/mt5/sdk.py`
  (`get_historical_bars`, `get_quote`) — never yfinance/futures proxies for
  the executable price (`market_data_pack.py:189-199`, with an explicit
  comment about a past incident where research tools quoted the wrong
  instrument, `committee_reporter.py:825-841`). VERIFIED.
- Timeframes pulled per pass: M15, H1, H4, D1 (`TIMEFRAMES`,
  `market_data_pack.py:32`), plus a 3-bar weekly pull for "previous week"
  levels (`market_data_pack.py:294`). VERIFIED.
- Bars of history: `TREND_BARS = 120` per timeframe for EMA/RSI/ATR/trend
  (`market_data_pack.py:33`); `SWING_LOOKBACK_H1_BARS = 72` for fractal swing
  levels (`market_data_pack.py:34`); regime classifier uses 100 H1 bars +14
  ATR warm-up (`strategy_tracking.py:146-165`, `REGIME_LOOKBACK=100`).
  VERIFIED.
- **Closed-only vs. forming candle:** the data-pack's EMA/RSI/ATR/trend table
  uses `get_historical_bars(..., limit=120)` — this returns the broker's bar
  series up to "now," and nothing in `_bars` (`market_data_pack.py:197-199`)
  explicitly excludes an in-progress bar. Whether the connector's own
  `get_historical_bars` drops the forming bar is **not verified in this
  audit** (would require reading the full MT5 connector's bar-fetch
  internals) — **INFERRED** only (industry-standard MT5 `copy_rates_from`
  usually returns the current, still-forming bar as the last row unless
  explicitly excluded; this module does not filter it out). This is a real
  look-ahead risk worth checking (see §14).
- **Look-ahead discipline in backtests** (separate from live trading): the
  three draft backtest scripts and `exit_replay.py` explicitly reconstruct
  "as the live pass would have seen it" using `bisect.bisect_right` against
  each bar's own close time (`opened_utc`), e.g.
  `scripts/adx_gate_backtest.py:76-78`, `scripts/gartley_pullback_backtest.py:85-87`,
  `scripts/baseline_trend_backtest.py` (same pattern),
  `scripts/exit_replay.py:57-68`. This is a real point-in-time discipline and
  is VERIFIED at the backtest-script level. It does **not** by itself prove
  the *live* `market_data_pack.py` excludes a forming bar — these are two
  separate code paths.
- The committee is explicitly told not to re-download bars for the traded
  instrument and to treat the pack as ground truth
  (`fx_commodity_day_desk.yaml:16` etc.) — a prompt instruction, not a code
  enforcement; nothing stops a sub-agent calling `get_market_data` anyway
  (the tool is in its toolset, `fx_commodity_day_desk.yaml:50`). CLAIMED (the
  "treat as ground truth, don't re-fetch" rule is prompt text only).

Confidence: **medium** (data-pack construction is fully traced; forming-bar
exclusion inside the MT5 connector itself was not read line-by-line).

---

## 3. Every indicator / calculation

All pure-Python, zero LLM cost (`market_data_pack.py` docstring,
line 12-14). VERIFIED for formulas below; all read directly from
`scripts/market_data_pack.py` and `scripts/strategy_tracking.py`.

| Indicator | File:line | Formula | Params | TF | Used in decision? |
|---|---|---|---|---|---|
| EMA | `market_data_pack.py:46-53` | `k=2/(n+1); out[i]=v*k+out[i-1]*(1-k)` | n=20, 50 | M15/H1/H4/D1 | Yes — trend_label, breakeven not used live (see §6) |
| RSI(14) | `market_data_pack.py:56-68` | Wilder-style smoothed avg gain/loss, `100-100/(1+avg_g/avg_l)` | period=14 | each TF | Displayed in data pack only; no explicit RSI threshold gates a decision in code (LLM may reference it as "momentum" context) — **INFERRED not code-gated** |
| ATR(14) | `market_data_pack.py:71-76` | `TR=max(H-L,\|H-prevC\|,\|L-prevC\|)`, mean of last 14 TR | period=14 | each TF (data pack); `ATR_PERIOD="15m"`, `ATR_LOOKBACK_BARS=14` for the stop floor (`strategy_tracking.py:1250-1252`) | **Yes** — stop-loss floor (`_atr_stop_floor`), regime classifier input |
| `trend_label` | `market_data_pack.py:79-89` | up: `price>EMA20>EMA50` and EMA20 rising over 5 bars; down: mirrored; else "mixed" | needs ≥55 closes | H1/H4/D1 | **Yes** — hard trend gate, H1 fallback, baseline comparator |
| Fractal swing highs/lows | `market_data_pack.py:92-101` | bar's high/low is the extreme of a `±width` (2) window | width=2 | H1 | Context only (data-pack "key levels"); HTF_LEVEL_REJECTION edge references these qualitatively, not via a hard code threshold |
| Pullback structure (Gartley) | `market_data_pack.py:109-166` | impulse X→A (extreme-finding walk), reaction B; `retrace=\|A-B\|/\|A-X\|`; zone bands: <1/3 shallow, 1/3–2/3 "gartley", ≤0.786 "deep", else "too deep"; `current_retrace` similarly; volume_ratio = avg tick-volume(reaction)/avg tick-volume(impulse) | `GARTLEY_MIN_RETRACE=1/3`, `GARTLEY_MAX_RETRACE=2/3`, `DEEP_RETRACE_LIMIT=0.786` | H1, trend from H4/D1 agreement | **Yes** — HTF_TREND_CONTINUATION edge requires "gartley" or "deep" zone (checklist item 2, `fx_commodity_day_desk.yaml:186`) |
| Synthetic DXY | `market_data_pack.py:169-178, 36-39` | `50.14348112 * Π(pair_close ^ weight)`, ICE DXY weights | 30-day daily closes | D1 | Context only (no hard gate on DXY value found in code) |
| ADX (Wilder, simplified smoothing) | `strategy_tracking.py:714-766` | standard ±DM/TR/±DI/DX/ADX, but rolling-mean smoothing instead of Wilder's recursive EMA | period=14 (`REGIME_ATR_PERIOD`) | H1 (used by backtest script) | **No** — explicit "DRAFT — not wired into any live gate yet" (`strategy_tracking.py:708-713`). **Dead/unused in production**, exists only for `scripts/adx_gate_backtest.py`. |
| ATR-percentile regime | `strategy_tracking.py:670-705` | rolling ATR(14) over H1 bars, percentile rank of current ATR within trailing 100 values | period=14, lookback=100 | H1 (`REGIME_TIMEFRAME`) | **Yes** — classifies CALM/NORMAL/VOLATILE/EXTREME, drives size multiplier and whether the LLM is even invoked |
| `risk_based_lots` (fixed-fractional sizing) | `strategy_tracking.py:889-931` | `lots = risk_usd/(stop_distance*contract_size)`, rounded down to lot step | — | — | **No** — explicit "DRAFT — not wired into either reporter's live sizing path yet" (`strategy_tracking.py:880-888`). Dead in production; feeds only `scripts/risk_sizing_whatif.py`. |

Confidence: **high**.

---

## 4. Market reading

- **Trend:** `trend_label` (above) on H1/H4/D1. HTF agreement (H4==D1) is the
  primary trend read. VERIFIED.
- **HTF/LTF combination and who wins on disagreement:**
  - `strategy_tracking.trend_gate` (`strategy_tracking.py:550-569`): if H4 and
    D1 **both** say "up," only BUY is allowed that pass; both "down" → only
    SELL; **any disagreement → no restriction at all** (fails open by design,
    comment: "a data error must not silently turn into a trading ban").
    VERIFIED.
  - When H4/D1 disagree, `trend_continuation_fallback`
    (`strategy_tracking.py:610-645`) checks H1 alone: if H1 shows a clear
    single-TF trend, HTF_TREND_CONTINUATION may still fire off H1 alone, but
    **mandatory** 0.5x size and 1.3x stop widening
    (`strategy_tracking.py:648-660`, also mirrored in the regime-override
    block `committee_reporter.py:2519-2527`, which force-relabels the regime
    to VOLATILE for this case). So: **H4/D1 agreement wins when present; H1
    is a lower-conviction fallback, explicitly downgraded in size/stop when
    it is the only signal.** VERIFIED.
- **Pullback/structure/BOS:** `pullback_structure` (§3) is the only
  code-computed "BOS"-adjacent concept — it tracks an impulse's origin/
  extreme and the current reaction's depth. There is no separate explicit
  break-of-structure detector; HTF_LEVEL_REJECTION and SESSION_RANGE_BREAKOUT
  edges are defined only in the LLM prompt (`fx_commodity_day_desk.yaml:189-190`)
  with **no corresponding code-level verification** of "price at a documented
  H4/D1 level" or "breakout with volume > 1.5x the 20-bar average" — those
  thresholds exist **only as prompt text**, not as a computed/enforced fact
  the way the Gartley pullback zone is. CLAIMED (for HTF_LEVEL_REJECTION and
  SESSION_RANGE_BREAKOUT's specific numeric criteria), VERIFIED (for
  HTF_TREND_CONTINUATION's pullback-zone criterion, which is computed in
  `pullback_structure` and injected as a verified fact).

Confidence: **high** for trend gate/fallback; **medium** for the
level-rejection/breakout edges (their numeric thresholds are unenforced LLM
instructions).

---

## 5. Entry logic

Checklist from `fx_commodity_day_desk.yaml:195-213` (head_trader system
prompt — **this is LLM prompt text that the model is instructed to follow,
not code that enforces it item-by-item**; only a subset is independently
re-verified in code post-fill, see §7/§14):

Required (per prompt, any "no"/"unclear" ⇒ PASS):
1. Trade must align with H4/D1 trend, OR qualify under NEWS_MOMENTUM/H1-fallback carve-outs.
2. Edge must be exactly one of: `HTF_TREND_CONTINUATION`, `HTF_LEVEL_REJECTION`,
   `SESSION_RANGE_BREAKOUT`, `NEWS_MOMENTUM` (the last two only ever
   conditionally offered).
3. Specific entry trigger (candle close / level break) must be named.
4. Invalidation level must be a real structure price, not an arbitrary pip count.
5. **Net reward:risk ≥ 1.5 after spread/commission** — prompt instruction,
   also independently **re-enforced in code post-fill**
   (`MIN_REWARD_RISK_RATIO = 1.5`, `committee_reporter.py:411`,
   `_post_trade_reward_risk_check`, `committee_reporter.py:3229-3337`). VERIFIED (code side).
6. Lot size is fixed by account config, not chosen by the LLM.
7. No high-impact news for either currency within 2h — **pre-verified in code**
   before the LLM is even invoked (`fn_news.is_news_blackout`,
   `NEWS_BLACKOUT_WINDOW_MINUTES = 120`, `committee_reporter.py:594, 2458-2464`). VERIFIED.
8. No loss on this symbol in the last 24h (revenge-trade check) — prompt only,
   not separately found enforced in code in the reviewed sections (`bench_reason`/
   `consume_repeat_deviation` in `strategy_tracking.py:981-1050` cover
   "3 consecutive losses" and "repeat deviation," see item 9 below — the
   24h-single-loss "revenge trade" check itself appears to be **prompt-only**).
9. Symbol "benched" after 3 consecutive losses in 24h — **pre-verified in
   code**: `strategy_tracking.bench_reason` is checked by
   `_rulebook_skip_reason` (`committee_reporter.py:2956-2969`). VERIFIED,
   though note `_rulebook_skip_reason`'s call site was not traced all the way
   into `run_committee`'s gate ordering in this audit — flagged as a gap.
10. Not the same setup as the last trade unless items 1-5 are materially different — prompt only.

Entry price: market order, at the live bid/ask fetched just before
`trading_place_order` (`_symbol_live_quote`, `committee_reporter.py:825-853`);
stop/target are **recomputed as a distance from the live quote at execution
time**, not the PM's original absolute price
(`committee_reporter.py:2381-2394`, "STOP-LOSS RECOMPUTATION RULE"). VERIFIED.

Stop distance bounds (code-computed, injected as facts, and
**independently re-checked/corrected post-fill**):
- Floor: `max(1.5 × 15m-ATR(14), 8× live spread)` — `ATR_STOP_MULTIPLE=1.5`
  (`strategy_tracking.py:1252`), `MIN_STOP_TO_SPREAD_RATIO=8.0`
  (`committee_reporter.py:454`). Note: the YAML prompt tells the LLM to use
  "1.2x ATR" (`fx_commodity_day_desk.yaml:206`) while the code's actual floor
  multiple is **1.5x** (`ATR_STOP_MULTIPLE = 1.5`, `strategy_tracking.py:1252`,
  and `_post_trade_stop_floor_check`'s own log line literally says
  "1.5x 15m ATR", `committee_reporter.py:2871`). **Discrepancy flagged**: the
  prompt instructs 1.2x, the code enforces/widens to 1.5x. This is a
  documentation/prompt-vs-code mismatch, not a bug that loses money (code
  wins), but it means the LLM is reasoning against a stated number that
  doesn't match what will actually be enforced.
- Ceiling: `MAX_LOSS_PER_ORDER_USD / (contract_size × lots)` or the tighter
  portfolio-based figure (`_max_stop_distance`, `committee_reporter.py:894-915`).
- Also tightened to at most `MAX_STOP_PRICE_FRACTION=0.01` (1%) of price
  post-fill (`committee_reporter.py:2908-2953`).

Confidence: **high** for the code-enforced items (5, 7, 9, stop floor/ceiling);
**medium-low** for items only ever appearing as prompt text (8, 10, the
specific numeric criteria of edges 2/3).

---

## 6. Exit and risk logic

**Current production exits are plain stop-loss + take-profit, set once at
entry, never actively managed** — this is explicitly locked in the prompt
itself: *"Plain stop and target set at entry; the stop is never widened or
moved; no breakeven, no partials, no trailing; automated exits only (40h max
hold, Friday 20:00 UTC flatten)"* (`fx_commodity_day_desk.yaml:208`). This
matches the code: **`STOP_TRAILING_ENABLED = False`** in both reporters
(`committee_reporter.py:580`, `fundednext_reporter.py:321`). The breakeven
rule, ATR-trail rule, and time-decay-tighten rule (all implemented in
`_profit_protection_check`, `committee_reporter.py:1740-1979`) are **dead code
in production today** — guarded entirely behind that one flag, which was
flipped off 2026-09-28 after a real backtest
(`exit_replay.py`) showed the three stop-moving rules scored **-6.47R total
vs. -0.97R with them off**, on 34 Exness trades (comment,
`committee_reporter.py:569-580`). VERIFIED (both the flag's current value and
the historical data behind the decision).

What **does** still run regardless of the flag:
- **Unconditional time-stop flatten** at `MAX_HOLD_HOURS = 40.0`
  (`committee_reporter.py:683`), checked every `BREAKEVEN_POLL_SECONDS = 300`
  (5 min) poll, independent of P&L (`committee_reporter.py:1815-1858`).
  VERIFIED.
- **Weekend flatten**: closes all live positions from Friday 20:00 UTC
  (`WEEKEND_CUTOFF_UTC_HOUR = 20`) through the weekend
  (`_weekend_flatten_and_notify`, `committee_reporter.py:1592-1633`). VERIFIED.

**Position sizing:** fixed lot per symbol, set in `TARGETS`
(e.g. EURUSDm 0.01 lots on Exness, EURUSD 0.12 lots on FundedNext —
`committee_reporter.py:136`, `fundednext_reporter.py:137`), only ever scaled
by the **regime size multiplier** (0.5x on CALM/VOLATILE,
`strategy_tracking.regime_adjusted_lots`, `strategy_tracking.py:850-877`).
**Not** risk-based/fixed-fractional sizing — stop distance varies trade to
trade while lot size stays nearly fixed, so dollar risk per trade genuinely
varies (acknowledged explicitly as a known gap,
`strategy_tracking.py:880-888`, and quantified by the draft
`risk_sizing_whatif.py` — not live). VERIFIED.

**Risk per trade:**
- Exness: `min(MAX_LOSS_PER_ORDER_USD=$4.00, equity × 10% / 3 concurrent)`
  (`_effective_max_loss_usd`, `committee_reporter.py:401,413-424,856-885`).
- FundedNext: `min($60 mandate ceiling, 0.75% of CURRENT balance)`
  (`fundednext_guardrails.effective_max_loss_usd`, `fundednext_guardrails.py:76,108-130`).
Both VERIFIED, both enforced only as a *prompt-side budget plus post-fill
correction* — the hard backstop is the mandate gate's
`max_loss_per_order_usd` check (`agent/src/live/enforcement.py:495-508`),
which **denies the order outright** (fail-closed) if no stop is attached or
the stop implies more loss than the committed mandate ceiling. VERIFIED.

**Daily/total loss limits:**
- Exness: `DAILY_LOSS_STOP_PCT = 2%` intraday (research-only rest of day, does
  not trip persistent halt) and `LIVE_DRAWDOWN_HALT_PCT = 50%` (persistent
  kill-switch trip + flatten) — both measured against a daily equity baseline
  (`committee_reporter.py:319,600,1384-1484`). Note the 50% halt is far looser
  than the 2% soft stop; the 50% figure reads as a true last-resort backstop,
  not a tuned daily-loss policy.
- FundedNext: `DAILY_LOSS_HALT_PCT = 2.0%` same-server-day (persistent halt +
  flatten, via the kill switch) and `MAX_DRAWDOWN_HALT_PCT = 4.8%` life-of-
  challenge static drawdown vs. `initial_balance_usd`, set once and never
  rebaselined (`fundednext_guardrails.py:69-70,152-206`). These are
  deliberately **tighter** than FundedNext's own real limits (3%/6%) to leave
  margin for slippage. VERIFIED.

**Max open trades:** `MAX_SAME_DIRECTION_POSITIONS = 4` default on Exness
(each target can override via `max_stack`; EURUSD/GBPUSD both use
`max_stack=1` — no pyramiding, `committee_reporter.py:88-158,303`).
FundedNext: `MAX_SAME_DIRECTION_POSITIONS = 1` globally
(`fundednext_reporter.py:251`). Both VERIFIED, both re-checked post-fill in
code (`_post_trade_cap_check`).

Confidence: **high**.

---

## 7. Filters and gates (everything that can block a trade)

All of these run **before the LLM is invoked** except where noted, each
independently capable of forcing a canned PASS with zero LLM cost:

| Gate | Threshold | File:line | Pre- or post-LLM |
|---|---|---|---|
| Kill switch (halt flag) | sentinel file exists | `agent/src/live/halt.py`, checked in `_live_circuit_breaker_check`/`fundednext_guardrails.guardrail_check` | pre |
| Live circuit breaker (daily drawdown) | 2% soft-stop / 50% hard-halt (Exness) | `committee_reporter.py:319,600,1384` | pre |
| FundedNext daily-loss halt | 2.0% same-server-day | `fundednext_guardrails.py:69,152-178` | pre |
| FundedNext static drawdown halt | 4.8% life-of-challenge | `fundednext_guardrails.py:70,181-206` | pre |
| FundedNext soft trade-count ceiling | 10/day (vs. FN's real 200/day) | `fundednext_guardrails.py:84-88,133-149` | pre |
| Exclusive-symbol-group (correlation) | EURUSD/GBPUSD mutually exclusive on both bots | `committee_reporter.py:266,2403-2426`; `fundednext_reporter.py:246` | pre |
| News blackout | 120 min window around any high-impact EUR/USD/AUD/GBP release; **fails closed** if calendar unreachable | `committee_reporter.py:594`, `fn_news.is_news_blackout`, `fundednext_news_calendar.py:398-439` | pre |
| Hard trend gate | H4==D1 restricts side | `strategy_tracking.py:550-569` | pre (modifies prompt, not a hard skip) |
| Volatility regime gate | CALM (<25th pctile) and EXTREME (>90th) skip the LLM entirely; NORMAL/VOLATILE invoke it with size adjustment | `strategy_tracking.py:145-148,190-191,778-800` | pre — CALM/EXTREME are code-level `PASS_NO_LLM` short-circuits |
| Data-pack availability | hard gate — no pack, no LLM call | `committee_reporter.py:2152-2155` | pre |
| Symbol benched (3 consecutive losses/24h) | `strategy_tracking.bench_reason` | `strategy_tracking.py:981-998`, called via `_rulebook_skip_reason` | pre (per module, not fully traced into run_committee's own call order in this audit) |
| Min reward:risk | 1.5:1 net of spread | `MIN_REWARD_RISK_RATIO=1.5`, `committee_reporter.py:411` | prompt instruction + **post-fill correction** (§5/§6) |
| Volatility/spread stop floor | `max(1.5×ATR14, 8×spread)` | `committee_reporter.py:454`, `strategy_tracking.py:1250-1252` | prompt instruction + **post-fill correction** |
| Max stop as % of price | 1% | `MAX_STOP_PRICE_FRACTION=0.01`, `committee_reporter.py:593` | **post-fill only** (tightens, never blocks pre-trade) |
| Confidence floor | <60 ⇒ PASS | `fx_commodity_day_desk.yaml:209,214` | **prompt-only, not independently re-verified in code** anywhere found in this audit |
| Spec match (symbol/lots/order type) | exact match to `TARGETS` | `committee_reporter.py:3076-3167` | **post-fill**, closes + halts on mismatch |
| Research-only trade-placement violation | — | `committee_reporter.py:3170-3226` | **post-fill**, closes + halts |
| Mandate gate (hard backstop) | notional, max-loss-per-order, total exposure, leverage, daily count, funding | `agent/src/live/enforcement.py:394-588` | **at order time**, broker-independent, fail-closed |

Confidence: **high** for the pre-LLM gates (all directly traced in
`run_committee`); **medium** for the prompt-only items (confidence floor,
"same setup" check, "revenge trade" single-loss check) since nothing in the
scripts directory was found independently re-verifying them.

---

## 8. Scoring / weights

There is **no numeric weighted-sum scoring formula** anywhere in this system.
The only "score" concepts are:
- **ATR-percentile regime** (0-100 percentile rank) — a single number, not a
  weighted composite, with fixed hand-tuned band edges
  (`REGIME_CALM_MAX_PERCENTILE=25`, `REGIME_VOLATILE_MIN_PERCENTILE=75`,
  `REGIME_EXTREME_MIN_PERCENTILE=90`, `strategy_tracking.py:145-147`).
  Hand-tuned, not learned. VERIFIED.
- **CONFIDENCE** (0-100): a free-text LLM self-report, with a prompt-only rule
  that <60 forces PASS and a "regime skeptic" instruction telling the model to
  subtract 15 points under certain conditions
  (`fx_commodity_day_desk.yaml:214`). This is **entirely LLM-generated, not
  code-computed, not independently verified** anywhere found in this audit —
  the number the model writes down is trusted at face value for the
  "confidence ≥60" rule. CLAIMED, not VERIFIED (no code path re-derives or
  checks this number).
- Regime **size_multiplier** (0.5 for CALM/VOLATILE, 0.0 for EXTREME, 1.0
  NORMAL) is a fixed lookup table, hand-tuned (`strategy_tracking.py:148,156,
  794-800`), not fit from data.

No weights are "learned" in any statistical sense anywhere in this repo's live
path.

Confidence: **high**.

---

## 9. Learning / AI

- **The LLM debate itself** is the only AI/judgment component in the live
  decision path (4 agents: long_case_analyst, short_case_analyst,
  fx_risk_manager, head_trader, `fx_commodity_day_desk.yaml`). It does not
  learn/adapt across runs — every pass is a **fresh, isolated subprocess**
  with no persisted model state (`committee_reporter.py:14-16`, "a stuck or
  crashed run never takes the reporter down with it" — explicit statelessness
  by design).
- **Trade journal feedback** (`_journal_summary_text`, fed into the prompt as
  `journal_fact`) gives the LLM a short textual summary of recent real
  outcomes — this is **context injected into the prompt**, not a training
  signal; the model cannot update its own weights and the summary is
  deliberately terse "not a richer feedback mechanism"
  (`committee_reporter.py:336-343`). VERIFIED.
- **Model routing** is rule-based, not learned: Haiku by default, Sonnet only
  for live VOLATILE/EXTREME passes (`_model_override_for`,
  `committee_reporter.py:291-296`). Hand-coded, not adaptive.
- **No actual machine-learning model (trained weights, backprop, fitted
  classifier) exists anywhere in the live path.** The regime/ADX/pullback
  "classifiers" are deterministic rule-based thresholds on computed
  indicators, not fitted statistical models.
- The repeated backtests (`exit_replay.py`, `adx_gate_backtest.py`,
  `gartley_pullback_backtest.py`, `baseline_trend_backtest.py`,
  `risk_sizing_whatif.py`) are **human-in-the-loop research tools**: a human
  runs them manually, reads the printed summary, and (in the one case that
  shipped — `STOP_TRAILING_ENABLED=False`) manually edits a constant. There is
  no automatic retraining or feedback loop that changes live behavior without
  a human editing a constant and redeploying. VERIFIED.

Confidence: **high**.

---

## 10. Worked example (real trade, traced step by step)

Using a real closed trade from the Exness journal
(`logs/trade_journal.json`, ticket `1077000511`):

```
symbol=GBPUSDm, side=buy, lots=0.01
entry_price=1.34133, stop_loss=1.339197 (≈23.1 pips), take_profit=1.34478 (≈34.5 pips)
opened_at=2026-11-02T14:07:17Z, regime=VOLATILE, edge=HTF_TREND_CONTINUATION
closed_at=2026-11-04T01:02:39Z, exit_price=1.34478, profit=$3.45, outcome=win
```

1. **Data in:** at pass time, `market_data_pack.build_data_pack("GBPUSDm", "mt5-live-trade")`
   pulls M15/H1/H4/D1 bars (120 each) and a live quote
   (`market_data_pack.py:210-348`).
2. **Indicators:** `trend_label` on H4/D1 closes; both came back "up" (the
   trade's `trend_alignment` field reads `"neutral"` in the journal, which —
   per `strategy_tracking.trend_gate`'s own fail-open design — is consistent
   with H4/D1 either both agreeing, or disagreeing and no restriction being
   applied; the journal's `"neutral"` tag is a separate annotation and was not
   independently re-derived for this audit). `pullback_structure` on H1 bars
   against the H4 trend direction would have classified the current H1
   reaction's retracement into shallow/gartley/deep/too-deep zones.
3. **Read:** `edge=HTF_TREND_CONTINUATION` was named by the head_trader —
   this requires the pullback zone to be "gartley" or "deep"
   (`fx_commodity_day_desk.yaml:186`).
4. **Regime:** classified **VOLATILE** (ATR percentile between 75-90) →
   `size_multiplier=0.5` applied to the base lot size, and the LLM was
   instructed to widen its stop 1.3x versus the standard floor
   (`fx_commodity_day_desk.yaml:212`).
5. **Filters:** no news blackout hit (else this pass would have been a
   code-level PASS with zero LLM cost); no exclusive-group conflict (no GBPUSD
   partner EURUSD open at the time, or this pass itself would have been
   research-only).
6. **Decision:** `DECISION: LONG`, reward:risk ≈ 34.5/23.1 ≈ 1.49:1 — right at
   the 1.5:1 floor (`MIN_REWARD_RISK_RATIO`); if the real net ratio (after
   spread) came in slightly under 1.5, `_post_trade_reward_risk_check`
   (`committee_reporter.py:3229`) would have tightened the stop or widened the
   target automatically after the fill.
7. **Entry/stop/target:** market buy at live ask ≈1.34133; stop 1.339197 (23.1
   pips away, consistent with a widened VOLATILE-regime floor); target
   1.34478.
8. **Outcome:** closed 2 days later (well inside the 40h `MAX_HOLD_HOURS`
   ceiling would NOT apply here if open >40h — this trade ran ~35h, under the
   cap) at the take-profit, +$3.45, tagged `excursion_tag: "clean"` (moved
   favorably without a meaningful adverse excursion first,
   `_classify_excursion`, `committee_reporter.py:1175-1233`).

Confidence: **medium** — the trade's real journal fields are VERIFIED ground
truth; the exact pullback-zone/ATR values *at the time of that specific pass*
were not re-computed in this audit (would require re-querying MT5 historical
bars as of 2026-11-02), so the indicator values quoted in steps 2-4 are
**reconstructed from the code's known behavior, not re-derived from live
data**.

---

## 11. Decision flowchart

```mermaid
flowchart TD
    A[New-candle / scheduled pass] --> B{Kill switch tripped?}
    B -- yes --> Z1[Hard PASS, no LLM, flatten already done]
    B -- no --> C{Daily/static drawdown halt?}
    C -- trips now --> Z2[Trip halt, flatten positions, PASS]
    C -- no --> D{Exclusive-group partner open?}
    D -- yes --> Z3[PASS research-only, no LLM]
    D -- no --> E{News blackout window?}
    E -- yes / calendar unreachable --> Z4[PASS research-only, no LLM]
    E -- no --> F[Compute H4/D1 trend gate]
    F --> G[Compute volatility regime]
    G -- CALM or EXTREME --> Z5[Code-level PASS_NO_LLM, zero cost]
    G -- NORMAL or VOLATILE --> H{Fresh data pack built?}
    H -- no --> Z6[Hard PASS, no LLM]
    H -- yes --> I[Build verified-facts prompt: quote, risk budget,\nATR/spread stop floor, trend rule, regime rule,\njournal summary, news status]
    I --> J[Run 4-agent swarm: long case, short case,\nrisk manager, head trader]
    J --> K{DECISION field}
    K -- PASS --> L[Report PASS + reason, no order]
    K -- LONG/SHORT --> M{All template conditions hold?\nstop given, no opposite position,\nunder max_stack}
    M -- no --> L
    M -- yes --> N[trading_place_order:\nfixed lots, recomputed stop/target\nfrom live quote]
    N --> O[Mandate gate check_mandate:\nnotional, max-loss, exposure,\nleverage, daily count, funding]
    O -- breach --> P[Order denied / paused for reauth]
    O -- allow --> Q[Order filled on broker]
    Q --> R[Post-fill checks:\nspec match, trend-gate compliance,\nstop floor, max-stop%, reward:risk,\nposition-cap re-check]
    R -- violation --> S[Close position immediately,\ntrip kill switch]
    R -- compliant --> T[Journal the trade]
    T --> U[Live position management:\nMAX_HOLD_HOURS=40h unconditional flatten,\nFriday 20:00 UTC weekend flatten\n(breakeven/trail/time-decay DISABLED)]
    U --> V[Position closes at SL, TP, or time-stop]
```

Confidence: **high** (every node traced to code except the exact internal LLM
decision process itself, which is opaque by nature).

---

## 12. Backtests and results

**No conventional vectorized backtest engine drives any live parameter in
this repo's current trading path.** What exists instead is a set of
**draft, uncommitted, journal-replay research scripts** (`git status` at
session start shows these as untracked: `scripts/adx_gate_backtest.py`,
`scripts/gartley_pullback_backtest.py`, `scripts/risk_sizing_whatif.py`;
`scripts/baseline_trend_backtest.py` and `scripts/exit_replay.py` are already
tracked/committed). None are wired into any live decision — each says so
explicitly in its own docstring ("DRAFT / research tool — not wired into
either reporter's live decision path").

**What they actually test, and their data:**
- **`exit_replay.py`** (committed 2026-09-28, the one whose result *did* ship
  live — `STOP_TRAILING_ENABLED=False`): replays each closed journal trade on
  real **M5 bars from MT5**, under 13 candidate exit rules + the current
  plain-stop baseline, simulating fills pessimistically (stop checked before
  target on the same bar; buy exits on bid, sell exits on ask+spread,
  `exit_replay.py:98-108`). This models spread but not slippage/commission
  explicitly beyond that spread adjustment. Real historical data, in-sample
  only (it replays the exact trades that were actually taken — there is no
  held-out validation set; see below).
- **`adx_gate_backtest.py`**: real M15 bars from MT5, point-in-time ADX at
  entry via `bisect.bisect_right` against each bar's own close
  (`adx_gate_backtest.py:76-78`), classifying each of the account's **34 and
  12 real closed trades** (Exness and FundedNext respectively — confirmed by
  reading the committed JSON outputs, `adx_gate_backtest_mt5-live-trade.json`
  has 33 scored rows, `adx_gate_backtest_mt5fn-live-trade.json` has 12) by
  whether ADX would have passed/filtered them, across three candidate
  thresholds (20/25/30).
- **`gartley_pullback_backtest.py`**: same point-in-time discipline,
  reconstructs H4 trend + H1 pullback zone *exactly as the live data pack
  would have computed them* at entry time, via the live `pullback_structure`
  function itself (not a re-implementation) — tests the committee's own
  self-flagged proposal (reject shallow pullbacks).
- **`baseline_trend_backtest.py`**: splits closed trades into "H4/D1 agreed at
  entry" (baseline and real trade are identical by construction — not
  informative) vs. "H4/D1 disagreed" (the only bucket where the LLM's own
  judgment, not the hard trend gate, produced the trade) and reports win rate/
  net/avg-R for each.
- **`risk_sizing_whatif.py`**: replays the real closed journal through
  `risk_based_lots` (fixed-fractional sizing) at a chosen target $ risk,
  scaling the *actual realized* P&L by the lot-size ratio — explicitly **not**
  a re-simulated fill (no re-derived slippage at the hypothetical size,
  acknowledged in its own comment, `risk_sizing_whatif.py:58-65`).

**Real vs. synthetic data:** all five tools read **real MT5 historical bars**
and the **real trade journal** (`logs/trade_journal.json` — 37 entries, all
`status: closed`; `logs/fundednext_trade_journal.json` — 13 entries). Nothing
synthetic. **In-sample only** — every script replays the trades that were
actually taken under the system's own entry rules; there is no out-of-sample
holdout, no walk-forward split, and (per `gartley_pullback_backtest.py`'s own
caveat) the "would filter" buckets are explicit **upper bounds** because a
documented judgment-call exception (rejection candle) cannot be tested from
journal fields alone.

**Sample sizes are small** (33-34 scored trades on Exness, 12 on FundedNext,
per the actual committed JSON outputs) — any conclusion drawn from these
(including the live `STOP_TRAILING_ENABLED=False` decision) rests on a
**very small sample** and should be treated as directional evidence, not
statistically robust proof.

**Trustworthiness:** medium for the point-in-time discipline (genuinely
rigorous, no look-ahead within the backtest scripts themselves) but **low**
for generalizability (tiny in-sample-only replays of a live system's own
historical trades, not independent validation).

Confidence: **high** on what the scripts do; **high** on sample sizes (directly
counted from the committed JSON); **medium** on whether `STOP_TRAILING_ENABLED`'s
one-time flip was the right call long-term, given the sample size.

---

## 13. All tunable numbers

| Name | Value | File:line | Controls |
|---|---|---|---|
| MAX_LOSS_PER_ORDER_USD (Exness) | $4.00 | `committee_reporter.py:401` | per-order risk prompt budget (mirrored in mandate) |
| MAX_LOSS_PER_ORDER_USD (mandate, Exness) | $4.00 | `scripts/commit_mt5_mandate.py:94` | hard gate denial threshold |
| MAX_LOSS_PER_ORDER_USD (mandate, FundedNext) | $60.00 | `scripts/commit_fundednext_mandate.py:111` | hard gate denial threshold |
| MANDATE_MAX_LOSS_PER_ORDER_USD (FN guardrail copy) | $60.00 | `fundednext_guardrails.py:82` | prompt-side mirror |
| RISK_PER_TRADE_FRACTION (FundedNext) | 0.75% of current balance | `fundednext_guardrails.py:76` | per-order risk cap vs. balance |
| PORTFOLIO_RISK_FRACTION (Exness) | 10% of equity | `committee_reporter.py:423` | portfolio-level risk split |
| PORTFOLIO_MAX_CONCURRENT_TRADES (Exness) | 3 | `committee_reporter.py:424` | divides portfolio risk fraction |
| MIN_REWARD_RISK_RATIO | 1.5 | `committee_reporter.py:411`, `fundednext_reporter.py:274` | min net reward:risk, enforced post-fill |
| ATR_STOP_MULTIPLE | 1.5 | `strategy_tracking.py:1252` | ATR-based stop floor (prompt says 1.2x — discrepancy, §5) |
| MIN_STOP_TO_SPREAD_RATIO | 8.0 | `committee_reporter.py:454` | spread's max share of the stop (1/8) |
| MAX_STOP_PRICE_FRACTION | 1% of price | `committee_reporter.py:593` | post-fill stop tightening ceiling |
| GARTLEY_MIN_RETRACE | 1/3 | `market_data_pack.py:104` | pullback-zone lower bound |
| GARTLEY_MAX_RETRACE | 2/3 | `market_data_pack.py:105` | pullback "gartley zone" upper bound |
| DEEP_RETRACE_LIMIT | 0.786 | `market_data_pack.py:106` | pullback "deep" zone upper bound |
| TREND_BARS | 120 | `market_data_pack.py:33` | bars pulled for trend/EMA/RSI/ATR per TF |
| SWING_LOOKBACK_H1_BARS | 72 | `market_data_pack.py:34` | fractal swing lookback |
| REGIME_CALM_MAX_PERCENTILE | 25.0 | `strategy_tracking.py:145` | CALM boundary |
| REGIME_VOLATILE_MIN_PERCENTILE | 75.0 | `strategy_tracking.py:146` | VOLATILE boundary |
| REGIME_EXTREME_MIN_PERCENTILE | 90.0 | `strategy_tracking.py:147` | EXTREME boundary |
| REGIME_VOLATILE_SIZE_MULTIPLIER | 0.5 | `strategy_tracking.py:148` | size cut on VOLATILE/CALM |
| REGIME_LOOKBACK | 100 | `strategy_tracking.py:165` | ATR-percentile rank window |
| REGIME_SKIP_CALM | False | `strategy_tracking.py:190` | whether CALM skips the LLM |
| REGIME_SKIP_EXTREME | True | `strategy_tracking.py:191` | EXTREME always skips the LLM |
| MAX_HOLD_HOURS | 40.0 | `committee_reporter.py:683`, `fundednext_reporter.py:340` | unconditional time-stop flatten |
| TIME_DECAY_START_FRACTION | 0.75 | `committee_reporter.py:684` | when time-decay tightening begins (dead code, see §6) |
| BREAKEVEN_TRIGGER_FRACTION | 0.5 | `committee_reporter.py:567` | breakeven trigger (dead code) |
| EARLY_PROFIT_TRIGGER_USD | $8.00 default, per-target overrides | `committee_reporter.py:649` | ATR-trail arming profit (dead code) |
| STOP_TRAILING_ENABLED | False (both bots) | `committee_reporter.py:580`, `fundednext_reporter.py:321` | master switch for breakeven/trail/decay rules |
| TREND_FILTER_ENABLED | True (both bots) | `committee_reporter.py:588`, `fundednext_reporter.py:329` | hard H4/D1 trend gate on/off |
| NEWS_BLACKOUT_WINDOW_MINUTES | 120 | `committee_reporter.py:594` | news blackout window |
| DAILY_LOSS_STOP_PCT (Exness) | 2.0% | `committee_reporter.py:600` | intraday soft daily stop |
| LIVE_DRAWDOWN_HALT_PCT (Exness) | 50% | `committee_reporter.py:319` | persistent kill-switch trip |
| DAILY_LOSS_HALT_PCT (FundedNext) | 2.0% | `fundednext_guardrails.py:69` | persistent same-day halt |
| MAX_DRAWDOWN_HALT_PCT (FundedNext) | 4.8% | `fundednext_guardrails.py:70` | static life-of-challenge halt |
| MAX_DAILY_TRADES_SOFT_CEILING (FundedNext) | 10/day | `fundednext_guardrails.py:88` | soft pre-trade skip |
| MAX_SAME_DIRECTION_POSITIONS (Exness default) | 4 (overridden to 1 via `max_stack` on live targets) | `committee_reporter.py:303` | pyramiding cap |
| MAX_SAME_DIRECTION_POSITIONS (FundedNext) | 1 | `fundednext_reporter.py:251` | pyramiding cap |
| WEEKEND_CUTOFF_UTC_HOUR | 20 | `committee_reporter.py:525` | Friday flatten hour |
| MAX_ITER (swarm wrapper loop) | 15 | `committee_reporter.py:276` | wrapping-agent iteration cap |
| RUN_TIMEOUT_SECONDS | 3600 | `committee_reporter.py:277` | subprocess kill timeout |
| MAX_ORDER_USD (Exness mandate) | $6,000 | `scripts/commit_mt5_mandate.py:84` | single-order notional cap |
| MAX_LEVERAGE (Exness mandate) | 200 | `scripts/commit_mt5_mandate.py:86` | leverage cap |
| MAX_TRADES_PER_DAY (Exness mandate) | 5 | `scripts/commit_mt5_mandate.py:87` | mandate daily order cap |
| MAX_ORDER_USD (FundedNext mandate) | $45,000 | `scripts/commit_fundednext_mandate.py:93` | single-order notional cap |
| MAX_TOTAL_EXPOSURE_USD (FundedNext mandate) | $75,000 | `scripts/commit_fundednext_mandate.py:98` | total exposure cap |
| MAX_LEVERAGE (FundedNext mandate) | 30 | `scripts/commit_fundednext_mandate.py:99` | leverage cap |
| MAX_TRADES_PER_DAY (FundedNext mandate) | 10 | `scripts/commit_fundednext_mandate.py:100` | mandate daily order cap |
| CHALLENGE_TARGET_PCT | 10.0% | `fundednext_state.py:79` | FundedNext profit target |
| MIN_TRADING_DAYS | 2 | `fundednext_state.py:83` | FundedNext min trading-days rule |
| CONSISTENCY_WATCH_FRACTION | 30% | `fundednext_state.py:95` | informational-only daily-profit-share watch |

Confidence: **high** — every row above was read directly from source.

---

## 14. Weak points

**Bugs / risks worth checking first (ranked):**

1. **Forming-bar inclusion not verified in the live data pack.** `_bars()`
   (`market_data_pack.py:197-199`) filters only on missing OHLC fields, not on
   bar completeness. If `get_historical_bars` returns the currently-forming
   bar as the last row, every EMA/RSI/ATR/trend figure the LLM reasons from
   includes a partial, still-changing candle — a genuine look-ahead-adjacent
   risk in the *live* decision path (distinct from the backtest scripts,
   which are provably point-in-time). **Check this first** — read the MT5
   connector's `get_historical_bars` implementation directly.
2. **Prompt says "1.2x ATR," code enforces 1.5x** (`ATR_STOP_MULTIPLE=1.5`,
   `strategy_tracking.py:1252`, vs. `fx_commodity_day_desk.yaml:206,386`). Not
   dangerous (code wins, and widens rather than narrows the stop), but it
   means the LLM's own stated reasoning uses a number that silently doesn't
   match what gets enforced — a genuine prompt/code inconsistency.
3. **CONFIDENCE is a self-reported, unverified LLM number** that gates a
   "<60 ⇒ PASS" rule purely in the prompt — nothing in code independently
   derives or sanity-checks it. A model that always reports 65 regardless of
   actual conviction would never be caught by this rule.
4. **Dead code risk from flag drift:** `STOP_TRAILING_ENABLED=False` disables
   ~230 lines of breakeven/trail/time-decay logic
   (`committee_reporter.py:1740-1979`) that are fully implemented, tested-
   looking, and could be silently re-enabled by a future edit without anyone
   re-running the backtest that justified turning it off. The 34/12-trade
   sample behind that decision is small.
5. **`risk_based_lots` and ADX gating are fully built but never wired in** —
   real, working code with no live caller
   (`strategy_tracking.py:708-713,880-888`). Low risk (it's inert), but it is
   dead weight that could confuse a future maintainer into assuming it's
   active.
6. **HTF_LEVEL_REJECTION and SESSION_RANGE_BREAKOUT edges have numeric
   criteria ("volume > 1.5x the 20-bar average," "documented H4/D1 level")
   that exist only in the LLM prompt** (`fx_commodity_day_desk.yaml:189-190`),
   with no code-level verification the way the Gartley pullback zone has.
   These two edges are strictly weaker-verified than HTF_TREND_CONTINUATION.
7. **EXCLUSIVE_SYMBOL_GROUP is hand-duplicated across two files** (
   `committee_reporter.py:266` and `fundednext_reporter.py:246`), explicitly
   flagged in comments as a known maintenance hazard — re-enabling a paused
   correlated symbol in one file without updating the other silently disables
   the concentration guard for the new pair.
8. **Two independently-maintained copies of `MAX_LOSS_PER_ORDER_USD`**
   (the prompt-side constant and the mandate-committing script) must be
   kept manually in sync, same pattern/risk as #7 (`committee_reporter.py:376-382`).
9. **"Revenge trade" (item 8) and "same setup as last trade" (item 10) in the
   checklist have no independently found code-level re-verification** —
   they rely entirely on the LLM self-policing, unlike the bench-after-3-
   losses rule (item 9) which is code-enforced.
10. **Backtests are in-sample replays of a small, non-independent trade
    population (33-34 and 12 trades)** — every tuning decision made from them
    (the stop-trailing flip, any future ADX/Gartley-gate decision) is based on
    a thin evidential base; treat conclusions from these scripts as
    directional hints, not statistically validated results.

Other notable points, not necessarily bugs:
- The regime gate's "CALM/EXTREME skip the LLM" short-circuit and the
  research-only short-circuits (news/correlation/circuit-breaker) are a
  genuinely clean design — by construction, no LLM output can ever reach
  `trading_place_order` on those paths, since the canned `CommitteeResult` is
  constructed directly without ever running the subprocess
  (`committee_reporter.py:2443-2491, 2530-2553`). This matches the stated
  intent of commit `e1ba51a7` in the recent git log.
- The mandate gate (`agent/src/live/enforcement.py`) is a well-isolated,
  broker-agnostic, fail-closed pure function — the strongest, most
  trustworthy layer in the whole stack, and it is independent of whatever the
  LLM or the reporter scripts do.

---

## 5 most important things to understand about how this system trades

1. **It is not a self-learning trading bot.** It is a deterministic Python
   risk/gating pipeline that calls an LLM only inside a narrow, fixed
   checklist (4 named edges), and most "intelligence" (trend, regime,
   pullback zone, stop sizing) is computed in code and handed to the LLM as
   facts it must not recompute.
2. **Exits are currently dumb on purpose**: plain stop-loss + take-profit set
   once at entry, with only a 40-hour unconditional time-stop and a Friday
   weekend flatten as automated safety nets. Breakeven/trailing/partial logic
   exists in code but is switched off (`STOP_TRAILING_ENABLED=False`) because
   a real backtest on this account's own 34 trades showed it losing more.
3. **Multiple independent code-level gates can — and routinely do — force a
   zero-cost PASS before the LLM is ever invoked**: kill switch, drawdown
   halts, correlation exclusivity, news blackout, data-pack unavailability,
   and CALM/EXTREME volatility regime. Most "no trade" outcomes never reach
   the LLM at all.
4. **The mandate gate (`agent/src/live/enforcement.py`) is the true hard
   backstop**, independent of everything above it — it fail-closed denies any
   order whose notional, implied max loss, exposure, leverage, or daily count
   exceeds a committed, time-limited, human-approved mandate, regardless of
   what the LLM or the reporter scripts decide.
5. **Sample sizes behind every empirical tuning decision are small** (dozens
   of trades per account) and backtests are in-sample replays of the system's
   own historical trades, not independent out-of-sample validation — treat
   any "this improved outcomes" claim in a code comment as directional
   evidence from a small sample, not proof.

## Open questions only the author can answer

- Does `agent/src/trading/connectors/mt5/sdk.py`'s `get_historical_bars`
  exclude the currently-forming candle? (Not resolved in this audit — see
  Weak point #1.)
- Is the "1.2x ATR" vs. "1.5x ATR" prompt/code mismatch (§5, §14 #2)
  intentional, or should the YAML prompt text be updated to match the code?
- Is `_rulebook_skip_reason` (bench/repeat-deviation check) actually called
  inside `run_committee`'s gate chain before the LLM runs, or only
  conditionally? (This audit found the function and its callers but did not
  fully trace every call site into `run_committee`'s top-level gate ordering.)
- Is there any plan to wire `risk_based_lots` (fixed-fractional sizing) or the
  ADX gate into live decisions, or are they permanently research-only?
- Given the small sample sizes behind `STOP_TRAILING_ENABLED=False`, is there
  a plan to re-test with a larger sample before the account scales up?

## Confidence per section

| Section | Confidence |
|---|---|
| 1. Overview | High |
| 2. Market data used | Medium (forming-bar handling in the MT5 connector not fully traced) |
| 3. Indicators | High |
| 4. Market reading | High (trend gate), Medium (level-rejection/breakout edges) |
| 5. Entry logic | High (code-enforced items), Medium-low (prompt-only items) |
| 6. Exit/risk | High |
| 7. Filters/gates | High (pre-LLM gates), Medium (prompt-only checklist items) |
| 8. Scoring/weights | High |
| 9. Learning/AI | High |
| 10. Worked example | Medium (real journal data; indicator values reconstructed, not re-queried) |
| 11. Flowchart | High |
| 12. Backtests | High |
| 13. Tunable numbers | High |
| 14. Weak points | High |
