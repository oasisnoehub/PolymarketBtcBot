# Polymarket BTC 5-Min Oracle Lag Bot

> Automated trading bot for Polymarket's BTC UP/DOWN 5-minute prediction markets.
> Primary edge: **Oracle Lag Arbitrage** — Chainlink price updates lag 10–45 seconds
> behind CEX consensus. This bot detects that gap and bets before the market corrects.

---

## Table of Contents

1. [What This Bot Does](#1-what-this-bot-does)
2. [How the Edge Works](#2-how-the-edge-works)
3. [System Architecture](#3-system-architecture)
4. [Project Structure](#4-project-structure)
5. [Installation & Credential Setup](#5-installation--credential-setup)
6. [Configuration Reference](#6-configuration-reference)
7. [Running the Bot](#7-running-the-bot)
8. [Trading Strategies Explained](#8-trading-strategies-explained)
9. [Signal Engine Deep Dive](#9-signal-engine-deep-dive)
10. [Risk Management System](#10-risk-management-system)
11. [Live Dashboard](#11-live-dashboard)
12. [Backtesting & Validation](#12-backtesting--validation)
13. [Logs & Trade Output](#13-logs--trade-output)
14. [Troubleshooting](#14-troubleshooting)
15. [Security Checklist](#15-security-checklist)

---

## 1. What This Bot Does

Polymarket's BTC UP/DOWN 5-minute markets are binary prediction markets that resolve every 300 seconds. At the start of each window, a price is locked in. At the end, the market resolves UP if BTC closed higher, DOWN if lower. You buy tokens for either outcome at a price between $0.01 and $0.99 (representing implied probability), and collect $1.00 per token if you're right.

This bot finds two complementary edges in these markets:

**Edge 1 — Oracle Lag Arbitrage (primary)**
The Chainlink oracle that records the "window open price" updates 10–45 seconds behind real-time CEX prices. When BTC moves significantly during that lag window, the oracle hasn't updated yet — but you already know the direction. The token price in the market hasn't fully reflected this information either. You buy before it does.

**Edge 2 — Technical Signal Fusion (secondary)**
When no oracle lag opportunity is present, the bot analyzes 11 indicators (order flow, momentum, funding rate, liquidation cascades, multi-timeframe EMAs, RSI, VWAP, order book imbalance) fused into a single conviction score. It enters at the optimal moment in the last 50 seconds of a window when conviction exceeds the mode's threshold.

The bot manages position sizing via Kelly Criterion, tracks volatility regimes, adjusts sizing based on win/loss streaks, and halts automatically when risk limits are breached.

---

## 2. How the Edge Works

### 2.1 Oracle Lag — Step by Step

```
5-minute window timeline:

  T + 0s   ──── Window opens.
                 Chainlink oracle reads BTC price: $84,200.
                 This becomes the "window open price" — locked.

  T + 28s  ──── BTC moves to $84,326 (+0.15%) on Binance, OKX, Kraken.
                 All three exchanges agree. Move is real.

  T + 30s  ──── Chainlink oracle still shows $84,200. Lag = 30s.
                 Bot detects: lag 30s, divergence 0.15%, 3-exchange agreement.
                 Confidence score: 0.81. Direction: UP.

  T + 31s  ──── Bot buys UP tokens at $0.54 (market underpricing the edge).

  T + 34s  ──── Chainlink updates to $84,326.
                 "Window open price" remains $84,200 (locked at T+0).

  T + 300s ──── Window closes. BTC = $84,410.
                 Market resolves UP. Bot collects $1.00/token.
                 Profit: ($1.00 - $0.54) × shares = ~85% return on bet.
```

The key mechanism: **the window open price is frozen at T+0**. Even after the oracle catches up, the payout is calculated against that original locked price. If BTC moved strongly during the lag, the current price is already biased toward one outcome — but the market's implied probabilities lag behind, creating a pricing inefficiency you can exploit.

### 2.2 The 5-Dimensional Confidence Score

Not every lag event is an opportunity. The bot scores each candidate across five dimensions:

```
Composite Score = Σ(dimension_score × dimension_weight)

┌─────────────────────────┬────────┬──────────────────────────────────────────┐
│ Dimension               │ Weight │ Logic                                    │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Lag Duration            │  30%   │ Ideal: 10–35s. Too short = oracle not    │
│                         │        │ stale yet. Too long = window closing.     │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Price Divergence        │  30%   │ Ideal: 0.08–0.35%. Too small = noise.    │
│                         │        │ Too large = suspicious data error.        │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Cross-Exchange Agreement│  20%   │ Binance + OKX + Kraken must agree on     │
│                         │        │ direction. Filters single-exchange noise. │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Tick Momentum           │  12%   │ Last 10s of real Binance trades confirm  │
│                         │        │ direction (not just quoted price).        │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Historical Accuracy     │   8%   │ Rolling 50-signal win rate fed back as   │
│                         │        │ a Bayesian prior.                         │
└─────────────────────────┴────────┴──────────────────────────────────────────┘
```

Only when the composite score clears the mode's minimum threshold does the bot commit capital.

### 2.3 Confidence → Bet Size

| Confidence | Estimated Win Rate | Suggested Action |
|------------|-------------------|-----------------|
| ≥ 0.85 | ~72% | Full Kelly bet |
| ≥ 0.75 | ~65% | Strong bet |
| ≥ 0.65 | ~59% | Normal bet |
| ≥ 0.50 | ~54% | Minimum bet |
| < 0.50 | < 54% | Skip |

These win-rate estimates are calibrated empirically from backtests — not assumed from theory.

---

## 3. System Architecture

```
                        ┌──────────────────────────────────────┐
                        │              bot.py                  │
                        │  Main loop — one 5-min window/cycle  │
                        │  Orchestrates all modules below      │
                        └──────┬──────────────┬───────────────┘
                               │              │
              ┌────────────────▼──┐    ┌──────▼────────────────────┐
              │   oracle_lag.py   │    │       strategy.py          │
              │                   │    │                            │
              │ Background thread │    │ 11-indicator fusion        │
              │ polls every 2s    │    │ Kelly criterion sizing     │
              │                   │    │ Volatility regime detect   │
              │ 5-dim confidence  │    │ Adaptive threshold         │
              │ scoring           │    │                            │
              └────────┬──────────┘    └──────┬─────────────────────┘
                       │                      │
              ┌────────▼──────────────────────▼─────────────────────┐
              │                   price_feed.py                      │
              │                                                      │
              │  Primary: Binance WebSocket (btcusdt@aggTrade)      │
              │  Fallbacks: OKX REST → Binance REST → Coinbase REST │
              │                                                      │
              │  Maintains: current_price, tick_history (300 ticks) │
              │             klines_cache (1-min candles for TA)     │
              └─────────────────────┬────────────────────────────────┘
                                    │
               ┌────────────────────┼──────────────────────┐
               │                    │                       │
    ┌──────────▼──────┐  ┌──────────▼──────┐  ┌───────────▼──────────┐
    │   oracle.py     │  │   market.py     │  │      risk.py         │
    │                 │  │                 │  │                      │
    │ Chainlink price │  │ Polymarket CLOB │  │ Kelly + drawdown     │
    │ polling (3s)    │  │ Market discovery│  │ Streak multipliers   │
    │ Cross-validates │  │ Order placement │  │ Profit lock          │
    │ with 3 CEXes    │  │ Settlement      │  │ Daily loss limit     │
    └─────────────────┘  └─────────────────┘  └──────────────────────┘
                                    │
               ┌────────────────────┼──────────────────────┐
               │                    │                       │
    ┌──────────▼──────┐  ┌──────────▼──────┐  ┌───────────▼──────────┐
    │   logger.py     │  │   server.py     │  │    backtest.py       │
    │                 │  │                 │  │                      │
    │ JSONL trade log │  │ Flask +         │  │ Walk-forward valid.  │
    │ Stats tracker   │  │ Socket.IO       │  │ Monte Carlo CI       │
    │ Console display │  │ Live dashboard  │  │ Parameter sweep      │
    └─────────────────┘  └─────────────────┘  └──────────────────────┘
```

### Data Flow Per Window

```
Window opens (T+0)
       │
       ▼
Fetch window open price ──────────────────────────────────────────────┐
       │                                                               │
       ▼                                                               │
Oracle lag detector running in background (every 2s)                  │
       │                                                               │
       ├── Lag detected & confidence ≥ threshold? ──► Bet immediately │
       │                                                               │
       └── No lag signal → Wait until T-50s, start snipe loop         │
                 │                                                     │
                 ▼                                                     │
         Every 1.5s until T-4s:                                       │
           Run 11 indicators                                           │
           Compute Kelly-sized bet                                     │
           Score ≥ threshold? ──► Bet now                             │
           Else: wait next tick                                        │
                 │                                                     │
                 ▼                                                     │
         T-4s hard deadline: bet best signal or skip                  │
                 │                                                     │
                 ▼                                                     │
         Wait for window close (T+300s)                               │
                 │                                                     │
                 ▼                                                     │
         Fetch resolution (UP/DOWN) ◄────────────────────────────────┘
               │
               ├── WIN: bankroll += profit, record win streak
               └── LOSS: bankroll -= bet, record loss streak, check risk limits
```

---

## 4. Project Structure

```
polymarket-bot/
│
├── bot.py              Main trading loop. Orchestrates all modules.
│                       Args: --mode, --strategy, --dry-run, --verbose
│
├── oracle_lag.py       Oracle lag detector. Background thread, 5-dim scoring.
│                       Run standalone: python oracle_lag.py
│
├── strategy.py         11-indicator signal fusion + Kelly criterion.
│                       Also: volatility regime detection, adaptive threshold.
│
├── price_feed.py       Real-time BTC price via Binance WebSocket.
│                       Fallback chain: OKX → Binance REST → Coinbase.
│
├── market.py           Polymarket CLOB API wrapper.
│                       Market discovery, order placement, settlement.
│
├── oracle.py           Chainlink oracle price poller (3s interval).
│                       Cross-validates with Binance, OKX, Kraken.
│
├── risk.py             Kelly criterion, drawdown/streak/daily-loss controls,
│                       profit lock, bet sizing pipeline.
│
├── config.py           ALL tunable parameters. Single source of truth.
│                       Edit this to tune indicator weights, thresholds, limits.
│
├── logger.py           JSONL trade log + aggregate stats + console display.
│
├── backtest.py         Historical simulation: walk-forward, Monte Carlo,
│                       parameter sweep, per-regime breakdown.
│
├── server.py           Flask + Socket.IO live dashboard server.
│                       Args: --port, --mode, --dry-run
│
├── dashboard.html      Browser UI: price chart, signals, trade tape, stats.
│
├── setup_creds.py      Interactive wizard to derive Polymarket API credentials
│                       from your private key.
│
├── run.sh              Interactive bash menu (easiest way to launch).
│
├── requirements.txt    Python dependencies.
│
├── .env.example        Environment variable template — copy to .env and fill in.
├── .env                Your actual credentials (NEVER commit this file).
│
└── logs/
    ├── trades.jsonl    Trade history (one JSON record per trade).
    ├── stats.json      Aggregate stats (win rate, ROI, bankroll).
    └── bot.log         Debug and info log.
```

---

## 5. Installation & Credential Setup

### Prerequisites

- Python 3.10 or newer
- A Polygon wallet funded with USDC (Polygon mainnet)
- A Polymarket account using the same wallet

### Install

```bash
git clone <repo-url>
cd polymarket-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configure Credentials

**Option A — Interactive wizard (recommended)**:
```bash
python setup_creds.py
```
The wizard will prompt for your private key, derive the Polymarket API key/secret/passphrase, and write them to `.env`.

**Option B — Manual**:
```bash
cp .env.example .env
# Edit .env and fill in all values
```

**`.env` fields**:

| Variable | Required | Description |
|----------|----------|-------------|
| `POLY_PRIVATE_KEY` | Yes | Polygon wallet private key (`0x...`) |
| `POLY_API_KEY` | Yes | Polymarket CLOB API key (derived from private key) |
| `POLY_API_SECRET` | Yes | Polymarket API secret |
| `POLY_API_PASSPHRASE` | Yes | Polymarket API passphrase |
| `POLY_FUNDER_ADDRESS` | Yes | Your funded wallet address |
| `POLY_SIGNATURE_TYPE` | Yes | Set to `1` for Polygon mainnet |
| `POLYGON_RPC` | No | Custom Polygon RPC (default: `https://polygon-rpc.com`) |
| `STARTING_BANKROLL` | No | Initial USDC allocation (default: `10`) |
| `MIN_BET` | No | Minimum bet per window (default: `1`) |

### Verify Before Going Live

```bash
# 1. Watch oracle lag detection (no money at risk)
python oracle_lag.py
# Run for 1+ hours. Observe: How often do signals appear? What confidence levels?

# 2. Historical simulation
python backtest.py --hours 48 --mode ultra

# 3. Paper trading
python bot.py --dry-run --mode ultra --strategy combined --verbose
```

---

## 6. Configuration Reference

All parameters are in `config.py`. Below are the most impactful ones.

### 6.1 Trading Modes

| Mode | Min Confidence | Kelly Multiplier | Character |
|------|---------------|-----------------|-----------|
| `safe` | 32% | 0.80× | Conservative — skips borderline signals |
| `aggressive` | 25% | 1.00× | Wide net, full Kelly |
| `degen` | 10% | Disabled | Bets on almost everything — research only |
| `oracle_lag` | 50% | 1.20× | Oracle lag signals only, overbet |
| `ultra` | 28% | 1.10× | **Default** — balanced aggression |

### 6.2 Window Timing

```python
WINDOW_DURATION  = 300    # 5 minutes per window (seconds)
SNIPE_START      = 50     # Begin snipe analysis at T-50s before window closes
HARD_DEADLINE    = 4      # Must enter position by T-4s or skip
POLL_INTERVAL    = 1.5    # Seconds between signal re-evaluations
```

### 6.3 Oracle Lag Thresholds

```python
MIN_LAG_SEC   = 8         # Ignore lags < 8s (oracle probably not stale yet)
MAX_LAG_SEC   = 55        # Ignore lags > 55s (window almost closed)
IDEAL_LAG_MIN = 10        # Sweet spot: lower bound
IDEAL_LAG_MAX = 35        # Sweet spot: upper bound

MIN_DIVERGENCE  = 0.03    # Minimum % price difference to count
IDEAL_DIV_MIN   = 0.08    # Sweet spot: lower bound
IDEAL_DIV_MAX   = 0.35    # Sweet spot: upper bound
```

### 6.4 Kelly Criterion

```python
KELLY_FRACTION = 0.40     # Use 40% of full Kelly (conservative fractional Kelly)
KELLY_MIN_EDGE = 0.02     # Require at least 2% positive edge to apply Kelly
KELLY_MAX      = 0.50     # Hard cap: never bet more than 50% of bankroll
KELLY_MIN      = 0.04     # Hard floor: always bet at least 4% when trading
```

### 6.5 Risk Limits

```python
MAX_DRAWDOWN          = 0.55   # Halt if bankroll drops >55% from all-time high
MAX_CONSECUTIVE_LOSS  = 5      # Halt after 5 straight losses
DAILY_LOSS_LIMIT      = 0.28   # Halt if today's loss ≥ 28% of opening bankroll
PROFIT_LOCK_TRIGGER   = 1.40   # Lock profits when bankroll hits 140% of original
PROFIT_LOCK_RESERVE   = 0.35   # Lock 35% of profits once triggered
PROFIT_LOCK_BET_CAP   = 0.20   # Cap bets to 20% of bankroll while lock active
```

### 6.6 Indicator Weights

Defined in `config.py`, consumed by `strategy.py`. Higher weight = stronger vote.

```
Tier 1 — Direct market answer (highest weight)
  window_delta        8.0   BTC price change within the window

Tier 2 — High-edge signals
  funding_rate        3.0   Perp funding rate (contrarian: extreme longs → fade)
  liquidation_cascade 2.5   $500k+ liquidations = momentum continuation
  tick_trend          2.5   Real-time order flow direction

Tier 3 — Technical confirmers
  mtf_alignment       2.0   5m + 15m EMA agree on direction
  micro_momentum      2.0   Last 2 candles direction
  ob_imbalance        2.5   Order book bid vs ask pressure
  rsi                 2.0   Overbought / oversold context

Tier 4 — Supporting context
  ema_crossover       1.0   Short EMA crosses long EMA
  volume_surge        1.0   Unusual volume spike
  vwap_deviation      1.5   Distance from VWAP
  acceleration        1.5   Momentum speeding up or slowing down
  cross_window_streak 1.5   Adjacent window momentum (×3 multiplier cap)
```

---

## 7. Running the Bot

### 7.1 Interactive Menu (Easiest)

```bash
bash run.sh
```

Presents a numbered menu. No command-line knowledge required.

### 7.2 Direct Commands

```bash
python bot.py [--mode MODE] [--strategy STRATEGY] [--dry-run] [--verbose]

# Examples:
python bot.py --dry-run --mode ultra --strategy combined --verbose  # Paper trade
python bot.py --mode ultra --strategy combined                       # Live
python bot.py --mode safe  --strategy oracle_lag                     # Conservative live
```

### 7.3 Other Entry Points

```bash
python oracle_lag.py                          # Monitor lag only, no trading
python server.py --mode ultra --dry-run       # Dashboard only (http://localhost:5000)
python backtest.py --hours 48 --mode ultra    # Historical simulation
python setup_creds.py                         # Credential wizard
```

### 7.4 Recommended Rollout

**Phase 1 — Observation (1–3 days, no money)**
```bash
# Terminal 1
python oracle_lag.py

# Terminal 2
python bot.py --dry-run --mode ultra --strategy combined --verbose

# Terminal 3
python server.py --dry-run --mode ultra
# Open http://localhost:5000
```
Watch for: oracle lag signal frequency, win rate of dry-run signals, risk halts.

**Phase 2 — Live, small bankroll ($10–25)**
```bash
# .env: STARTING_BANKROLL=10, MIN_BET=1
python bot.py --mode ultra --strategy combined
```

**Phase 3 — Scale up**

Only after 100+ live trades with positive expectancy. The Kelly system naturally sizes bets larger as bankroll grows.

---

## 8. Trading Strategies Explained

### 8.1 `oracle_lag` — Pure Arbitrage

Only trades when the oracle lag detector fires a signal above the confidence threshold. Skips all windows where no lag opportunity exists.

```
Background (every 2s):
  CEX price (Binance WS) ──► compare ──► Chainlink price (3s poll)
                                │
                    lag duration + divergence + 3-exchange agreement
                    + tick momentum + historical accuracy
                                │
                    Composite score ──► stored as active signal

Per window:
  Active signal present AND confidence ≥ threshold?
    YES → Bet in signal direction immediately
    NO  → Skip this window
```

**Best for**: Conservative operators, initial live deployment.
**Frequency**: 0–5 opportunities per day.

### 8.2 `snipe` — Technical Analysis Entry

Uses all 11 indicators to find the best entry in the last 50 seconds of each window.

```
T - 50s: Snipe loop begins
    │
    ▼ (every 1.5s)
    Compute all 11 indicators
    Score = Σ(indicator_value × weight)
    Confidence = score / CONFIDENCE_DIVISOR
         │
         ├── Confidence ≥ threshold AND direction clear? → Bet NOW
         └── Not yet → wait for next tick
    │
T - 4s: Hard deadline
    │
    ├── Any signal accumulated? → Place best signal
    └── Nothing? → Skip window
```

**Best for**: High-frequency operation when oracle lag is rare.
**Frequency**: Attempts every window (every 5 minutes).

### 8.3 `combined` — Recommended Default

Checks oracle lag first (higher edge), falls back to snipe.

```
Start of window:
    │
    ▼
Oracle lag signal present AND confidence ≥ threshold?
    YES ──► Bet immediately on oracle lag signal
    NO
    │
    ▼
Wait until T-50s → run snipe loop
    │
    ▼
Snipe generates signal?
    YES ──► Bet at best snipe entry
    NO  ──► Skip window
```

**Rationale**: Oracle lag signals are rarer but carry higher confidence and edge. Snipe fills idle windows, increasing capital deployment without sacrificing quality.

---

## 9. Signal Engine Deep Dive

### 9.1 How Raw Score Is Computed

Each indicator votes in points (positive = UP, negative = DOWN):

```
Example window:

  BTC up 0.18% from window open  →  window_delta    + 8.0 pts
  Funding rate: extreme longs     →  funding_rate    - 3.0 pts  (contrarian)
  3 large liquidations (long)     →  liquidation     + 2.5 pts  (cascade UP)
  Last 10s: 65% buy ticks         →  tick_trend      + 2.5 pts
  5m + 15m EMAs both up           →  mtf_alignment   + 2.0 pts
  RSI = 69 (approaching OB)       →  rsi             - 1.0 pts
  Order book: 60% bids            →  ob_imbalance    + 2.5 pts
  VWAP: price is above            →  vwap_deviation  + 1.5 pts
  Volume: 2× average              →  volume_surge    + 1.0 pts
  2-window win streak             →  cross_window    + 1.5 pts

  Raw score = 8.0 - 3.0 + 2.5 + 2.5 + 2.0 - 1.0 + 2.5 + 1.5 + 1.0 + 1.5
            = 17.5  →  Direction: UP
```

### 9.2 Confidence & Win Rate Calibration

```
confidence = raw_score / CONFIDENCE_DIVISOR  (default: 9.0)
# e.g., 17.5 / 9.0 = well above 1.0 → capped at 1.0

# Empirical win-rate lookup (calibrated from backtests):
┌────────────┬───────────────────┐
│ Confidence │ Estimated Win Rate│
├────────────┼───────────────────┤
│ ≥ 0.80     │ 72%               │
│ 0.70–0.79  │ 65%               │
│ 0.60–0.69  │ 59%               │
│ 0.50–0.59  │ 54%               │
│ < 0.50     │ skip              │
└────────────┴───────────────────┘
```

These are used as `p` (win probability) in the Kelly formula — not assumed from theory.

### 9.3 Kelly Criterion Formula

```
Variables:
  p = estimated win rate (from calibration above)
  q = 1 - p
  b = (1 - token_price) / token_price    ← payout per dollar bet

Kelly fraction:
  f* = (p × b - q) / b

Fractional Kelly (40% of full):
  f_base = 0.40 × f*

With mode multiplier (ultra = 1.10):
  f_mode = f_base × 1.10

With streak adjustment:
  Win streak (4+): f_final = f_mode × 1.30
  Loss streak (4+): f_final = f_mode × 0.40

Bet amount = f_final × current_bankroll
Clamped to: [MIN_BET, min(KELLY_MAX × bankroll, MAX_BET)]
```

### 9.4 Volatility Regime

ATR (Average True Range) over the last 14 candles determines the regime:

```
ATR < 15         →  Quiet    →  bet × 0.40  (market too calm, signals unreliable)
ATR 15–35        →  Low      →  bet × 0.70
ATR 35–350       →  Ideal    →  bet × 1.00  (normal conditions)
ATR 350–800      →  Volatile →  bet × 0.75  (some caution)
ATR > 800        →  Extreme  →  bet × 0.55  (chaotic, reduce exposure)
```

### 9.5 Adaptive Confidence Threshold

The minimum confidence to place a bet is not fixed — it tightens after losses:

```
threshold = BASE_THRESHOLD + (consecutive_losses × 0.05)

# ultra mode base = 0.28
# After 0 losses:  threshold = 0.28
# After 2 losses:  threshold = 0.38
# After 4 losses:  threshold = 0.48
```

This prevents the bot from chasing bad variance by lowering the bar when it's struggling.

---

## 10. Risk Management System

### 10.1 Hard Stops (Auto-Halt)

The bot stops trading and exits when any of these trigger:

| Condition | Default Trigger | Why |
|-----------|----------------|-----|
| Peak drawdown | > 55% from all-time high | Prevent catastrophic loss |
| Consecutive losses | 5 in a row | Signal or market regime may have broken |
| Daily loss | ≥ 28% of opening bankroll | Prevent single-day blowup |
| Min bankroll | Below $1 | Cannot place minimum bets |

After a halt, the bot exits cleanly. Your USDC remains in your wallet. Investigate before restarting.

### 10.2 Profit Lock

When you're winning, the system protects gains automatically:

```
1. Trigger: bankroll ≥ 140% of original deposit
2. Lock: 35% of profits are notionally reserved
3. Effect: future bets are capped at 20% of bankroll
4. Purpose: a losing streak cannot erase a big run entirely
```

### 10.3 Bet Sizing Pipeline

```
1. Signal provides Kelly fraction
       │
       ▼
2. Apply mode multiplier (safe=0.80, ultra=1.10, oracle_lag=1.20)
       │
       ▼
3. Apply volatility regime multiplier (0.40–1.00)
       │
       ▼
4. Apply streak multiplier (+30% win / -60% loss)
       │
       ▼
5. Apply profit lock cap (20% max if active)
       │
       ▼
6. Clamp to [MIN_BET, min(50% × bankroll, bankroll)]
       │
       ▼
7. Final bet amount in USDC
```

### 10.4 Streak Multipliers

| Streak | Effect on Bet Size |
|--------|-------------------|
| 5+ wins | +40% |
| 4 wins | +30% |
| 3 wins | +20% |
| 2 wins | +10% |
| Neutral | 0% |
| 2 losses | -15% |
| 3 losses | -30% |
| 4+ losses | -60% |

---

## 11. Live Dashboard

### Launch

```bash
# Terminal 1: bot
python bot.py --mode ultra --strategy combined --dry-run

# Terminal 2: dashboard
python server.py --mode ultra --dry-run

# Browser
open http://localhost:5000
```

Custom port:
```bash
python server.py --port 8080 --mode ultra --dry-run
```

### Dashboard Panels

**BTC Price Chart** — Live rolling chart of the last 300 price ticks (~5 minutes of data).

**Oracle Lag Panel**:
- Current lag duration in seconds
- Price divergence between oracle and CEX (%)
- Composite confidence score (0.0–1.0)
- Active signal direction (UP / DOWN) and when it was detected

**Signal Panel**:
- Current strategy signal direction and confidence
- Per-indicator breakdown: which fired, which vetoed, point contributions
- Estimated win probability and Kelly-sized bet amount
- Regime label (Quiet / Ideal / Volatile / Extreme)

**Trade Tape** — Last 200 trades:
- Window timestamp, direction bet, token price paid
- Shares, bet size in USDC
- Result (WIN/LOSS), profit/loss
- Running bankroll after each trade

**Stats Bar**:
- Total trades, all-time win rate, total ROI
- Current win streak / loss streak
- Today's P&L vs daily loss limit

### REST API Endpoints

```
GET /api/state       Full bot state snapshot (JSON)
GET /api/polymarket  Current window market info (UP/DOWN prices, liquidity)
GET /api/trades      Full trade history (JSON array)
```

---

## 12. Backtesting & Validation

### 12.1 Basic Backtest

```bash
python backtest.py --hours 48 --mode ultra
```

Fetches 48 hours of 1-minute BTC candles (OKX → Binance fallback), groups into 5-minute windows, simulates the bot's decision logic, and outputs:
- Win rate and total ROI
- Sharpe ratio
- Maximum drawdown
- Per-regime performance breakdown (Quiet / Ideal / Volatile / Extreme)

### 12.2 Walk-Forward Validation

```bash
python backtest.py --hours 120 --mode ultra --walk-forward
```

Trains on rolling prior windows, validates on unseen holdout periods. Prevents in-sample overfitting. This is the most honest estimate of expected live performance.

### 12.3 Monte Carlo Confidence Intervals

```bash
python backtest.py --hours 48 --mode ultra --monte-carlo
```

Bootstraps trade outcomes 1,000 times and reports 95% confidence intervals on final ROI. Answers: "Could these results be explained by luck?"

### 12.4 Parameter Sweep

```bash
python backtest.py --hours 72 --sweep
# Results saved to logs/sweep_results.json
```

Grid-searches indicator weight combinations to find the highest risk-adjusted return. **Always follow up with walk-forward validation** to confirm sweep results aren't overfit.

---

## 13. Logs & Trade Output

### Trade Log Format

Each trade appends one JSON line to `logs/trades.jsonl`:

```json
{
  "timestamp": "2025-03-15T10:30:00Z",
  "window_ts": 1742035800,
  "direction": "UP",
  "token_price": 0.52,
  "shares": 19.23,
  "bet_usd": 10.0,
  "confidence": 0.71,
  "score": 6.4,
  "window_delta": 0.18,
  "reasons": ["window_delta+8.0", "tick_trend+2.5", "funding-1.5"],
  "mode": "ultra",
  "dry_run": false,
  "result": "WIN",
  "actual_direction": "UP",
  "profit": 9.08,
  "bankroll_after": 59.08
}
```

### Quick Analysis

```bash
# Win rate and total profit
python -c "
import json
trades = [json.loads(l) for l in open('logs/trades.jsonl')]
wins = [t for t in trades if t['result'] == 'WIN']
profit = sum(t['profit'] for t in trades)
print(f'Win rate: {len(wins)}/{len(trades)} = {100*len(wins)/max(len(trades),1):.1f}%')
print(f'Total profit: \${profit:.2f}')
"

# Live trade stream
tail -f logs/trades.jsonl | python -m json.tool

# Bot debug log
tail -f logs/bot.log
```

### Aggregate Stats (`logs/stats.json`)

Updated after every trade:
```json
{
  "total_trades": 147,
  "wins": 84,
  "losses": 63,
  "win_rate": 0.5714,
  "roi": 0.234,
  "bankroll": 62.34
}
```

---

## 14. Troubleshooting

### "No market found for this window"

Polymarket hasn't listed the BTC 5-min market for the current window, or the slug format changed.

- Check that the market exists at `polymarket.com`
- Inspect what slug `market.py` is constructing
- Verify your system clock is accurate (NTP sync)

### "Oracle price unavailable"

Chainlink RPC call is failing.

- Default RPC (`https://polygon-rpc.com`) may be rate-limiting your IP
- Set `POLYGON_RPC` in `.env` to a dedicated endpoint (Alchemy, QuickNode, Infura)

### "WebSocket disconnected"

Binance WebSocket dropped. The feed auto-reconnects — watch for `Reconnecting...` in logs. If it stays disconnected:
- Check internet connectivity
- Check Binance status page
- The bot falls back to REST polling automatically

### Risk halt triggered

One of the drawdown/loss limits was hit.

- Read `logs/trades.jsonl` to understand the sequence of losses
- Read `logs/bot.log` for the specific halt reason
- Don't lower limits reflexively — they exist for a reason
- Only restart after understanding what changed

### "Order placement failed"

- Ensure your wallet has USDC on Polygon mainnet (not Ethereum)
- Verify credentials in `.env` are correct and not expired
- Run `python setup_creds.py` to regenerate API credentials
- Check Polymarket CLOB status

### Low signal frequency

- Switch to `--strategy combined` to use both oracle lag and snipe
- Check oracle lag detection with `python oracle_lag.py` — if no signals appear, your connection may have elevated latency
- Oracle lag signals are naturally rare during low-volatility BTC periods

---

## 15. Security Checklist

**Before live deployment:**

- [ ] `.env` is in `.gitignore` — never commit it (already configured, do not remove)
- [ ] Private key lives only in `.env`, never in any source file
- [ ] Use a dedicated wallet for this bot — not your main holdings wallet
- [ ] `STARTING_BANKROLL` is set to an amount you can afford to lose entirely
- [ ] Run `--dry-run` for at least 24 hours before going live
- [ ] Run `python backtest.py` and review results honestly before going live
- [ ] Understand the risk controls in `config.py` before changing them

**Ongoing:**

- [ ] Monitor `logs/trades.jsonl` and `logs/bot.log` daily
- [ ] If win rate drops below 45% over 50+ live trades — pause and diagnose
- [ ] Keep your private key and API credentials secret; rotate them if compromised
- [ ] Prediction markets are inherently speculative; past performance does not guarantee future results

---

## Quick Reference

```bash
# First-time setup
python setup_creds.py                                          # Configure credentials
python oracle_lag.py                                           # Watch for signals (no money)
python backtest.py --hours 48 --mode ultra                     # Historical validation

# Paper trading
python bot.py --dry-run --mode ultra --strategy combined --verbose
python server.py --dry-run --mode ultra                        # http://localhost:5000

# Live trading
bash run.sh                                                    # Interactive menu
python bot.py --mode ultra --strategy combined                 # Direct

# Monitoring
tail -f logs/trades.jsonl                                      # Live trade stream
tail -f logs/bot.log                                           # Debug log
python server.py --mode ultra                                  # Dashboard

# Advanced backtesting
python backtest.py --hours 120 --walk-forward                  # Walk-forward validation
python backtest.py --hours 72 --sweep                          # Parameter grid search
python backtest.py --hours 48 --monte-carlo                    # Confidence intervals
```

---

> **Disclaimer**: This bot trades real money on prediction markets. Prediction markets are
> speculative instruments. Oracle lag opportunities may disappear as markets become more
> efficient. Always start with `--dry-run`. Never risk capital you cannot afford to lose.
