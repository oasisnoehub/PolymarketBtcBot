"""
strategy.py — ULTRA signal engine. 11 indicators + Kelly sizing.

NEW vs baseline:
  ★ Perp Funding Rate    — Contrarian fade on crowded longs/shorts
  ★ Liquidation Cascade  — Large liq → momentum continuation
  ★ MTF Alignment        — 1m/5m/15m proxy via EMA9/21/55
  ★ Cross-Window Streak  — Adjacent window momentum feedback
  ★ Online Kelly         — Per-trade optimal bet fraction
  ★ Regime Gating        — ATR-based: skip untradeable conditions

WINDOW DELTA IS STILL KING. Weight 8×. Every other indicator confirms or vetoes.
"""

import logging
import math
import time
import requests
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Deque

import config

logger = logging.getLogger(__name__)

# ── Module-level state (persists across cycles) ───────────────────────────────
_window_history: Deque[dict] = deque(maxlen=12)      # cross-window momentum
_funding_cache:  dict = {"rate": 0.0, "ts": 0.0}    # funding rate cache
_liq_cache:      dict = {"long_usd": 0.0, "short_usd": 0.0, "ts": 0.0}


@dataclass
class SignalResult:
    direction:         str
    score:             float
    confidence:        float
    reasons:           List[str]        = field(default_factory=list)
    window_delta_pct:  float            = 0.0
    ema_fast:          float            = 0.0
    ema_slow:          float            = 0.0
    rsi:               float            = 0.0
    kelly_fraction:    float            = 0.0
    regime:            str              = "unknown"
    atr:               float            = 0.0
    vwap:              float            = 0.0
    ob_imbalance:      float            = 0.0
    funding_rate:      float            = 0.0
    liq_usd:           float            = 0.0
    signal_quality:    str              = "normal"


# ─────────────────────────────────────────────────────────────────────
# Technical helpers
# ─────────────────────────────────────────────────────────────────────
def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k   = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def _atr(candles: List[dict], period: int = 10) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-period:]) / period


def _vwap(candles: List[dict]) -> float:
    total_vol = total_pv = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        total_pv  += typical * c["volume"]
        total_vol += c["volume"]
    return total_pv / total_vol if total_vol > 0 else 0.0


def _detect_regime(atr_val: float) -> Tuple[str, float]:
    """Returns (regime_name, multiplier)."""
    if atr_val <= 0:
        return "unknown", 1.0
    if atr_val < config.ATR_MIN_THRESHOLD:
        return "quiet", config.REGIME_MULTIPLIERS["quiet"]
    elif atr_val > config.ATR_MAX_THRESHOLD:
        return "extreme", config.REGIME_MULTIPLIERS["extreme"]
    elif config.ATR_IDEAL_LOW <= atr_val <= config.ATR_IDEAL_HIGH:
        return "ideal", config.REGIME_MULTIPLIERS["ideal"]
    else:
        return "moderate", config.REGIME_MULTIPLIERS["moderate"]


