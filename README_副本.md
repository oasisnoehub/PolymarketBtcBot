# Polymarket BTC 5-Min Oracle Lag Bot

Automated trading bot for Polymarket BTC 5-minute prediction markets.
Core edge: **Oracle Lag Arbitrage** — Chainlink updates 10–45 seconds
behind Binance; this bot detects that gap and bets before the market corrects.

---

## Project structure

```
polybot/
├── oracle_lag.py     ★ Oracle lag detector (confidence-scored signals)
├── bot.py            Main trading loop
├── strategy.py       9-indicator signal engine
├── risk.py           Position sizing & circuit breakers
├── oracle.py         Raw Chainlink poller (used by oracle_lag.py)
├── price_feed.py     Binance WebSocket + OKX/Kraken fallback
├── market.py         Polymarket CLOB order placement
├── backtest.py       Walk-forward backtester
├── server.py         Flask + Socket.IO dashboard server
├── dashboard.html    Real-time browser dashboard
├── config.py         All tuneable parameters
├── logger.py         Trade logging
├── setup_creds.py    Interactive credential setup
├── requirements.txt  Python dependencies
├── .env.example      Environment variable template
└── run.sh            Quick-start script
```

---

## Quick start

### 1. Clone & install

```bash
git clone <your-repo> polybot
cd polybot
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set credentials

```bash
cp .env.example .env
python setup_creds.py             # interactive wizard
```

Or edit `.env` directly (see `.env.example` for all fields).

### 3. Verify oracle lag is detectable

```bash
python oracle_lag.py
```

Watch the live output. You should see oracle price + lag seconds printing
every 3s. A signal line prints when confidence ≥ 0.40. **Run this for at
least a few hours before putting real money in** — you want to see how often
high-confidence signals actually appear on your connection.

### 4. Backtest

```bash
# Single mode, 48 hours of data
python backtest.py --hours 48 --mode ultra

# Sweep all parameter combos (saves to logs/sweep_results.json)
python backtest.py --hours 72 --sweep

# Walk-forward validation
python backtest.py --hours 168 --mode ultra --walk-forward
```

### 5. Paper trade (no real money)

```bash
python bot.py --dry-run --mode ultra --strategy oracle_lag --verbose
```

### 6. Launch dashboard

In one terminal:
```bash
python server.py --dry-run --mode ultra
```

Open browser: http://localhost:5000

### 7. Go live (real money)

```bash
python bot.py --mode ultra --strategy oracle_lag
```

---

## Modes

| Mode       | Description                          | Risk level |
|------------|--------------------------------------|------------|
| `safe`     | High threshold, small bets           | Low        |
| `aggressive` | Lower threshold, larger bets       | Medium     |
| `oracle_lag` | Only oracle-lag signals            | Medium     |
| `ultra`    | Oracle lag + adaptive sizing         | High       |
| `degen`    | All signals, max size                | Very high  |

---

## Strategies

| Strategy      | Description |
|---------------|-------------|
| `snipe`       | Enter at T-45s based on TA signals only |
| `oracle_lag`  | Only trade when oracle lag signal fires |
| `combined`    | Oracle lag opportunistic + snipe fallback |

Recommended: `--strategy oracle_lag` until you have 100+ trades of history.

---

## Confidence thresholds (oracle_lag.py)

| Confidence | Suggested bet | Meaning |
|-----------|---------------|---------|
| ≥ 0.85    | 35%           | Extremely strong signal |
| ≥ 0.75    | 25%           | High confidence |
| ≥ 0.65    | 15%           | Good signal |
| ≥ 0.40    | 8%            | Marginal — observation only |
| < 0.40    | 0%            | Skip |

Five scoring dimensions (weighted):
- **Lag duration** 30% — sweet spot 10–35s
- **Price divergence** 30% — ideal 0.08–0.35%
- **Cross-exchange validation** 20% — Binance + OKX + Kraken
- **Tick momentum** 12% — last 10s direction alignment
- **Historical accuracy** 8% — rolling 50-signal win rate

---

## Environment variables

| Variable             | Required | Description |
|----------------------|----------|-------------|
| `POLY_PRIVATE_KEY`   | Yes      | Polygon wallet private key |
| `POLY_API_KEY`       | Yes      | Polymarket API key |
| `POLY_API_SECRET`    | Yes      | Polymarket API secret |
| `POLY_API_PASSPHRASE`| Yes      | Polymarket API passphrase |
| `POLY_FUNDER_ADDRESS`| Yes      | Funded wallet address |
| `POLYGON_RPC`        | No       | Custom Polygon RPC (default: polygon-rpc.com) |

---

## Risk controls (config.py)

- Max drawdown: 50% before auto-stop
- Max consecutive losses: 4
- Daily loss limit: 25% of bankroll
- Profit lock: activates at 130% of starting balance

---

## Important warnings

- Start with `--dry-run` until you understand the system
- Oracle lag opportunities appear **0–5 times per day** — patience required
- Never risk money you cannot afford to lose
- Past backtest performance does not guarantee future results
