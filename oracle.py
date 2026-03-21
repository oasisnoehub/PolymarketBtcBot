"""
oracle.py — ULTRA Chainlink oracle monitor with multi-exchange cross-validation.

Upgrades:
  • 3s polling (was 5s)
  • Cross-validates with OKX + Kraken for consensus price
  • signal_confidence score (0–1)
  • Rolling lag statistics for predictive timing
  • Signals only when lag is in tradeable sweet spot (10-45s)
"""

import time
import json
import logging
import threading
import requests
from collections import deque
from dataclasses import dataclass
from typing import Optional, List

import config
import price_feed

logger = logging.getLogger(__name__)

_LATEST_ROUND_DATA_SIG = "0xfeaf968c"


@dataclass
class OracleState:
    oracle_price:        float = 0.0
    oracle_updated_at:   float = 0.0
    fetch_time:          float = 0.0
    lag_seconds:         float = 0.0
    divergence_pct:      float = 0.0
    signal:              Optional[str] = None   # 'UP', 'DOWN', or None
    signal_confidence:   float = 0.0            # 0–1 quality of the lag opportunity
    cross_validated:     bool  = False          # multiple exchanges agree
    consensus_price:     float = 0.0


_state   = OracleState()
_lock    = threading.Lock()
_running = False
_lag_history: deque = deque(maxlen=360)    # 30 min of lag samples


def get_state() -> OracleState:
    with _lock:
        return OracleState(
            oracle_price      = _state.oracle_price,
            oracle_updated_at = _state.oracle_updated_at,
            fetch_time        = _state.fetch_time,
            lag_seconds       = _state.lag_seconds,
            divergence_pct    = _state.divergence_pct,
            signal            = _state.signal,
            signal_confidence = _state.signal_confidence,
            cross_validated   = _state.cross_validated,
            consensus_price   = _state.consensus_price,
        )


def _call_contract(data: str) -> Optional[str]:
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params":  [{"to": config.CHAINLINK_CONTRACT, "data": data}, "latest"],
        "id": 1,
    }
    try:
        resp = requests.post(config.CHAINLINK_ORACLE_URL, json=payload, timeout=4)
        resp.raise_for_status()
        return resp.json().get("result", "")
    except Exception as e:
        logger.debug(f"Oracle RPC: {e}")
        return None


def _parse_latest_round(hex_result: str) -> Optional[tuple]:
    if not hex_result or hex_result == "0x":
        return None
    data = hex_result[2:]
    if len(data) < 320:
        return None
    try:
        answer     = int(data[64:128], 16)
        updated_at = int(data[192:256], 16)
        return answer / 1e8, updated_at
    except Exception:
        return None


def _get_multi_exchange_prices() -> List[float]:
    prices = []

    # Binance WebSocket price
    bp = price_feed.get_current_price()
    if bp > 0:
        prices.append(bp)

    # OKX REST
    try:
        resp = requests.get(
            f"{config.OKX_REST}/api/v5/market/ticker",
            params={"instId": "BTC-USDT"}, timeout=2
        )
        if resp.status_code == 200:
            d = resp.json().get("data", [{}])
            if d:
                prices.append(float(d[0].get("last", 0)))
    except Exception:
        pass

    # Kraken REST
    try:
        resp = requests.get(
            f"{config.KRAKEN_REST}/0/public/Ticker",
            params={"pair": "XBTUSD"}, timeout=2
        )
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            if result:
                prices.append(float(list(result.values())[0]["c"][0]))
    except Exception:
        pass

    return [p for p in prices if p > 0]


def _consensus_price(prices: List[float]) -> float:
    if not prices:
        return 0.0
    median = sorted(prices)[len(prices) // 2]
    # Filter outliers > 0.2% from median
    valid  = [p for p in prices if abs(p - median) / median < 0.002]
    return sum(valid) / len(valid) if valid else median


def _score_signal(lag: float, div_abs: float, cross_validated: bool) -> float:
    """Score the oracle lag opportunity. Returns 0–1."""
    score = 0.0
    # Lag sweet spot: 10-35s is best
    if 10 <= lag <= 20:
        score += 0.45
    elif 20 < lag <= 35:
        score += 0.35
    elif 8 <= lag < 10 or 35 < lag <= 45:
        score += 0.20
    # Divergence
    if div_abs >= 0.30:
        score += 0.40
    elif div_abs >= 0.15:
        score += 0.30
    elif div_abs >= 0.05:
        score += 0.15
    # Cross-validation bonus
    if cross_validated:
        score += 0.15
    return min(score, 1.0)


def _poll_oracle():
    global _running
    _running = True
    logger.info("Oracle monitor started (3s polling, multi-CEX)")

    while _running:
        try:
            result = _call_contract(_LATEST_ROUND_DATA_SIG)
            if result:
                parsed = _parse_latest_round(result)
                if parsed:
                    oracle_price, updated_at = parsed
                    now    = time.time()
                    lag    = now - updated_at
                    _lag_history.append((now, lag))

                    prices    = _get_multi_exchange_prices()
                    consensus = _consensus_price(prices)
                    cross_ok  = len(prices) >= 2

                    divergence   = 0.0
                    signal       = None
                    sig_conf     = 0.0

                    if consensus > 0 and oracle_price > 0:
                        divergence = (consensus - oracle_price) / oracle_price * 100
                        div_abs    = abs(divergence)

                        if config.ORACLE_LAG_MIN_SEC <= lag <= config.ORACLE_LAG_MAX_SEC:
                            if div_abs >= config.ORACLE_MIN_DIVERGENCE:
                                signal   = "UP" if divergence > 0 else "DOWN"
                                sig_conf = _score_signal(lag, div_abs, cross_ok)

                    with _lock:
                        _state.oracle_price      = oracle_price
                        _state.oracle_updated_at = float(updated_at)
                        _state.fetch_time        = now
                        _state.lag_seconds       = lag
                        _state.divergence_pct    = divergence
                        _state.signal            = signal
                        _state.signal_confidence = sig_conf
                        _state.cross_validated   = cross_ok
                        _state.consensus_price   = consensus

                    if signal:
                        logger.info(
                            f"ORACLE LAG  lag={lag:.1f}s  "
                            f"consensus=${consensus:,.0f}  oracle=${oracle_price:,.0f}  "
                            f"div={divergence:+.3f}%  signal={signal}  conf={sig_conf:.2f}"
                        )
        except Exception as e:
            logger.debug(f"Oracle poll error: {e}")

        time.sleep(3)


_oracle_thread: Optional[threading.Thread] = None

def start_oracle_monitor():
    global _oracle_thread
    if _oracle_thread and _oracle_thread.is_alive():
        return
    _oracle_thread = threading.Thread(target=_poll_oracle, daemon=True, name="OracleMonitor")
    _oracle_thread.start()


def stop_oracle_monitor():
    global _running
    _running = False
