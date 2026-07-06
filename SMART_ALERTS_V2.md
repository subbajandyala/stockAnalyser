# Smart Alerts v2 — Design & Enhancement Plan

Tab name: **⚡ Smart Alerts Pro**
Screener module: `screener/smart_alerts_v2.py`
App tab variable: `tab11`

---

## What Changes vs v1

| Dimension | v1 (current) | v2 (planned) |
|---|---|---|
| Factors | 8 | 13 |
| Signal gate | Score threshold only | Score + VWAP + VIX + Time gate |
| SL/Target | Fixed 35% / 65% | VIX-adjusted dynamic |
| Alert trigger | 1 scan | 2–3 consecutive scans agree |
| Timeframe | Single TF scan | Dual-TF confirmation (1min + 5min) |
| Expiry awareness | None | Expiry-day vs regular-day logic |
| IV context | None | IV Spike filter + VIX gate |
| Cross-index | None | Conflict check (Nifty vs Banknifty) |
| History | Signal log only | Signal log + outcome tracking (hit SL or target) |

---

## New Factors Added (on top of existing 8)

### Factor 9 — VWAP Position
**Source:** Kite historical API — 1-min candles from 9:15 AM, compute running VWAP
```
VWAP = cumsum(price × volume) / cumsum(volume)
```
| Condition | Direction | Points |
|---|---|---|
| Spot > VWAP by > 0.3% | BULL | +2 |
| Spot > VWAP (mild) | BULL | +1 |
| Spot < VWAP by > 0.3% | BEAR | −2 |
| Spot < VWAP (mild) | BEAR | −1 |

**Why it matters:** BUY CE above VWAP, BUY PE below VWAP — this single filter removes ~35% of false signals.

---

### Factor 10 — India VIX Level
**Source:** `yfinance` `^INDIAVIX` daily close (cached 5 min)
| VIX Range | Signal | Points |
|---|---|---|
| 13 – 18 | Ideal zone — proceed | 0 (no penalty) |
| < 12 | Too calm, range-bound | WAIT override |
| 18 – 22 | Elevated — tighten SL | −1 |
| > 22 | Too expensive — block CE/PE buy | WAIT override |

**Why it matters:** Above VIX 22, option premiums are bloated. You can be right on direction and still lose on theta + vega crush. Below VIX 12, nothing moves enough to recover premium cost.

---

### Factor 11 — IV Spike Detection
**Source:** Kite quote API — compare current ATM option LTP vs 3-scan rolling average LTP
```
iv_ratio = current_atm_ltp / rolling_avg_atm_ltp(last 3 scans)
```
| Condition | Meaning | Action |
|---|---|---|
| iv_ratio > 1.25 | IV spike — someone bought aggressively | +2 in same direction as price |
| iv_ratio > 1.15 | Mild IV expansion | +1 |
| iv_ratio < 0.85 | IV crush — sellers dominant | −1 |
| iv_ratio < 0.75 | Sharp IV collapse | WAIT — premium buying risky |

**Why it matters:** A sudden IV spike means large players just bought options — they know something. Riding that spike gives better entry and exit timing. An IV crush after a big move is the trap — don't buy into it.

**Implementation note:** Store last 3 ATM LTP values in `sa_v2_ltp_history` session state. Compute ratio on each scan.

---

### Factor 12 — Time-of-Day Gate
**Source:** IST system time
| Time Window | Quality | Points |
|---|---|---|
| 9:15 – 9:30 | Opening noise — OI not settled | WAIT override |
| 9:30 – 11:30 | Prime window — highest reliability | +1 bonus |
| 11:30 – 1:30 | Mid-session — normal | 0 |
| 1:30 – 2:15 | Afternoon drift — reduced reliability | −1 |
| 2:15 – 3:00 | Expiry-day spike window (Fridays only) | see Expiry Logic below |
| 3:00 – 3:30 | Last 30 min — too noisy, avoid | WAIT override |

---

### Factor 13 — OI Velocity (Rate of Change)
**Source:** `toi_rows` session state — diff between last 2 snapshots
```
velocity = abs(diff_oi[latest] - diff_oi[prev]) / scan_interval_seconds
```
| Velocity | Meaning | Points |
|---|---|---|
| Very high spike (top 10% vs session avg) | Institutional burst — high conviction | +2 in direction |
| Normal build | Steady accumulation | +1 |
| Flat / noise | No fresh money | 0 |

**Why it matters:** A slow OI build over 30 minutes = positioning. A sudden 5-lakh OI spike in 1 minute = institutional conviction. The latter deserves more weight.

---

## New Signal Gates (Hard Filters)

These override the score and force WAIT regardless of numeric score:

