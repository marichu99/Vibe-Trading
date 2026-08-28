# Live diversification roadmap (committee_reporter.py)

Researched 2026-08-26, account 134611912 (Exness Kenya, live). Purpose: know
exactly what's ready-to-add vs. what needs real engineering first, so adding
a symbol later is a config change + a sentence, not a research project.

Every number below is a **real min-lot notional**, pulled live from this
account's `symbol_info`, not estimated.

## Tier 1 — ready now (USD-quoted, correct asset-class classification exists)

No code changes needed beyond: extend the mt5 mandate's `asset_classes` /
add a TARGETS entry pointed at `mt5-live-trade`.

| Symbol | Asset class | Min-lot notional | Notes |
|---|---|---|---|
| XAGUSDm (silver) | commodity (already classified) | ~$3,438 | Nearest cousin to gold — same asset class, similar risk order of magnitude (~74% of gold's exposure). Top pick, per earlier discussion. |
| EURUSDm | forex | ~$1,167 | Most liquid FX pair, 24/5, most predictable spread behavior. |
| AUDUSDm | forex | ~$719 | Smaller notional than EUR — genuinely the lowest-exposure major. |
| NZDUSDm | forex | ~$596 | Smallest notional of any major here — but thinner liquidity than EUR/AUD, spread was widest of the USD majors (14 points). |
| GBPUSDm | forex | ~$1,363 | Fine, no particular edge over EUR for this purpose. |
| USOILm | commodity (already classified) | ~$801 | Real commodity option, but 20-point spread — costlier per round-trip than FX majors. |
| UKOILm | commodity (already classified) | ~$854 | Same profile as USOIL, wider spread (35 points) — priciest to trade frequently of anything in this tier. |
| US30m | us_index (already classified) | ~$2,682 | Same session-availability caveat as USTEC/US500 (closed-market dead ticks outside NY hours). |

## Tier 2 — needs an FX-conversion fix first (not dangerous, just non-functional)

These are all quoted in a currency other than USD. The mandate's notional
check (`src.live.sdk_order_gate._normalize_notional`) takes the connector's
raw quote price at face value and calls it `notional_usd` — no currency
conversion happens. Concretely, for these symbols that means:

- **USDJPYm**: native notional ≈ ¥159,068 (real ≈ $1,000 USD) — misread as
  $159,068, ~159x overstated. Would never clear any sane cap.
- **AUDJPYm**: native ≈ ¥114,295 (real ≈ $719) — same problem.
- **USDCADm**: native ≈ C$1,386 (real ≈ $1,000) — ~39% overstated.
- **USDCHFm**: native ≈ Fr803.93 (real ≈ $1,000) — similar overstatement.
- **UK100m**: native ≈ £544.66 (real ≈ $742) — understated this direction, still wrong.
- **AUS200m**: native ≈ A$547.10 (real ≈ $393) — overstated.
- **HK50m**: native ≈ HK$1,796.51 (real ≈ $230) — ~7.8x overstated.

**The failure direction is always safe** (the gate ends up too strict, never
too permissive — it fails toward blocking a trade that should be fine, never
toward waving through something dangerous). But it means these are
effectively **unusable** until someone adds real FX-rate conversion to
`_normalize_notional` (fetch USD/<quote_ccy>, multiply through). Real
engineering work, not a config change.

**Also**, UK100m/AUS200m/HK50m would currently get *mis-classified* as
`AssetClass.US_INDEX` by `src.trading.service._mt5_asset_class`'s catch-all
("anything that isn't metals/forex/oil falls through to US_INDEX") — that's
wrong (they're UK/Australian/HK indices), and needs its own fix independent
of the FX-conversion work: give non-US indices their own `AssetClass` value
(e.g. `INTL_INDEX`) instead of lumping them into `US_INDEX`.

## Recommended order, when the time comes

1. **Silver** — smallest lift, same asset class already authorized in spirit, similar risk profile to what's proven on gold.
2. **EURUSD** — most liquid, most predictable, best 24/5 fit for the 2-hour cadence.
3. **AUDUSD or NZDUSD** — if the goal is smaller notional per trade specifically.
4. Anything in Tier 2 — only after the FX-conversion fix is actually built and tested (this is a real project, budget for it, don't assume it's a config flip).

## What "add symbol X" actually requires each time (Tier 1 only)

1. Real-account risk check: recompute min-lot notional vs. current balance (prices drift — reuse `research_candidates.py` pattern in scratch, or just re-query `symbol_info`).
2. Extend `scripts/commit_mt5_mandate.py`'s `ASSET_CLASSES` list (and re-run it — issues a fresh mandate, doesn't mutate the old one).
3. Add a TARGETS entry in `committee_reporter.py` pointed at `mt5-live-trade`, with `max_stack` sized deliberately (start at 1, same as gold).
4. Restart the loop.

No new connector code, no new tests needed for Tier 1 — the mt5 sdk/gate/journal/circuit-breaker all already generalize across symbols.
