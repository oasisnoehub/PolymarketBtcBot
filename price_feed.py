"""
price_feed.py — Real-time BTC price feed via Binance WebSocket + REST fallback.

Maintains:
  • current_price  — latest trade price (float)
  • tick_history   — deque of recent (timestamp, price) tuples
  • klines_cache   — recent 1-min candles for TA

The WebSocket listener runs in a background thread so the main bot thread
never blocks waiting for price data.
"""

import time
import json
import logging
import threading
import requests
import websocket
from collections import deque
from typing import Optional, List, Tuple

import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Shared state (thread-safe reads are fine for floats in CPython)
# ─────────────────────────────────────────────────────────────
_lock          = threading.Lock()
_current_price: float        = 0.0
_tick_history: deque         = deque(maxlen=300)   # ~10 min of ticks
_connected:    bool          = False
_ws_thread:    Optional[threading.Thread] = None
_ws_ever_connected: bool     = False  # tracks if current session ever connected


def get_current_price() -> float:
    return _current_price

def get_tick_history() -> List[Tuple[float, float]]:
    with _lock:
        return list(_tick_history)

def is_connected() -> bool:
    return _connected


# ─────────────────────────────────────────────────────────────
# Binance REST — fetch 1-min klines
# ─────────────────────────────────────────────────────────────
def fetch_klines(symbol: str = config.BINANCE_SYMBOL,
                 interval: str = "1m",
                 limit: int = 60) -> List[dict]:
    """
    Returns a list of candle dicts with keys:
      open_time, open, high, low, close, volume, close_time
    Tries OKX first, then Binance, then Coinbase.
    Raises on failure; caller should handle.
    """
    # Try OKX first (excellent global availability)
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {
            "instId": "BTC-USDT",
            "bar": "1m",
            "limit": str(limit)
        }
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("code") == "0" and data.get("data"):
            candles = []
            for r in reversed(data["data"]):  # OKX returns newest first
                candles.append({
                    "open_time":  float(r[0]) / 1000,
                    "open":       float(r[1]),
                    "high":       float(r[2]),
                    "low":        float(r[3]),
                    "close":      float(r[4]),
                    "volume":     float(r[5]),
                    "close_time": float(r[0]) / 1000 + 60,
                })
            return candles
    except Exception as e:
        logger.debug(f"OKX klines failed: {e}")
    
    # Try Binance
    try:
        url = f"{config.BINANCE_REST}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        raw = resp.json()
        candles = []
        for r in raw:
            candles.append({
                "open_time":  r[0] / 1000,
                "open":       float(r[1]),
                "high":       float(r[2]),
                "low":        float(r[3]),
                "close":      float(r[4]),
                "volume":     float(r[5]),
                "close_time": r[6] / 1000,
            })
        return candles
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 451:
            logger.debug("Binance blocked (451) - trying other exchanges")
        else:
            logger.debug(f"Binance klines failed: {e}")
    except Exception as e:
        logger.debug(f"Binance klines error: {e}")
    
    # Fallback: Coinbase (returns fewer candles, max 300)
    try:
        # Coinbase uses granularity in seconds (60 = 1 minute)
        granularity = 60
        end_time = int(time.time())
        start_time = end_time - (limit * granularity)
        
        url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
        params = {
            "start": start_time,
            "end": end_time,
            "granularity": granularity
        }
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        raw = resp.json()
        
        candles = []
        for r in reversed(raw):  # Coinbase returns newest first
            candles.append({
                "open_time":  r[0],
                "open":       float(r[3]),
                "high":       float(r[2]),
                "low":        float(r[1]),
                "close":      float(r[4]),
                "volume":     float(r[5]),
                "close_time": r[0] + granularity,
            })
        return candles[-limit:] if len(candles) > limit else candles
    except Exception as e:
        logger.warning(f"Coinbase klines failed: {e}")
        raise Exception("All klines APIs failed")