```
GATE 1: Time gate      — Block 9:15–9:30 and 3:00–3:30
GATE 2: VIX gate       — Block if VIX < 12 or VIX > 22
GATE 3: IV crush gate  — Block if iv_ratio < 0.75 (selling into rally)
GATE 4: Consecutive    — Require 2 of last 3 scans to agree on direction
```

Only after all 4 gates pass does the score-based signal fire.

---

## Consecutive Scan Confirmation

Store last 3 signal directions in `sa_v2_direction_history`:

```python
# Each scan stores: "BULL", "BEAR", or "NEUTRAL"
# Signal fires only when:
recent = direction_history[-3:]
bull_count = recent.count("BULL")
bear_count = recent.count("BEAR")

if bull_count >= 2 and score >= 4:    → confirmed BUY CE
if bear_count >= 2 and score <= -4:   → confirmed BUY PE
else:                                  → WAIT (not yet confirmed)
```

This eliminates single-scan noise. A WAIT that's about to flip shows "🟡 Building..." instead of triggering an alert.

---

## Dual Timeframe Confirmation

Run two parallel OI snapshots:
- **Fast scan:** 1-min interval (uses existing `toi_rows`)
- **Slow scan:** 5-min interval (new `sa_v2_toi_5m_rows` session state)

```
Strong signal = both 1-min AND 5-min agree on direction
Weak signal   = only 1-min agrees (shown as tentative)
No signal     = they conflict → WAIT
```

UI shows: `1Min: BULL ↑ | 5Min: BULL ↑ → CONFIRMED` or `1Min: BULL | 5Min: BEAR → CONFLICT → WAIT`

---

## Expiry Day Logic

**Expiry detection:** Check if today is a Friday AND today's date matches the selected expiry.

| Scenario | Logic change |
|---|---|
| Regular day (2+ days to expiry) | Normal score + gates above |
| Expiry day, before 2:00 PM | Reduce CE/PE buy — time decay too fast — require score ≥ 7 instead of 4 |
| Expiry day, 2:00 – 3:00 PM | Prime expiry window — normal threshold (≥ 4) — OI moves are explosive |
| Expiry day, after 3:00 PM | WAIT override — too close to close |

**SL/Target on expiry day:** Tighter exit — SL at −25% (not −35%), Target at +50% (quick flip), exit no matter what by 3:05 PM.

---

## Dynamic SL / Target (VIX-adjusted)

```python
if vix < 14:
    sl_mult, tgt_mult = 0.80, 1.35   # tight SL (−20%), modest target (+35%)
elif vix < 18:
    sl_mult, tgt_mult = 0.75, 1.55   # standard (−25%, +55%)  
elif vix < 22:
    sl_mult, tgt_mult = 0.65, 1.75   # current default (−35%, +75%)
else:
    # VIX gate kicks in — no signal
```

---

## Cross-Index Conflict Check

Fetch OI scores for both NIFTY and BANKNIFTY simultaneously:

```
If Nifty direction == BULL and Banknifty direction == BEAR → CONFLICT → WAIT
If both BULL → high confidence → add +1 to score
If both BEAR → high confidence → add −1 to score  
If one is NEUTRAL → proceed on the selected index alone
```

Only runs when the selected index is NIFTY or BANKNIFTY. SENSEX/BANKEX use BSE instruments and are self-contained.

---

## Signal History with Outcome Tracking

Extend the history record to track what happened after the signal:

```python
history_entry = {
    "ts":        "14:22",
    "signal":    "BUY CE",
    "score":     +5,
    "spot":      24180.0,
    "strike":    24200,
    "ltp_entry": 45.0,
    "sl":        29.25,
    "target":    74.25,
    "ltp_30min": None,   # filled in after 30 min by background scan
    "outcome":   None,   # "TARGET HIT" / "SL HIT" / "OPEN" — auto-detected
    "vix":       15.3,
    "iv_ratio":  1.18,
}
```

The tab shows a colour-coded outcome column: 🟢 TARGET | 🔴 SL | 🟡 OPEN

---

## New UI Layout (Tab 11)

