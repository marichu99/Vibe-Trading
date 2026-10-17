---
name: price-action-basics
description: Discretionary price-action reference -- support/resistance, trendlines/channels, candlestick anatomy and the 15 classic reversal/continuation patterns, Fibonacci retracement, and session volatility windows. Built for spotting and grading a "documented level + rejection candle" setup on an intraday FX chart, not for computing a numeric indicator.
category: strategy
---
# Price Action & Candlestick Reading

## Purpose

A reasoning reference for reading raw price action -- not a signal engine. Use this to turn "price did something near a level" into a graded, levels-and-candles writeup: where the level actually is, whether price respected or broke it, and whether the candle at that level is a real rejection or just noise. This is the methodology behind the `HTF_LEVEL_REJECTION` edge (documented H4/D1 level + LTF rejection candle, entry on break of the rejection candle) and strengthens the "session support/resistance, recent swing highs/lows" leg of any intraday long/short case.

## Trend Structure

| Trend | Structure | Trendline drawn along |
|---|---|---|
| Uptrend | Higher highs + higher lows | Bottoms (valleys) -- "ascending" |
| Downtrend | Lower highs + lower lows | Tops (peaks) -- "descending" |
| Sideways / ranging | No consistent higher/lower structure | N/A -- trade the range edges instead |

A trend only "officially" changes when the higher-low / lower-high structure itself breaks (not on a single candle poking through a trendline).

## Support & Resistance

- **Resistance**: the highest point reached before price pulled back -- a surplus-of-sellers zone.
- **Support**: the lowest point reached before price turned back up -- a surplus-of-buyers zone.
- **Role reversal**: once resistance breaks, it tends to become support on a retest (and vice versa for support -> resistance). This is *why* a broken level is worth re-entering on a pullback rather than chasing the breakout candle.
- **Strength**: a level gets stronger the more times price tests it without breaking. A level that breaks after many failed tests usually gives a stronger follow-through move (more trapped positions unwinding).
- Draw S/R on the line chart / closes, around clusters of several peaks or valleys at roughly the same price -- not a single touch.

### Two ways to trade a level
1. **The Bounce** (mean reversion): buy as price falls toward support, sell as price rises toward resistance. Needs the level to actually hold -- pair with a rejection candle (see below), don't buy/sell blind into the level.
2. **The Break, conservative version** (momentum + pullback): don't enter on the break itself. Wait for price to pull back and retest the broken level (old resistance now support, or vice versa), then enter on the bounce off that retest. Slower, but avoids buying a level that was never really broken (a fake-out).

## Trendlines & Channels

- Needs **two** touches to draw a trendline, **three** to consider it confirmed/valid.
- The steeper the trendline, the less reliable it is and the more likely it breaks soon -- don't force a trendline through price that doesn't fit it.
- Trendlines get stronger (more respected) the more times they're tested and hold, same logic as horizontal S/R.
- **Channel**: a parallel line drawn at the same angle as the trendline, touching the most recent opposite extreme (most recent peak for an up-channel, most recent valley for a down-channel). Both rails must stay parallel.
  - Bottom of the channel = buy zone, top of the channel = sell zone.
  - Ascending channel (higher highs + higher lows) = bullish; descending channel (lower highs + lower lows) = bearish.
  - Never force price to fit a channel while drawing it.

## Candlestick Anatomy

```
     |  <- upper shadow (session high)
   ┌───┐
   │   │  <- real body (open/close range)
   └───┘
     |  <- lower shadow (session low)
```

- Body color: close > open = bullish/hollow candle; close < open = bearish/filled candle.
- **Long body** = strong directional pressure, the longer the more intense (one side was clearly in control).
- **Short body** = little net buying/selling activity -- indecision.
- **Long shadow** = price traveled well past open/close before reversing within the candle (rejection of that extreme). **Short shadow** = action stayed close to open/close.

## Candlestick Patterns

