# 1. BTST option confluence (Quantsapp)

Added 2026-10-31 · Option buying · positional (overnight) · NIFTY · 1 lot · **not implemented**

Source: Quantsapp strategy library (web.quantsapp.com), pre-built, default settings.

## Rules
1. Skip the day if yesterday's India VIX closed above 20, or if yesterday was a futures expiry
   (the rolled futures series jumps and disturbs the indicators).
2. At 15:00, five votes on the near-month NIFTY future:
   - price above / below the session VWAP
   - RSI(14) on 15-minute closes above 55 (bull) / below 45 (bear)
   - yesterday's daily ADX(14) above 20, voting with the larger DI line
   - 15-minute EMA 9 above / below EMA 21
   - 15-minute Supertrend(10, 3) up / down
3. 4 or more bull votes → buy the CE one strike above ATM; 4 or more bear votes → buy the PE one
   strike below ATM. 1 lot. Weekly expiry that does not expire today or the next session.
4. No stop by default (optional stop as % of premium). Hold overnight.
5. Sell at 09:40 the next trading session. One trade a day at most.

Settings: entry time, exit time, min score (3–5), VIX max, RSI n / bull / bear, ADX min,
EMA fast / slow, Supertrend period / multiplier, bar minutes, stop %, lots.

## Source backtest (1 Oct 2025 – 30 Sep 2026, NIFTY, 1 lot, defaults)
- 142 trades, win 48%, profit factor 1.34
- +₹70,171 before costs; about +₹61,620 after ₹20/order and bid–ask slippage (statutory charges not included)
- Avg winner ≈ ₹4,031, avg loser ≈ ₹2,756; best trade +₹35,597; worst −₹9,753
- Max drawdown −₹36,244; 7 of 12 months profitable; longest losing run 8 trades
- **Without the best 15 trades: −₹96,973** (edge lives in a few big nights)
- Monthly: Oct −10.9k, Nov −3.3k, Dec +8.5k, Jan +6.4k, Feb +36.6k, Mar +33.1k, Apr −17.5k,
  May +26.2k, Jun +1.2k, Jul −17.9k, Aug −6.5k, Sep +14.3k

| Condition | P&L | Trades | Won |
|---|---|---|---|
| NIFTY rose > 1% while held | +₹52.3k | 16 | 69% |
| NIFTY within ±1% while held | −₹68.1k | 95 | 39% |
| NIFTY fell > 1% while held | +₹76.1k | 28 | 64% |
| NIFTY above 20-day average (uptrend) | −₹29.5k | 80 | 44% |
| NIFTY below 20-day average (downtrend) | +₹98.1k | 61 | 52% |
| VIX < 11.5 / 11.5–14.5 / > 14.5 | +13.7k / −14.9k / +69.8k | 41 / 71 / 29 | 46 / 42 / 62% |
| Expiry day / other days | −4.0k / +72.5k | 27 / 114 | 48 / 47% |
| Quiet session (< 0.75% range) / trend day / choppy | −10.5k / +64.6k / +14.5k | 58 / 50 / 33 | 36 / 60 / 48% |
| Entry day closed above / below its open | −1.1k / +69.7k | 65 / 76 | 37 / 57% |
| Gap up / flat / gap down open | +2.2k / +67.7k / −1.4k | 13 / 110 / 18 | 46 / 50 / 33% |
| Mon / Tue / Wed / Thu / Fri | +41.3k / −1.2k / −25.4k / +12.7k / +41.1k | 28/26/23/29/34 | 54/50/39/52/41% |

## Assessment (2026-10-31)
**Positives**
- Positive after costs over a full year; very low charges (~12 trades a month, 2 orders each).
- Risk capped at the premium paid; small capital.

**Concerns**
- Profit concentrated in a few nights (negative without the best 15); big drawdowns and losing months.
- Mostly a bearish edge from a falling year (uptrend −₹29.5k, downtrend +₹98.1k): likely to lose in a rising market.
- Flat nights (95 of 142) lost ₹68k: overnight time decay works against every trade.
- The "works best when" splits are after the fact — filters picked from the same year are curve-fitting.
- The five votes come from the same price data, so "4 of 5 agree" is weaker than it looks.
- LTP / market fills flatter the 09:40 gap-morning exit.

**Fit for MarketPulse**
- Not a replacement for the intraday candle bot (different style, doesn't fix its problems).
- Possible small separate add-on: 1 lot, one machine, kept out of the candle bot's 15:15
  square-off and position handling.

**Lessons for the intraday bot**
- Trend context (trend day vs quiet day) matters most — supports the day-trend filter idea;
  its 5-vote score is a candidate for that filter.
- Fewer, higher-conviction trades beat many small ones once charges count.

**Next checks before any use (untested — run on Quantsapp, which has expired-option prices)**
1. The previous year (Oct 2024 – Sep 2025): does it hold outside the year it was described on?
2. Min score = 5, and a 30% premium stop: how much edge survives?
3. Each run after costs and without the best 10% of trades.
Paper trial only if it stays positive on both years without its best nights.