# ─────────────────────────────────────────────────────────────────────
# ★ NEW: Perpetual Funding Rate Signal
# ─────────────────────────────────────────────────────────────────────
def _fetch_funding_rate() -> float:
    """
    Fetch current BTC perpetual funding rate.
    Returns annualized rate as fraction (e.g., 0.0005 = +0.05% per 8h).

    Positive = longs paying shorts = crowded long = DOWN bias (contrarian)
    Negative = shorts paying longs = crowded short = UP bias (contrarian)

    Tries Binance Futures first, then OKX swap.
    """
    now = time.time()
    if now - _funding_cache["ts"] < config.FUNDING_CACHE_TTL:
        return _funding_cache["rate"]

    # Try Binance Futures
    try:
        resp = requests.get(
            f"{config.BINANCE_FUTURES_REST}/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            rate = float(data.get("lastFundingRate", 0))
            _funding_cache.update({"rate": rate, "ts": now})
            return rate
    except Exception as e:
        logger.debug(f"Binance futures funding failed: {e}")

    # Fallback: OKX swap
    try:
        resp = requests.get(
            f"{config.OKX_REST}/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", [])
            if items:
                rate = float(items[0].get("fundingRate", 0))
                _funding_cache.update({"rate": rate, "ts": now})
                return rate
    except Exception as e:
        logger.debug(f"OKX swap funding failed: {e}")

    return _funding_cache.get("rate", 0.0)


def _funding_signal(rate: float) -> Tuple[float, str]:
    """
    Convert funding rate to (score_contribution, reason_str).
    Contrarian: extreme positive funding → bears will squeeze longs → DOWN.
    """
    w = config.WEIGHTS
    if rate >= config.FUNDING_EXTREME_BULL:
        # Extreme longs → fade → DOWN
        return -w.funding_extreme, f"Funding EXTREME BULL ({rate*100:.3f}%/8h) → fade DOWN ×{w.funding_extreme}"
    elif rate >= config.FUNDING_MODERATE_BULL:
        return -w.funding_moderate, f"Funding bull ({rate*100:.3f}%/8h) → lean DOWN ×{w.funding_moderate}"
    elif rate <= config.FUNDING_EXTREME_BEAR:
        # Extreme shorts → squeeze → UP
        return +w.funding_extreme, f"Funding EXTREME BEAR ({rate*100:.3f}%/8h) → squeeze UP ×{w.funding_extreme}"
    elif rate <= config.FUNDING_MODERATE_BEAR:
        return +w.funding_moderate, f"Funding bear ({rate*100:.3f}%/8h) → lean UP ×{w.funding_moderate}"
    return 0.0, ""


# ─────────────────────────────────────────────────────────────────────
# ★ NEW: Liquidation Cascade Detection
# ─────────────────────────────────────────────────────────────────────
def _fetch_liquidations() -> Tuple[float, float]:
    """
    Fetch recent forced liquidations from Binance Futures.
    Returns (long_liq_usd, short_liq_usd) in last LIQUIDATION_WINDOW_SEC seconds.

    Large LONG liquidations = price dropped hard = continuation DOWN
    Large SHORT liquidations = price spiked up = continuation UP
    """
    now = time.time()
    if now - _liq_cache["ts"] < config.LIQUIDATION_CACHE_TTL:
        return _liq_cache["long_usd"], _liq_cache["short_usd"]

    try:
        resp = requests.get(
            f"{config.BINANCE_FUTURES_REST}/fapi/v1/allForceOrders",
            params={"symbol": "BTCUSDT", "limit": 100},
            timeout=3,
        )
        if resp.status_code == 200:
            orders = resp.json()
            cutoff = (now - config.LIQUIDATION_WINDOW_SEC) * 1000
            long_usd = short_usd = 0.0
            for o in orders:
                if o.get("time", 0) < cutoff:
                    continue
                side = o.get("side", "")    # SELL = long got liquidated, BUY = short got liquidated
                qty  = float(o.get("origQty", 0))
                price = float(o.get("price", 0))
                usd   = qty * price
                if side == "SELL":   # long liquidation
                    long_usd += usd
                elif side == "BUY":  # short liquidation
                    short_usd += usd
            _liq_cache.update({"long_usd": long_usd, "short_usd": short_usd, "ts": now})
            return long_usd, short_usd
    except Exception as e:
        logger.debug(f"Liquidation fetch failed: {e}")

    return _liq_cache.get("long_usd", 0.0), _liq_cache.get("short_usd", 0.0)


def _liquidation_signal(long_liq: float, short_liq: float) -> Tuple[float, float, str]:
    """
    Convert liquidation data to (score, total_usd, reason).
    Large long liq = price falling = DOWN. Large short liq = UP.
    """
    w          = config.WEIGHTS
    total_liq  = long_liq + short_liq
    net_liq    = short_liq - long_liq    # positive = more short liq = UP pressure

    if total_liq < config.LIQUIDATION_LARGE_USD:
        return 0.0, total_liq, ""

    if abs(net_liq) >= config.LIQUIDATION_EXTREME_USD:
        sign   = 1 if net_liq > 0 else -1
        label  = "EXTREME"
        weight = w.liquidation_cascade * 1.4
    elif abs(net_liq) >= config.LIQUIDATION_LARGE_USD:
        sign   = 1 if net_liq > 0 else -1
        label  = "large"
        weight = w.liquidation_cascade
    else:
        return 0.0, total_liq, ""

    direction = "UP" if sign > 0 else "DOWN"
    reason    = (
        f"Liq cascade {label}: short=${short_liq/1e6:.1f}M long=${long_liq/1e6:.1f}M "
        f"→ {direction} ×{weight:.1f}"
    )
    return sign * weight, total_liq, reason


# ─────────────────────────────────────────────────────────────────────
# MTF Alignment (proxy via EMA55 slope)
# ─────────────────────────────────────────────────────────────────────
def _mtf_alignment(candles: List[dict]) -> Tuple[float, str]:
    closes = [c["close"] for c in candles]
    if len(closes) < 55:
        return 0.0, ""
    ef = _ema(closes, config.EMA_FAST)
    es = _ema(closes, config.EMA_SLOW)
    et = _ema(closes, config.EMA_TREND)
    if not ef or not es or not et:
        return 0.0, ""
    # Check stack alignment: EMA9 > EMA21 > EMA55 = bullish
    if ef[-1] > es[-1] > et[-1]:
        return config.WEIGHTS.mtf_alignment, f"MTF bullish stack EMA9>21>55 ×{config.WEIGHTS.mtf_alignment}"
    elif ef[-1] < es[-1] < et[-1]:
        return -config.WEIGHTS.mtf_alignment, f"MTF bearish stack EMA9<21<55 ×{config.WEIGHTS.mtf_alignment}"
    return 0.0, ""


# ─────────────────────────────────────────────────────────────────────
# Cross-window momentum
# ─────────────────────────────────────────────────────────────────────
def _cross_window_momentum() -> Tuple[float, str]:
    if len(_window_history) < 2:
        return 0.0, ""
    recent = list(_window_history)[-5:]
    up_wins   = sum(1 for w in recent if w.get("direction") == "UP"   and w.get("correct"))
    dn_wins   = sum(1 for w in recent if w.get("direction") == "DOWN" and w.get("correct"))
    streak    = 0
    streak_dir = None
    for w in reversed(recent):
        if w.get("correct"):
            if streak_dir is None:
                streak_dir = w.get("direction")
            if w.get("direction") == streak_dir:
                streak += 1
            else:
                break
        else:
            break

    if streak < 2 or streak_dir is None:
        return 0.0, ""

    weight = min(config.WEIGHTS.cross_window * streak, config.WEIGHTS.cross_window * 3)
    sign   = 1 if streak_dir == "UP" else -1
    return sign * weight, f"CrossWindow streak {streak}× {streak_dir} ×{weight:.1f}"


def record_window_result(window_ts: int, direction: str, actual: str,
                         confidence: float, delta_pct: float):
    """Called after each window settles to feed cross-window momentum."""
    _window_history.append({
        "ts":        window_ts,
        "direction": direction,
        "actual":    actual,
        "correct":   direction == actual,
        "confidence": confidence,
        "delta_pct": delta_pct,
    })


# ─────────────────────────────────────────────────────────────────────
# Order book imbalance (Polymarket token OB)
# ─────────────────────────────────────────────────────────────────────
def _fetch_ob_imbalance(up_token: str, down_token: str) -> Tuple[float, str]:
    try:
        def top_depth(token_id: str, n: int = 5) -> float:
            resp = requests.get(
                f"{config.CLOB_API}/book",
                params={"token_id": token_id},
                timeout=2,
            )
            if resp.status_code != 200:
                return 0.0
            book = resp.json()
            bids = book.get("bids", [])[:n]
            asks = book.get("asks", [])[:n]
            bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids)
            ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks)
            return bid_depth - ask_depth

        up_imb = top_depth(up_token)
        dn_imb = top_depth(down_token)
        # net imbalance: positive = UP token has more buyer interest
        net    = (up_imb - dn_imb) / (abs(up_imb) + abs(dn_imb) + 1e-9)
        label  = f"OB imbalance net={net:+.2f}"
        return net, label
    except Exception as e:
        logger.debug(f"OB imbalance fetch failed: {e}")
        return 0.0, ""