All of these are about identifying a **genuine rejection vs. noise** -- context (where it sits in the current trend, and whether it's confirmed by the next candle) matters as much as the shape itself.

### Single-candle

| Pattern | Shape | Bias | Context that matters |
|---|---|---|---|
| Doji | Open ≈ close, tiny body | Indecision | After a strong directional run, signals exhaustion of that side |
| Spinning top | Small body, long upper + lower shadow | Indecision | In an uptrend = buyers may be exhausting; in a downtrend = sellers may be exhausting |
| Marubozu (white) | No shadows, open=low, close=high | Strong bullish | At the end of a downtrend = likely reversal; at the end of an uptrend = likely continuation |
| Marubozu (black) | No shadows, open=high, close=low | Strong bearish | At the end of an uptrend = likely reversal; at the end of a downtrend = likely continuation |
| Hammer | Small body top, long lower shadow, little/no upper shadow | Bullish reversal | Only valid **after a downtrend** |
| Hanging Man | Same shape as Hammer | Bearish reversal | Only valid **after an uptrend** -- identical candle, opposite meaning by context |
| Inverted Hammer | Small body bottom, long upper shadow, little/no lower shadow | Bullish reversal | Only valid **after a downtrend** -- sellers tried to push lower but failed to hold the close down |
| Shooting Star | Same shape as Inverted Hammer | Bearish reversal | Only valid **after an uptrend** -- buyers pushed up, got overpowered by sellers into the close |

### Dual-candle

| Pattern | Rule | Bias |
|---|---|---|
| Bullish engulfing | Bearish candle, then a larger bullish candle that fully engulfs its body | Strong up move, best after a downtrend/consolidation |
| Bearish engulfing | Mirror of the above | Strong down move |
| Tweezer tops | 1st candle matches the prior trend, 2nd candle opposes it, both have equal/near-equal highs | Reversal down, after an extended uptrend |
| Tweezer bottoms | Mirror, equal/near-equal lows | Reversal up, after an extended downtrend |

### Triple-candle

| Pattern | Rule | Bias |
|---|---|---|
| Morning star | Bearish candle -> small-body indecision candle -> bullish candle closing past the midpoint of candle 1 | Reversal up (downtrend end) |
| Evening star | Mirror | Reversal down (uptrend end) |
| Three White Soldiers | 3 long bullish candles after a downtrend; each closes near its high, candle 2 and 3 each ≥ the prior body | Strong reversal up |
| Three Black Crows | Mirror, after an uptrend | Strong reversal down |
| Three Inside Up | Long bearish candle at a downtrend bottom -> candle 2 body retraces ≥ its midpoint -> candle 3 closes above candle 1's high | Reversal up, confirms buyers overpowered the downtrend |
| Three Inside Down | Mirror -> candle 3 closes below candle 1's low | Reversal down |

## Putting It Together: Grading a Rejection-Candle Setup

For `HTF_LEVEL_REJECTION` (or any S/R-based entry), score the setup on:
1. **Is the level real?** Multiple prior touches at roughly the same price on H4/D1 (prior day/week high-low, round number, repeated swing point) -- not a single touch.
2. **Is the candle a real rejection?** One of the patterns above, in the right trend context (e.g. hammer only counts after a downtrend into support; shooting star only after an uptrend into resistance). A doji/spinning top at a level is weaker than a hammer/engulfing/marubozu at the same level.
3. **Confluence**: does a trendline, channel rail, or Fib retracement line up with the same level? More confluence = stronger.
4. **Entry**: on the break of the rejection candle's high (long) or low (short), not on the rejection candle itself closing.
5. **If the level already broke**: prefer the conservative pullback entry (retest of the broken level) over chasing the breakout candle.

## Fibonacci Retracement (supplementary confluence)

- Needs a clear **swing high** (a candle with at least 2 lower highs on both sides) and **swing low** (at least 2 higher lows on both sides) to anchor the tool.
- Retracement levels (potential S/R on a pullback): **0.236, 0.382, 0.618, 0.764**.
- Extension levels (potential profit-taking targets): **0.382, 0.618, 1.000, 1.382, 1.618**.
- Works best in a trending market: look for longs at a Fib retracement support in an uptrend, shorts at a Fib retracement resistance in a downtrend. Treat a Fib level the same as any other level above -- it needs a rejection candle, not just proximity, to be actionable.

## Session Volatility Windows (for the "session/timing fit" check)

| Session | Approx. window (GMT) | Character | Best-fit pairs |
|---|---|---|---|
| Sydney | 22:00-07:00 | Lower volume open | AUD/NZD crosses |
| Tokyo (Asian) | 00:00-09:00 | ~20% of daily volume; thinner liquidity, choppier range; can set the day's tone | AUD/USD, NZD/USD |
| London (European) | 08:00-17:00 | Highest-volume session; overlaps Tokyo at the open and New York in the afternoon | EUR/USD, GBP/USD, USD/JPY, USD/CHF, EUR/JPY, GBP/JPY |
| New York | 13:00-22:00 | Second-highest volume; London/NY overlap (13:00-17:00 GMT) is the single highest-volatility window | Majors generally |

For EURUSD/GBPUSD specifically: the London session and the London/New York overlap are where a rejection-candle or breakout setup has the most real participation behind it; a "clean" break during thin Tokyo-session liquidity is more likely to be a fake-out that doesn't hold through London.

## Common Pitfalls

- A trendline drawn too steep, or forced through price that doesn't really fit it, is not a valid trendline -- don't trade off it.
- The same candle shape means opposite things depending on where it sits in the trend (hammer vs. hanging man, inverted hammer vs. shooting star) -- never read a candle shape without the preceding trend.
- One touch does not make a support/resistance level; look for at least two to three.
- A broken level flipping role (resistance -> support or vice versa) is normal and expected, not a sign the level was fake.
- Don't treat Fibonacci levels as self-sufficient signals -- they're confluence for a level that still needs its own rejection candle.