def fetch_price_rest() -> float:
    """
    Fetch current BTC price from multiple exchanges with fallback.
    Tries OKX first (best global availability), then others.
    """
    # Try OKX first (excellent global availability)
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        resp = requests.get(url, params={"instId": "BTC-USDT"}, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0" and data.get("data"):
            return float(data["data"][0]["last"])
    except Exception as e:
        logger.debug(f"OKX REST failed: {e}")
    
    # Try Binance
    try:
        url = f"{config.BINANCE_REST}/api/v3/ticker/price"
        resp = requests.get(url, params={"symbol": config.BINANCE_SYMBOL}, timeout=3)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 451:
            logger.debug("Binance blocked (451) - trying other exchanges")
        else:
            logger.debug(f"Binance REST failed: {e}")
    except Exception as e:
        logger.debug(f"Binance REST error: {e}")
    
    # Fallback: Coinbase
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        logger.debug(f"Coinbase REST failed: {e}")
    
    # Fallback: Kraken
    try:
        url = "https://api.kraken.com/0/public/Ticker"
        resp = requests.get(url, params={"pair": "XBTUSD"}, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result"):
            return float(data["result"]["XXBTZUSD"]["c"][0])
    except Exception as e:
        logger.debug(f"Kraken REST failed: {e}")
    
    # Fallback: CoinGecko (no API key needed)
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        resp = requests.get(url, params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=3)
        resp.raise_for_status()
        return float(resp.json()["bitcoin"]["usd"])
    except Exception as e:
        logger.debug(f"CoinGecko REST failed: {e}")
    
    raise Exception("All price feed REST APIs failed")


def fetch_window_open_price(window_ts: int) -> float:
    """
    Get the exact BTC open price at the start of the 5-min window.
    Tries OKX first, then Binance, then falls back to latest REST price.
    """
    # Try OKX
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": "BTC-USDT", "bar": "1m", "limit": "1",
                  "before": str(window_ts * 1000), "after": str((window_ts - 60) * 1000)}
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0" and data.get("data"):
            return float(data["data"][0][1])  # open price
    except Exception as e:
        logger.debug(f"OKX window_open failed: {e}")

    # Try Binance
    try:
        url    = f"{config.BINANCE_REST}/api/v3/klines"
        params = {"symbol": config.BINANCE_SYMBOL, "interval": "1m",
                  "startTime": window_ts * 1000, "limit": 1}
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0][1])
    except Exception as e:
        logger.debug(f"Binance window_open failed: {e}")

    # Fallback: use current price as approximation
    p = fetch_price_rest()
    if p > 0:
        return p
    return 0.0


def fetch_window_result(window_ts: int) -> Optional[str]:
    """
    After the window closes, check if BTC closed UP or DOWN.
    Returns 'UP', 'DOWN', or None on failure.
    """
    close_ts = window_ts + config.WINDOW_SECONDS

    # Try OKX
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": "BTC-USDT", "bar": "1m", "limit": "10",
                  "after": str(window_ts * 1000),
                  "before": str((close_ts + 60) * 1000)}
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0" and data.get("data"):
            candles = list(reversed(data["data"]))  # oldest first
            open_price  = float(candles[0][1])
            close_price = float(candles[-1][4])
            return "UP" if close_price >= open_price else "DOWN"
    except Exception as e:
        logger.debug(f"OKX window_result failed: {e}")

    # Try Binance
    try:
        url    = f"{config.BINANCE_REST}/api/v3/klines"
        params = {"symbol": config.BINANCE_SYMBOL, "interval": "1m",
                  "startTime": window_ts * 1000,
                  "endTime":   (close_ts + 60) * 1000, "limit": 10}
        resp  = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data  = resp.json()
        if data:
            open_price  = float(data[0][1])
            close_price = float(data[-1][4])
            return "UP" if close_price >= open_price else "DOWN"
    except Exception as e:
        logger.debug(f"Binance window_result failed: {e}")

    logger.warning(f"fetch_window_result: all sources failed for window {window_ts}")
    return None