# ─────────────────────────────────────────────────────────────────────
# Kelly fraction calculator
# ─────────────────────────────────────────────────────────────────────
def _kelly_fraction(confidence: float, token_price: float, mode_name: str,
                    win_streak: int = 0, loss_streak: int = 0) -> float:
    """
    Compute optimal fractional Kelly bet.
    f* = (p*b - q) / b   clamped to [KELLY_MIN_PCT, KELLY_MAX_PCT]
    """
    mode = config.MODES.get(mode_name)
    if not mode or not mode.kelly_enabled:
        return mode.bet_fraction if mode else config.KELLY_MIN_PCT

    # Map confidence to calibrated win rate
    p = config.KELLY_MIN_PCT  # fallback
    for thresh in sorted(config.CONFIDENCE_TO_WINRATE.keys(), reverse=True):
        if confidence >= thresh:
            p = config.CONFIDENCE_TO_WINRATE[thresh]
            break
    q = 1 - p

    # Net odds: if token costs $t and pays $1, odds = (1-t)/t
    if token_price <= 0 or token_price >= 1.0:
        return config.KELLY_MIN_PCT
    b = (1.0 - token_price) / token_price

    # Kelly formula
    if b <= 0:
        return config.KELLY_MIN_PCT
    raw_kelly = (p * b - q) / b
    if raw_kelly <= config.KELLY_MIN_EDGE:
        return 0.0   # No edge — skip trade

    # Apply fractional Kelly + mode multiplier
    fraction = raw_kelly * config.KELLY_FRACTION * mode.kelly_multiplier

    # Streak adjustments
    if win_streak > 0:
        mult = config.STREAK_KELLY_MULT.get(min(win_streak, 5), 1.40)
        fraction *= mult
    elif loss_streak > 0:
        mult = config.STREAK_LOSS_MULT.get(min(loss_streak, 4), 0.40)
        fraction *= mult

    return max(config.KELLY_MIN_PCT, min(config.KELLY_MAX_PCT, fraction))