```
┌─────────────────────────────────────────────────────────────────┐
│  CONFIGURATION PANEL (bordered container)                        │
│  Index | Expiry | Interval | [Load] | Updated Xs ago [Analyze]  │
└─────────────────────────────────────────────────────────────────┘

┌───────────────────────────── GATE STATUS ───────────────────────┐
│  🟢 Time Gate  🟢 VIX Gate  🔴 IV Crush  🟢 Consecutive  →  WAIT│
└─────────────────────────────────────────────────────────────────┘

┌─────────────────── SIGNAL BANNER ──────────────────────────────┐
│  BUY CE  24200                           Score: +7 / HIGH      │
│  Buy SENSEX 24200 CE · expiry 11-Jul     VIX: 15.3  IV: 1.18x │
│  ████████████████████░░░░░░░░░  gauge                          │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────────┐  ┌──────────────────────────────────────┐
│  TRADE SETUP          │  │  MARKET CONTEXT                      │
│  Strike / Type /      │  │  Spot / ATM / VWAP / PCR / VIX      │
│  Entry / SL / Target  │  │  Max Pain / CE Wall / PE Wall        │
│  R:R  (VIX-adjusted)  │  │  1Min TF: BULL | 5Min TF: BULL ✓    │
└──────────────────────┘  └──────────────────────────────────────┘

┌─────────────────── FACTOR BREAKDOWN ───────────────────────────┐
│  Factor | Value | Direction | Pts | Interpretation              │
│  ...13 rows...                                                  │
│  Gate overrides shown in red if blocking                        │
└─────────────────────────────────────────────────────────────────┘

┌──────────── SIGNAL HISTORY + OUTCOMES ─────────────────────────┐
│  Time | Signal | Spot | Score | Strike | LTP | VIX | Outcome  │
│  🟢 TARGET  🔴 SL  🟡 OPEN                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## New Session State Keys

| Key | Type | Purpose |
|---|---|---|
| `sa_v2_last_signal` | dict | Latest signal dict |
| `sa_v2_last_fetch` | float | Unix timestamp of last scan |
| `sa_v2_history` | list[dict] | Signal history with outcomes |
| `sa_v2_direction_history` | list[str] | Last 3 scan directions for confirmation |
| `sa_v2_ltp_history` | list[float] | Last 3 ATM LTP values for IV ratio |
| `sa_v2_5m_rows` | list[dict] | 5-min OI snapshot history |
| `sa_v2_5m_last` | float | Timestamp of last 5-min snapshot |
| `sa_v2_vix` | float | Cached VIX value (5-min TTL) |
| `sa_v2_vwap` | float | Intraday VWAP (reset each session) |

---

## New Module: `screener/smart_alerts_v2.py`

Functions to add:

```python
def fetch_vwap(api_key, access_token, symbol) -> float:
    """Fetch 1-min candles from 9:15 AM, compute running VWAP, return latest."""

def fetch_india_vix() -> float:
    """yfinance ^INDIAVIX — cached 5 min."""

def compute_iv_ratio(current_atm_ltp: float, ltp_history: list) -> float:
    """current / rolling_avg(last 3). Returns 1.0 if insufficient history."""

def check_gates(vix, iv_ratio, ist_time, expiry_date) -> dict:
    """Returns {gate_name: bool} — True = gate passes, False = blocks signal."""

def compute_consecutive_confirmation(direction_history: list) -> str:
    """Returns 'BULL' / 'BEAR' / 'NEUTRAL' based on last 3 scans."""

def run_smart_signal_v2(
    api_key, access_token, symbol, expiry,
    instr_df, toi_rows=None, toi_5m_rows=None,
    ltp_history=None, direction_history=None,
) -> dict:
    """
    Enhanced signal. Returns everything v1 returns plus:
    gates, vix, iv_ratio, vwap, tf_agreement, expiry_day_mode,
    sl_mult, tgt_mult, consecutive_confirmed
    """
```

---

## Implementation Sequence

```
Step 1  fetch_india_vix()              — yfinance, trivial
Step 2  check_gates()                  — pure logic, no API
Step 3  compute_iv_ratio()             — needs ltp_history in session state
Step 4  fetch_vwap()                   — Kite historical 1-min candles
Step 5  consecutive confirmation       — session state list management
Step 6  dual TF snapshot              — second toi_rows at 5-min interval
Step 7  expiry day logic               — date comparison
Step 8  dynamic SL/target             — VIX lookup
Step 9  cross-index conflict check     — optional second OI fetch
Step 10 outcome tracking in history    — background LTP check after 30 min
```

Steps 1–5 can be built and tested without adding new Kite API calls.
Steps 6–10 add API calls but all are already available in the existing infrastructure.

---

## Notes / Constraints

- VWAP fetch needs Kite historical API access — requires `api_key` + `access_token` (same credentials already in sidebar)
- VIX from yfinance is daily close, NOT real-time intraday. Acceptable for gate purpose.
- IV ratio uses LTP as proxy for IV — not true Black-Scholes IV, but directionally correct and fast
- Dual-TF scan doubles API calls per refresh cycle — acceptable for 1-min + 5-min combo
- Cross-index conflict check fetches a second index OI — adds ~1-2 sec latency, run async if possible
- Outcome tracking requires a background scan 30 min after signal — can use autorefresh + session state timestamp comparison