# ─────────────────────────────────────────────────────────────
# Binance WebSocket — runs in background thread
# ─────────────────────────────────────────────────────────────
def _on_message(ws, message: str):
    global _current_price
    try:
        data  = json.loads(message)
        price = float(data["p"])
        ts    = data["T"] / 1000.0  # trade time in seconds
        _current_price = price
        with _lock:
            _tick_history.append((ts, price))
    except Exception as e:
        logger.debug(f"WS message parse error: {e}")


def _on_open(ws):
    global _connected, _ws_ever_connected
    _connected = True
    _ws_ever_connected = True
    logger.info("✅ Binance WebSocket connected")


def _on_close(ws, code, msg):
    global _connected
    _connected = False
    # Suppress close messages - they're expected during reconnection
    logger.debug(f"WebSocket closed (code: {code})")


def _on_error(ws, error):
    # Only log significant errors to reduce noise
    error_str = str(error)
    if "Connection reset" not in error_str and "Errno 54" not in error_str and "Errno 60" not in error_str:
        logger.warning(f"WebSocket error: {error}")
    else:
        logger.debug(f"WebSocket connection error: {error}")


def _get_proxy_kwargs() -> dict:
    """Extract proxy settings from HTTP_PROXY/HTTPS_PROXY env vars for websocket-client."""
    import os, urllib.parse
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    if not proxy_url:
        return {}
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        scheme = (parsed.scheme or "http").lower()
        # websocket-client proxy_type accepts: "http", "socks4", "socks5", "socks5h"
        proxy_type = scheme if scheme in ("http", "socks4", "socks5", "socks5h") else "http"
        return {
            "http_proxy_host": parsed.hostname,
            "http_proxy_port": parsed.port or 7890,
            "proxy_type":      proxy_type,
        }
    except Exception:
        return {}