# ─────────────────────────────────────────────────────────────────────
# Adaptive confidence threshold
# ─────────────────────────────────────────────────────────────────────
def get_adaptive_threshold(recent_trades: List[dict]) -> float:
    if not recent_trades:
        return config.ADAPTIVE_CONF_BASE
    n       = min(len(recent_trades), config.ADAPTIVE_WINDOW_TRADES)
    recent  = recent_trades[-n:]
    results = [t.get("result") for t in recent]
    wins    = results.count("WIN")
    losses  = results.count("LOSS")
    total   = wins + losses
    if total == 0:
        return config.ADAPTIVE_CONF_BASE

    win_rate = wins / total

    streak = 0
    for r in reversed(results):
        if r == "LOSS":
            streak += 1
        else:
            break

    streak_adj = config.ADAPTIVE_LOSS_STREAK_ADJ.get(
        min(streak, 5), config.ADAPTIVE_LOSS_STREAK_ADJ[5]
    )
    wr_adj = (
        -0.05 if win_rate >= 0.65 else
        -0.02 if win_rate >= 0.55 else
        +0.06 if win_rate <= 0.38 else
        +0.03 if win_rate <= 0.46 else 0.0
    )
    threshold = config.ADAPTIVE_CONF_BASE + streak_adj + wr_adj
    return max(config.ADAPTIVE_CONF_MIN, min(config.ADAPTIVE_CONF_MAX, threshold))


