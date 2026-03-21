"""
logger.py — Structured trade logging (JSONL) + live console display.

Each trade is logged as a JSON line to logs/trades.jsonl for easy analysis.
Stats (win rate, ROI, etc.) are updated to logs/stats.json after every trade.
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Optional

import config

# Ensure log dir exists
os.makedirs(config.LOG_DIR, exist_ok=True)

_log = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Configure root logger with clean console + file output."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level   = level,
        format  = fmt,
        datefmt = "%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f"{config.LOG_DIR}/bot.log"),
        ]
    )
    # Silence noisy libraries
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def log_trade(
    window_ts:     int,
    direction:     str,        # 'UP' or 'DOWN'
    token_price:   float,
    shares:        float,
    bet_usd:       float,
    confidence:    float,
    score:         float,
    window_delta:  float,
    reasons:       list,
    mode:          str,
    dry_run:       bool,
    # Filled in after resolution:
    result:        Optional[str]  = None,    # 'WIN' or 'LOSS'
    actual_dir:    Optional[str]  = None,    # actual BTC direction
    profit:        Optional[float] = None,
    bankroll_after: Optional[float] = None,
):
    record = {
        "ts":            time.time(),
        "datetime":      datetime.utcnow().isoformat(),
        "window_ts":     window_ts,
        "direction":     direction,
        "token_price":   round(token_price, 4),
        "shares":        round(shares, 2),
        "bet_usd":       round(bet_usd, 2),
        "confidence":    round(confidence, 3),
        "score":         round(score, 3),
        "window_delta":  round(window_delta, 5),
        "reasons":       reasons,
        "mode":          mode,
        "dry_run":       dry_run,
        "result":        result,
        "actual_dir":    actual_dir,
        "profit":        round(profit, 4) if profit is not None else None,
        "bankroll":      round(bankroll_after, 4) if bankroll_after is not None else None,
    }
    with open(config.TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_trades() -> list:
    if not os.path.exists(config.TRADE_LOG_FILE):
        return []
    trades = []
    with open(config.TRADE_LOG_FILE) as f:
        for line in f:
            try:
                trades.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
    return trades


def print_trade_header():
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║       POLYMARKET BTC 5-MIN BOT  —  TRADE MONITOR            ║\n"
        "╚══════════════════════════════════════════════════════════════╝"
    )


def print_signal(
    window_ts: int,
    direction: str,
    confidence: float,
    score: float,
    window_delta: float,
    token_price: float,
    bet_usd: float,
    reasons: list,
    dry_run: bool,
    mode: str,
):
    tag     = "[DRY RUN]" if dry_run else "[LIVE]"
    arrow   = "📈 UP" if direction == "UP" else "📉 DOWN"
    eta     = int((window_ts + config.WINDOW_SECONDS) - time.time())
    bar_len = int(confidence * 20)
    conf_bar = "█" * bar_len + "░" * (20 - bar_len)
    print(
        f"\n┌─────────────────────────────────────────────────────┐\n"
        f"│  {tag}  {arrow}   mode={mode:<10}  T-{eta:02d}s           │\n"
        f"│  Confidence: [{conf_bar}] {confidence:.0%}           │\n"
        f"│  Score: {score:+.2f}   Δ: {window_delta:+.4f}%  Token: ${token_price:.3f}  Bet: ${bet_usd:.2f}  │\n"
        f"└─────────────────────────────────────────────────────┘"
    )
    for r in reasons:
        print(f"  • {r}")


def print_result(direction: str, actual: str, profit: float, bankroll: float):
    won = direction == actual
    icon = "✅ WIN " if won else "❌ LOSS"
    print(
        f"\n  {icon}  Predicted={direction}  Actual={actual}  "
        f"P&L={profit:+.2f}  Bankroll=${bankroll:.2f}"
    )