def _run_ws():
    """Reconnect loop — keeps the WebSocket alive indefinitely with exponential backoff."""
    reconnect_delay = 2
    max_delay = 30
    consecutive_failures = 0
    proxy_kwargs = _get_proxy_kwargs()
    if proxy_kwargs:
        logger.info(f"WebSocket using proxy {proxy_kwargs['http_proxy_host']}:{proxy_kwargs['http_proxy_port']}")
    
    # Try multiple sources with fast failover
    # Start with Binance (most reliable), then fallback to others
    ws_sources = [
        {
            "name": "Binance",
            "url": "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
            "type": "binance"
        },
        {
            "name": "Coinbase",
            "url": "wss://ws-feed.exchange.coinbase.com",
            "type": "coinbase"
        },
        {
            "name": "Kraken",
            "url": "wss://ws.kraken.com",
            "type": "kraken"
        }
    ]
    
    current_source_idx = 0

    while True:
        try:
            source = ws_sources[current_source_idx]
            ws_url = source["url"]
            ws_type = source["type"]
            global _ws_ever_connected
            _ws_ever_connected = False  # reset for this connection attempt

            logger.info(f"Connecting to {source['name']} WebSocket...")
            
            if ws_type == "binance":
                # Binance WebSocket handlers
                def on_open_binance(ws):
                    global _connected, _ws_ever_connected
                    _connected = True
                    _ws_ever_connected = True
                    logger.info(f"✅ {source['name']} WebSocket connected")
                
                def on_message_binance(ws, message):
                    global _current_price
                    try:
                        data = json.loads(message)
                        price = float(data["p"])
                        ts = data["T"] / 1000.0  # trade time in seconds
                        _current_price = price
                        with _lock:
                            _tick_history.append((ts, price))
                    except Exception as e:
                        logger.debug(f"Binance WS message parse error: {e}")
                
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open_binance,
                    on_message=on_message_binance,
                    on_close=_on_close,
                    on_error=_on_error,
                )
                
            elif ws_type == "coinbase":
                # Coinbase WebSocket handlers
                def on_open_coinbase(ws):
                    global _connected, _ws_ever_connected
                    _connected = True
                    _ws_ever_connected = True
                    logger.info(f"✅ {source['name']} WebSocket connected")
                    subscribe_msg = {
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker"]
                    }
                    ws.send(json.dumps(subscribe_msg))
                
                def on_message_coinbase(ws, message):
                    global _current_price
                    try:
                        data = json.loads(message)
                        if data.get("type") == "ticker" and "price" in data:
                            price = float(data["price"])
                            ts = time.time()
                            _current_price = price
                            with _lock:
                                _tick_history.append((ts, price))
                    except Exception as e:
                        logger.debug(f"Coinbase WS message parse error: {e}")
                
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open_coinbase,
                    on_message=on_message_coinbase,
                    on_close=_on_close,
                    on_error=_on_error,
                )
                
            elif ws_type == "kraken":
                # Kraken WebSocket handlers
                def on_open_kraken(ws):
                    global _connected, _ws_ever_connected
                    _connected = True
                    _ws_ever_connected = True
                    logger.info(f"✅ {source['name']} WebSocket connected")
                    subscribe_msg = {
                        "event": "subscribe",
                        "pair": ["XBT/USD"],
                        "subscription": {"name": "trade"}
                    }
                    ws.send(json.dumps(subscribe_msg))
                
                def on_message_kraken(ws, message):
                    global _current_price
                    try:
                        data = json.loads(message)
                        # Kraken sends trade data as arrays
                        if isinstance(data, list) and len(data) >= 2:
                            trades = data[1]
                            if isinstance(trades, list) and len(trades) > 0:
                                # Get the last trade
                                trade = trades[-1]
                                if isinstance(trade, list) and len(trade) > 0:
                                    price = float(trade[0])
                                    ts = time.time()
                                    _current_price = price
                                    with _lock:
                                        _tick_history.append((ts, price))
                    except Exception as e:
                        logger.debug(f"Kraken WS message parse error: {e}")
                
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open_kraken,
                    on_message=on_message_kraken,
                    on_close=_on_close,
                    on_error=_on_error,
                )
            
            # Run with shorter timeouts for faster failover
            ws.run_forever(
                ping_interval=15,
                ping_timeout=5,
                **proxy_kwargs
            )
            # run_forever returned — count as failure if never connected (e.g. 451)
            if not _ws_ever_connected:
                consecutive_failures += 1
            else:
                # Clean disconnect — stay on same source, reset backoff
                consecutive_failures = 0
                reconnect_delay = 2

        except KeyboardInterrupt:
            logger.info("WebSocket thread interrupted")
            break
        except Exception as e:
            consecutive_failures += 1
            error_str = str(e)
            if any(err in error_str for err in ["Connection reset", "Errno 54", "Errno 60", "timed out"]):
                logger.debug(f"{source['name']} connection error (attempt {consecutive_failures})")
            else:
                logger.warning(f"{source['name']} WS exception: {e}")

        # ── Shared failover + backoff (runs after both normal and exception paths) ──
        if consecutive_failures >= 2:
            logger.info(f"{source['name']} failed {consecutive_failures}x — switching to next source")
            current_source_idx = (current_source_idx + 1) % len(ws_sources)
            consecutive_failures = 0
            reconnect_delay = 2
        elif consecutive_failures > 0:
            reconnect_delay = min(reconnect_delay * 1.3, max_delay)

        time.sleep(reconnect_delay)


def start_feed():
    """Start the WebSocket feed in a daemon thread. Call once at startup."""
    global _ws_thread
    if _ws_thread and _ws_thread.is_alive():
        return
    # Seed with REST price immediately so the bot has something to work with
    global _current_price
    try:
        _current_price = fetch_price_rest()
        logger.info(f"🌐 Seeded price from REST: ${_current_price:,.2f}")
    except Exception as e:
        logger.warning(f"REST seed failed: {e}")

    _ws_thread = threading.Thread(target=_run_ws, daemon=True, name="BinanceWS")
    _ws_thread.start()
    # Wait up to 5 s for the WebSocket to connect
    deadline = time.time() + 5
    while time.time() < deadline and not _connected:
        time.sleep(0.1)