# ─────────────────────────────────────────────────────────────────────
# MAIN ANALYSIS FUNCTION
# ─────────────────────────────────────────────────────────────────────
def analyze(
    candles:            List[dict],
    window_open_price:  float,
    current_price:      float,
    tick_history:       Optional[List] = None,
    up_token_id:        Optional[str]  = None,
    down_token_id:      Optional[str]  = None,
    mode_name:          str            = "ultra",
    win_streak:         int            = 0,
    loss_streak:        int            = 0,
) -> Optional[SignalResult]:
    """
    Run all 11 indicators and return a SignalResult with Kelly fraction.
    """
    if not candles or len(candles) < 5:
        return None
    if window_open_price <= 0 or current_price <= 0:
        return None

    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]
    score   = 0.0
    reasons = []
    w       = config.WEIGHTS

    # ── Regime detection ─────────────────────────────────────────────
    atr_val = _atr(candles, config.ATR_PERIOD)
    regime, regime_mult = _detect_regime(atr_val)

    # ── 1. WINDOW DELTA (KING — not attenuated by regime) ────────────
    delta     = (current_price - window_open_price) / window_open_price
    delta_pct = delta * 100
    delta_abs = abs(delta)

    if delta_abs >= config.DELTA_STRONG:
        wt, label = w.window_delta_strong, f"STRONG ({delta_pct:+.3f}%)"
    elif delta_abs >= config.DELTA_MEDIUM:
        wt, label = w.window_delta_medium, f"MEDIUM ({delta_pct:+.3f}%)"
    elif delta_abs >= config.DELTA_WEAK:
        wt, label = w.window_delta_weak, f"WEAK ({delta_pct:+.3f}%)"
    elif delta_abs >= config.DELTA_NOISE:
        wt, label = w.window_delta_noise, f"NOISE ({delta_pct:+.3f}%)"
    else:
        wt, label = 0.0, f"FLAT ({delta_pct:+.4f}%)"

    sign = 1 if delta >= 0 else -1
    delta_score = sign * wt
    score += delta_score
    if wt > 0:
        reasons.append(f"WindowDelta {label} → {'UP' if sign > 0 else 'DOWN'} ×{wt}")

    # ── 2. FUNDING RATE (NEW ★) ───────────────────────────────────────
    funding_rate = _fetch_funding_rate()
    fund_score, fund_reason = _funding_signal(funding_rate)
    if fund_score != 0:
        # Funding signal is contrarian to delta — amplify if they agree,
        # slightly dampen if they disagree (market might be forcing the squeeze)
        if math.copysign(1, fund_score) == math.copysign(1, delta_score) and wt > 0:
            fund_score *= 1.2  # agreeing = amplify
        score   += fund_score * regime_mult
        reasons.append(fund_reason)

    # ── 3. LIQUIDATION CASCADE (NEW ★) ───────────────────────────────
    long_liq, short_liq = _fetch_liquidations()
    liq_score, liq_usd, liq_reason = _liquidation_signal(long_liq, short_liq)
    if liq_score != 0:
        score   += liq_score * regime_mult
        reasons.append(liq_reason)

    # ── All remaining signals attenuated by regime ────────────────────
    # ── 4. MICRO MOMENTUM ────────────────────────────────────────────
    if len(closes) >= 3:
        m1 = closes[-1] - closes[-2]
        m2 = closes[-2] - closes[-3]
        if m1 != 0:
            ms = 1 if m1 > 0 else -1
            score   += ms * w.micro_momentum * regime_mult
            reasons.append(f"Momentum {'↑' if m1 > 0 else '↓'} ({m1:+.2f}) ×{w.micro_momentum}")
        # ── 5. ACCELERATION ──────────────────────────────────────────
        if m1 != 0 and m2 != 0 and math.copysign(1, m1) == math.copysign(1, m2):
            accel = 1 if abs(m1) > abs(m2) else -1
            score += math.copysign(accel * w.acceleration * regime_mult, m1)
            reasons.append(f"Momentum {'accel' if accel > 0 else 'decel'} ×{w.acceleration}")

    # ── 6. EMA CROSSOVER (9/21) ───────────────────────────────────────
    ef_series = _ema(closes, config.EMA_FAST)
    es_series = _ema(closes, config.EMA_SLOW)
    ef_val    = ef_series[-1] if ef_series else 0
    es_val    = es_series[-1] if es_series else 0
    if ef_val and es_val:
        ema_s = 1 if ef_val > es_val else -1
        score += ema_s * w.ema_crossover * regime_mult
        reasons.append(f"EMA9{'>' if ema_s > 0 else '<'}EMA21 ({ef_val:.0f}vs{es_val:.0f}) ×{w.ema_crossover}")

    # ── 7. RSI ────────────────────────────────────────────────────────
    rsi_val = _rsi(closes, config.RSI_PERIOD)
    if rsi_val >= config.RSI_OVERBOUGHT:
        score   -= w.rsi_extreme * regime_mult
        reasons.append(f"RSI overbought ({rsi_val:.1f}) → DOWN ×{w.rsi_extreme}")
    elif rsi_val <= config.RSI_OVERSOLD:
        score   += w.rsi_extreme * regime_mult
        reasons.append(f"RSI oversold ({rsi_val:.1f}) → UP ×{w.rsi_extreme}")
    elif rsi_val >= config.RSI_UPPER_MOD:
        score   -= w.rsi_moderate * regime_mult
        reasons.append(f"RSI elevated ({rsi_val:.1f}) → DOWN ×{w.rsi_moderate}")
    elif rsi_val <= config.RSI_LOWER_MOD:
        score   += w.rsi_moderate * regime_mult
        reasons.append(f"RSI depressed ({rsi_val:.1f}) → UP ×{w.rsi_moderate}")

    # ── 8. VOLUME SURGE ───────────────────────────────────────────────
    if len(volumes) >= 6:
        r_avg = sum(volumes[-3:]) / 3
        p_avg = sum(volumes[-6:-3]) / 3
        if p_avg > 0 and r_avg >= p_avg * config.VOLUME_SURGE_RATIO:
            cd     = 1 if closes[-1] >= closes[-2] else -1
            score += cd * w.volume_surge * regime_mult
            reasons.append(f"VolumeSurge ×{r_avg/p_avg:.1f} → {'UP' if cd > 0 else 'DOWN'}")

    # ── 9. TICK TREND ─────────────────────────────────────────────────
    if tick_history and len(tick_history) >= 10:
        cutoff = tick_history[-1][0] - 30
        recent = [(t, p) for t, p in tick_history if t >= cutoff]
        if len(recent) >= 5:
            up_t = sum(1 for i in range(1, len(recent)) if recent[i][1] > recent[i-1][1])
            dn_t = sum(1 for i in range(1, len(recent)) if recent[i][1] < recent[i-1][1])
            tot  = up_t + dn_t
            if tot > 0:
                up_r      = up_t / tot
                tick_move = abs(recent[-1][1] - recent[0][1]) / recent[0][1]
                if max(up_r, 1-up_r) >= config.TICK_DIRECTION_MIN and tick_move >= config.TICK_MIN_MOVE:
                    ts = 1 if up_r > 0.5 else -1
                    score += ts * w.tick_trend * regime_mult
                    reasons.append(f"TickFlow {'↑' if ts > 0 else '↓'} ({up_t}↑/{dn_t}↓) ×{w.tick_trend}")

    # ── 10. MTF ALIGNMENT ─────────────────────────────────────────────
    if len(closes) >= 55:
        mtf_s, mtf_r = _mtf_alignment(candles)
        if mtf_s != 0:
            score   += mtf_s * regime_mult
            reasons.append(mtf_r)

    # ── 11. CROSS-WINDOW MOMENTUM ─────────────────────────────────────
    cw_s, cw_r = _cross_window_momentum()
    if cw_s != 0:
        score   += cw_s * regime_mult
        reasons.append(cw_r)

    # ── 12. VWAP DEVIATION ────────────────────────────────────────────
    vwap = 0.0
    if len(candles) >= 10:
        vwap = _vwap(candles[-60:] if len(candles) >= 60 else candles)
        if vwap > 0:
            vdev = (current_price - vwap) / vwap
            if abs(vdev) >= 0.0002:
                vs = 1 if current_price > vwap else -1
                score += vs * w.vwap_deviation * regime_mult
                reasons.append(f"VWAP {'↑' if vs > 0 else '↓'} dev={vdev:+.3%} ×{w.vwap_deviation}")

    # ── 13. ORDER BOOK IMBALANCE ──────────────────────────────────────
    ob_imb = 0.0
    if up_token_id and down_token_id:
        ob_imb, ob_r = _fetch_ob_imbalance(up_token_id, down_token_id)
        if abs(ob_imb) >= config.OB_IMBALANCE_MIN:
            ob_s    = (1 if ob_imb > 0 else -1) * w.ob_imbalance
            score  += ob_s * regime_mult
            reasons.append(ob_r + f" → {'UP' if ob_imb > 0 else 'DOWN'} ×{w.ob_imbalance}")

    # ── Final confidence + direction ──────────────────────────────────
    direction   = "UP" if score >= 0 else "DOWN"
    confidence  = min(abs(score) / config.CONFIDENCE_DIVISOR, 1.0)

    # Signal quality boost/penalty
    tier1_aligned = (sign > 0 and score > 0) or (sign < 0 and score < 0)
    quality = "normal"
    if tier1_aligned and confidence >= 0.55 and len(reasons) >= 4:
        confidence = min(confidence * 1.08, 1.0)
        quality = "high"
    elif len(reasons) <= 2 or confidence < 0.22:
        quality = "low"

    # Kelly sizing
    token_price = estimate_token_price(delta_abs)
    kelly_f     = _kelly_fraction(confidence, token_price, mode_name, win_streak, loss_streak)

    return SignalResult(
        direction        = direction,
        score            = round(score, 3),
        confidence       = round(confidence, 4),
        reasons          = reasons,
        window_delta_pct = round(delta_pct, 5),
        ema_fast         = round(ef_val, 2),
        ema_slow         = round(es_val, 2),
        rsi              = round(rsi_val, 1),
        kelly_fraction   = round(kelly_f, 4),
        regime           = regime,
        atr              = round(atr_val, 2),
        vwap             = round(vwap, 2),
        ob_imbalance     = round(ob_imb, 3),
        funding_rate     = round(funding_rate, 6),
        liq_usd          = round(liq_usd, 0),
        signal_quality   = quality,
    )


# ─────────────────────────────────────────────────────────────────────
# Token price estimation
# ─────────────────────────────────────────────────────────────────────
def estimate_token_price(window_delta_abs: float) -> float:
    for dmin, dmax, pmin, pmax in config.DELTA_PRICE_MAP:
        if dmin <= window_delta_abs < dmax:
            if dmax >= 9998 or dmax == dmin:
                return pmin
            frac = (window_delta_abs - dmin) / (dmax - dmin)
            return pmin + frac * (pmax - pmin)
    return 0.97
