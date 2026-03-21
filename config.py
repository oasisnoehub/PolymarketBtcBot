"""
config.py — ULTRA configuration for the Polymarket BTC 5-min bot.

KEY ALPHA SOURCES (ranked by edge/frequency):
  1. Window Delta         — Direct answer to market question, dominant
  2. Perp Funding Rate    — Contrarian signal; crowded longs/shorts get wrecked
  3. Liquidation Cascade  — Large wicks attract more liquidations; continuation
  4. Oracle Lag           — Near-free money when Chainlink lags 10-45s
  5. MTF Trend Alignment  — 1m + 5m + 15m EMAs must agree for high confidence
  6. Tick Flow            — Real-time WS trades; informed order flow
  7. Cross-Window Streak  — Momentum from adjacent windows (trending markets)
  8. OB Imbalance         — Polymarket order book bid/ask depth skew
  9. VWAP Extension       — Price stretched away from value = continuation/reversal

RISK: Kelly Criterion with fractional sizing + streak management + profit lock.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────
# Polymarket API
# ─────────────────────────────────────────────
POLY_PRIVATE_KEY     = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY         = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET      = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE  = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS  = os.getenv("POLY_FUNDER_ADDRESS", "")
POLY_SIGNATURE_TYPE  = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

GAMMA_API            = "https://gamma-api.polymarket.com"
CLOB_API             = "https://clob.polymarket.com"


# ─────────────────────────────────────────────
# Price feeds
# ─────────────────────────────────────────────
BINANCE_REST         = "https://api.binance.com"
BINANCE_WS           = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_SYMBOL       = "BTCUSDT"
OKX_REST             = "https://www.okx.com"
KRAKEN_REST          = "https://api.kraken.com"

CHAINLINK_ORACLE_URL = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
CHAINLINK_CONTRACT   = "0xc907E116054Ad103354f2D350FD2514433D57F6F"


# ─────────────────────────────────────────────
# Window timing
# ─────────────────────────────────────────────
WINDOW_SECONDS       = 300          # 5-minute windows
SNIPE_OFFSET         = 50           # Start analysis at T-50s (earlier = more signal)
HARD_DEADLINE        = 4            # Force trade at T-4s if nothing fired
POLL_INTERVAL        = 1.5          # Re-evaluate every 1.5s
ORACLE_LAG_MIN_SEC   = 8
ORACLE_LAG_MAX_SEC   = 55
ORACLE_MIN_DIVERGENCE = 0.03        # 0.03% min divergence to signal


# ─────────────────────────────────────────────
# NEW: Perpetual Futures Funding Rate Signal
# ─────────────────────────────────────────────
# BTC perp funding rate = what longs pay shorts every 8h
# Strongly positive → longs crowded → fade → DOWN signal
# Strongly negative  → shorts crowded → fade → UP signal
# We fetch from Binance Futures (most liquid)
BINANCE_FUTURES_REST  = "https://fapi.binance.com"
OKX_SWAP_REST         = "https://www.okx.com"

FUNDING_EXTREME_BULL  = 0.0005      # +0.05% per 8h  → strong DOWN signal
FUNDING_MODERATE_BULL = 0.0002      # +0.02% per 8h  → mild DOWN signal
FUNDING_EXTREME_BEAR  = -0.0005     # -0.05% per 8h  → strong UP signal
FUNDING_MODERATE_BEAR = -0.0002     # -0.02% per 8h  → mild UP signal
FUNDING_CACHE_TTL     = 60          # Re-fetch funding rate every 60s


# ─────────────────────────────────────────────
# NEW: Liquidation Cascade Detection
# ─────────────────────────────────────────────
# Large liquidations attract more liq → momentum continuation signal
# Fetch from Binance liquidation stream or REST
LIQUIDATION_WINDOW_SEC   = 120      # Look at last 2 minutes
LIQUIDATION_LARGE_USD    = 500_000  # $500k+ = "large" liquidation
LIQUIDATION_EXTREME_USD  = 2_000_000  # $2M+ = "extreme" cascade
LIQUIDATION_CACHE_TTL    = 30


# ─────────────────────────────────────────────
# Strategy weights (11 indicators)
# ─────────────────────────────────────────────
@dataclass
class IndicatorWeights:
    # Tier 1: Direct market answer (KING)
    window_delta_strong:  float = 8.0   # |delta| > 0.10%
    window_delta_medium:  float = 5.5   # |delta| > 0.02%
    window_delta_weak:    float = 3.0   # |delta| > 0.005%
    window_delta_noise:   float = 1.0   # |delta| > 0.001%

    # Tier 2: High-edge confirmed signals
    funding_extreme:      float = 3.0   # Extreme funding rate (contrarian)
    funding_moderate:     float = 1.5   # Moderate funding rate
    liquidation_cascade:  float = 2.5   # Large liquidations (continuation)
    tick_trend:           float = 2.5   # Real-time trade flow direction

    # Tier 3: Technical confirming signals
    micro_momentum:       float = 2.0   # Last 2 candles direction
    acceleration:         float = 1.5   # Momentum building/fading
    cross_window:         float = 1.5   # Adjacent window streak (max ×3)
    mtf_alignment:        float = 2.0   # 5m+15m trend alignment

    # Tier 4: Supporting context
    ema_crossover:        float = 1.0
    rsi_extreme:          float = 2.0   # RSI <25 or >75
    rsi_moderate:         float = 1.0   # RSI <35 or >65
    volume_surge:         float = 1.0
    vwap_deviation:       float = 1.5
    ob_imbalance:         float = 2.5   # Polymarket token OB depth

WEIGHTS = IndicatorWeights()
CONFIDENCE_DIVISOR    = 9.0         # max expected |score| for normalization


# ─────────────────────────────────────────────
# Kelly Criterion
# ─────────────────────────────────────────────
# f* = (p*b - q) / b   where b = (1-token_price)/token_price
# We bet KELLY_FRACTION * f* to reduce variance (fractional Kelly)
KELLY_FRACTION        = 0.40        # 40% fractional Kelly
KELLY_MIN_EDGE        = 0.02        # Skip if Kelly implies < 2% bet
KELLY_MAX_PCT         = 0.50        # Hard cap: never bet > 50% bankroll
KELLY_MIN_PCT         = 0.04        # Minimum bet fraction if trading

# Empirically calibrated: raw confidence → true win rate (from backtests)
# Lower calibration = more conservative = fewer bust scenarios
CONFIDENCE_TO_WINRATE = {
    0.80: 0.72,
    0.70: 0.66,
    0.60: 0.61,
    0.55: 0.57,
    0.50: 0.54,
    0.45: 0.52,
    0.40: 0.50,
    0.35: 0.49,
    0.30: 0.48,
}

# Streak-based Kelly multiplier: hot streaks = press harder
STREAK_KELLY_MULT = {
    0: 1.00,   # no streak
    1: 1.05,   # 1 win → +5%
    2: 1.10,   # 2 wins → +10%
    3: 1.20,   # 3 wins → +20%
    4: 1.30,   # 4 wins → +30%
    5: 1.40,   # 5+ wins → +40%
}
STREAK_LOSS_MULT = {
    0: 1.00,
    1: 0.85,   # 1 loss → -15%
    2: 0.70,   # 2 losses → -30%
    3: 0.55,   # 3 losses → -45%
    4: 0.40,   # 4 losses → -60%
}


# ─────────────────────────────────────────────
# Volatility regime (ATR-based)
# ─────────────────────────────────────────────
ATR_PERIOD            = 10
ATR_MIN_THRESHOLD     = 15.0        # Too quiet — skip non-delta signals
ATR_IDEAL_LOW         = 35.0
ATR_IDEAL_HIGH        = 350.0       # Sweet spot: active but not chaotic
ATR_MAX_THRESHOLD     = 800.0       # Too extreme — halve bet size
REGIME_MULTIPLIERS    = {
    "ideal":    1.00,
    "moderate": 0.75,
    "quiet":    0.40,
    "extreme":  0.55,
}


# ─────────────────────────────────────────────
# Adaptive confidence threshold
# ─────────────────────────────────────────────
ADAPTIVE_CONF_BASE    = 0.32
ADAPTIVE_CONF_MIN     = 0.20
ADAPTIVE_CONF_MAX     = 0.58
ADAPTIVE_WINDOW_TRADES = 12         # Look at last 12 trades
ADAPTIVE_LOSS_STREAK_ADJ = {
    0: 0.00, 1: 0.02, 2: 0.05,
    3: 0.08, 4: 0.12, 5: 0.15,
}


# ─────────────────────────────────────────────
# Trading modes
# ─────────────────────────────────────────────
@dataclass
class ModeConfig:
    name:              str
    bet_fraction:      float     # Base fraction (overridden by Kelly if enabled)
    min_confidence:    float
    min_token_price:   float
    max_token_price:   float
    kelly_enabled:     bool
    kelly_multiplier:  float
    description:       str

MODES = {
    "safe": ModeConfig(
        name="safe",
        bet_fraction=0.25,
        min_confidence=0.32,
        min_token_price=0.50,
        max_token_price=0.94,
        kelly_enabled=True,
        kelly_multiplier=0.80,
        description="Kelly 80% × fractional. Survives 5-loss streaks."
    ),
    "aggressive": ModeConfig(
        name="aggressive",
        bet_fraction=0.40,
        min_confidence=0.25,
        min_token_price=0.50,
        max_token_price=0.95,
        kelly_enabled=True,
        kelly_multiplier=1.00,
        description="Full Kelly. Compound profits aggressively."
    ),
    "degen": ModeConfig(
        name="degen",
        bet_fraction=1.00,
        min_confidence=0.10,
        min_token_price=0.50,
        max_token_price=0.97,
        kelly_enabled=False,
        kelly_multiplier=1.50,
        description="All-in. Kelly ignored. Volatility maximized."
    ),
    "oracle_lag": ModeConfig(
        name="oracle_lag",
        bet_fraction=0.40,
        min_confidence=0.50,
        min_token_price=0.50,
        max_token_price=0.85,
        kelly_enabled=True,
        kelly_multiplier=1.20,
        description="Oracle-lag arb. 120% Kelly when signal fires."
    ),
    "ultra": ModeConfig(
        name="ultra",
        bet_fraction=0.35,
        min_confidence=0.28,
        min_token_price=0.50,
        max_token_price=0.96,
        kelly_enabled=True,
        kelly_multiplier=1.10,
        description="All signals active. Streak-adjusted Kelly. Max alpha."
    ),
}


# ─────────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────────
STARTING_BANKROLL     = float(os.getenv("STARTING_BANKROLL", "10.0"))
MIN_BET               = float(os.getenv("MIN_BET", "1.0"))
MAX_DRAWDOWN_PCT      = 0.55        # Halt if bankroll drops > 55% from peak
MAX_CONSECUTIVE_LOSS  = 5           # Halt after 5 consecutive losses
COOLDOWN_AFTER_LOSS   = 0
DAILY_LOSS_LIMIT_PCT  = 0.28        # Daily loss limit: 28%
POLYMARKET_FEE        = 0.0
MIN_SHARES            = 5
LIMIT_BUY_PRICE       = 0.95

# Profit lock: protect gains once up significantly
PROFIT_LOCK_TRIGGER   = 1.40        # Lock when bankroll ≥ 140% of original
PROFIT_LOCK_RESERVE   = 0.35        # Keep 35% of profits locked
PROFIT_LOCK_BET_CAP   = 0.20        # Max 20% bet when lock active


# ─────────────────────────────────────────────
# Technical analysis
# ─────────────────────────────────────────────
EMA_FAST              = 9
EMA_SLOW              = 21
EMA_TREND             = 55
RSI_PERIOD            = 14
RSI_OVERBOUGHT        = 72
RSI_OVERSOLD          = 28
RSI_UPPER_MOD         = 63
RSI_LOWER_MOD         = 37
VOLUME_SURGE_RATIO    = 1.4
TICK_DIRECTION_MIN    = 0.62
TICK_MIN_MOVE         = 0.00004
OB_IMBALANCE_MIN      = 0.15        # Min bid/ask imbalance to signal

# Delta thresholds (fraction, not %)
DELTA_STRONG          = 0.0010      # 0.10%
DELTA_MEDIUM          = 0.0002      # 0.02%
DELTA_WEAK            = 0.00005     # 0.005%
DELTA_NOISE           = 0.00001     # 0.001%


# ─────────────────────────────────────────────
# Delta-based token pricing (8-band, granular)
# ─────────────────────────────────────────────
DELTA_PRICE_MAP = [
    (0.000000, 0.000050, 0.500, 0.500),
    (0.000050, 0.000200, 0.500, 0.555),
    (0.000200, 0.000500, 0.555, 0.640),
    (0.000500, 0.000800, 0.640, 0.740),
    (0.000800, 0.001000, 0.740, 0.820),
    (0.001000, 0.001500, 0.820, 0.900),
    (0.001500, 0.002000, 0.900, 0.940),
    (0.002000, 9999.000, 0.940, 0.970),
]


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_DIR               = "logs"
TRADE_LOG_FILE        = f"{LOG_DIR}/trades.jsonl"
STATS_FILE            = f"{LOG_DIR}/stats.json"
