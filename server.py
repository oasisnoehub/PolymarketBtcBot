"""
server.py — ULTRA Flask + Socket.IO dashboard server.

All new signals streamed live:
  • Funding rate (color-coded)
  • Liquidation cascade USD
  • Kelly fraction ring
  • Volatility regime badge
  • Oracle lag confidence bar
  • Adaptive threshold
  • Win/loss streak indicators

Run:
  python server.py                      # http://localhost:5000
  python server.py --port 8080 --mode ultra --dry-run
"""

import eventlet
eventlet.monkey_patch()

import argparse
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO, emit

sys.path.insert(0, os.path.dirname(__file__))
import config
import market
import oracle
import price_feed
import risk
import strategy
from strategy import SignalResult

app = Flask(__name__)
app.config["SECRET_KEY"] = "ultra-poly-btc-bot"
sio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
log = logging.getLogger(__name__)

_state = {
    "running":           False,
    "mode":              "ultra",
    "dry_run":           True,
    "price":             0.0,
    "price_history":     deque(maxlen=300),
    "window_ts":         0,
    "window_open":       0.0,
    "eta":               0,
    "signal":            None,
    "last_trade":        None,
    "trades":            deque(maxlen=200),
    "bankroll_history":  deque(maxlen=500),
    "oracle":            {"lag": 0, "divergence": 0, "signal": None,
                          "signal_confidence": 0, "cross_validated": False},
    "stats":             {"trades": 0, "wins": 0, "losses": 0,
                          "win_rate": 0, "roi": 0, "bankroll": 0,
                          "streak_win": 0, "streak_loss": 0},
    "polymarket":        {"up_price": 0.0, "down_price": 0.0,
                          "liquidity": 0.0, "volume": 0.0,
                          "title": "-", "active": False, "closed": False,
                          "slug": "", "ts": 0},
}
_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# ─────────────────────────────────────────────────────────────────────
# Dashboard HTML (served inline)
# ─────────────────────────────────────────────────────────────────────
with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "r") as _f:
    DASHBOARD_HTML = _f.read()


@app.route("/")
def index():
    return DASHBOARD_HTML


@app.route("/api/state")
def api_state():
    rs = risk.get_state()
    return jsonify({
        "running":          _state["running"],
        "mode":             _state["mode"],
        "dry_run":          _state["dry_run"],
        "price":            _state["price"],
        "price_history":    list(_state["price_history"]),
        "window_ts":        _state["window_ts"],
        "window_open":      _state["window_open"],
        "eta":              _state["eta"],
        "signal":           _state["signal"],
        "last_trade":       _state["last_trade"],
        "trades":           list(_state["trades"]),
        "bankroll_history": list(_state["bankroll_history"]),
        "oracle":           _state["oracle"],
        "bankroll":         rs.bankroll,
        "stats":            _state["stats"],
        "polymarket":       dict(_state["polymarket"]),
    })


@app.route("/api/polymarket")
def api_polymarket():
    return jsonify(dict(_state["polymarket"]))


@app.route("/api/trades")
def api_trades():
    return jsonify(list(_state["trades"]))


# ─────────────────────────────────────────────────────────────────────
# Socket.IO
# ─────────────────────────────────────────────────────────────────────
@sio.on("connect")
def on_connect():
    rs = risk.get_state()
    emit("state_snapshot", {
        "running":          _state["running"],
        "mode":             _state["mode"],
        "dry_run":          _state["dry_run"],
        "price":            _state["price"],
        "price_history":    list(_state["price_history"]),
        "window_ts":        _state["window_ts"],
        "window_open":      _state["window_open"],
        "eta":              _state["eta"],
        "signal":           _state["signal"],
        "last_trade":       _state["last_trade"],
        "trades":           list(_state["trades"]),
        "bankroll_history": list(_state["bankroll_history"]),
        "oracle":           _state["oracle"],
        "bankroll":         rs.bankroll,
        "stats":            _state["stats"],
        "polymarket":       dict(_state["polymarket"]),
    })


@sio.on("start_bot")
def on_start_bot(data):
    global _bot_thread
    if _state["running"]:
        return
    _state["mode"]    = data.get("mode", "ultra")
    _state["dry_run"] = data.get("dry_run", True)
    _stop_event.clear()
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True, name="BotLoop")
    _bot_thread.start()


@sio.on("stop_bot")
def on_stop_bot(_=None):
    _stop_event.set()
    _state["running"] = False
    sio.emit("bot_status", {"running": False})


@sio.on("set_mode")
def on_set_mode(data):
    _state["mode"] = data.get("mode", "ultra")
    sio.emit("config_update", {"mode": _state["mode"]})


# ─────────────────────────────────────────────────────────────────────
# Price ticker (1s)
# ─────────────────────────────────────────────────────────────────────
def _price_ticker():
    import requests as _req
    _pm_tick   = 0
    _rest_tick = 0
    while True:
        price = price_feed.get_current_price()
        # When WebSocket is down, refresh price from REST every 5 s
        if not price_feed.is_connected():
            _rest_tick += 1
            if _rest_tick % 5 == 0:
                try:
                    fresh = price_feed.fetch_price_rest()
                    if fresh > 0:
                        price_feed._current_price = fresh
                        price = fresh
                except Exception:
                    pass
        if price > 0:
            ts   = time.time()
            _state["price"] = price
            _state["price_history"].append({"t": ts, "p": price})

            wts = market.current_window_ts()
            eta = int(market.seconds_until_close())
            _state["window_ts"] = wts
            _state["eta"]       = eta

            oracle_st = oracle.get_state()
            _state["oracle"] = {
                "lag":                round(oracle_st.lag_seconds, 1),
                "divergence":         round(oracle_st.divergence_pct, 3),
                "signal":             oracle_st.signal,
                "signal_confidence":  round(oracle_st.signal_confidence, 2),
                "cross_validated":    oracle_st.cross_validated,
            }

            rs = risk.get_state()
            _update_stats(rs)
            _state["bankroll_history"].append({"t": ts, "b": rs.bankroll})

            sio.emit("tick", {
                "price":      price,
                "t":          ts,
                "window_ts":  wts,
                "eta":        eta,
                "bankroll":   rs.bankroll,
                "oracle":     _state["oracle"],
                "stats":      _state["stats"],
            })

            # ── Polymarket poll every 5 s ───────────────────────────
            _pm_tick += 1
            if _pm_tick % 5 == 1:   # tick 1, 6, 11 … (first fire at tick 1)
                try:
                    import json as _json
                    slug = market.market_slug(wts)
                    resp = _req.get(
                        f"{config.GAMMA_API}/events",
                        params={"slug": slug}, timeout=4,
                    )
                    resp.raise_for_status()
                    data  = resp.json()
                    events = data if isinstance(data, list) else [data]
                    event  = events[0] if events else None
                    if event:
                        # outcomePrices / outcomes live inside markets[0], not the event
                        mkt = (event.get("markets") or [{}])[0]
                        # Both fields are JSON-encoded strings in the Gamma API
                        raw_prices   = mkt.get("outcomePrices", '["0.5","0.5"]')
                        raw_outcomes = mkt.get("outcomes",      '["Up","Down"]')
                        prices   = _json.loads(raw_prices)   if isinstance(raw_prices,   str) else raw_prices
                        outcomes = _json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                        up_idx   = 0 if "up" in outcomes[0].lower() else 1
                        dn_idx   = 1 - up_idx
                        # Use market-level liquidity/volume (strings); event-level is the total across all markets
                        liq = float(mkt.get("liquidity") or event.get("liquidity") or 0)
                        vol = float(mkt.get("volume")    or event.get("volume")    or 0)
                        pm = {
                            "up_price":   round(float(prices[up_idx]), 4),
                            "down_price": round(float(prices[dn_idx]), 4),
                            "liquidity":  round(liq, 2),
                            "volume":     round(vol, 2),
                            "title":      event.get("title", "-"),
                            "active":     bool(event.get("active", False)),
                            "closed":     bool(event.get("closed", False)),
                            "slug":       slug,
                            "ts":         ts,
                        }
                        _state["polymarket"].update(pm)
                        sio.emit("polymarket_tick", dict(_state["polymarket"]))
                        log.info(
                            f"Polymarket {pm['title']}: "
                            f"UP={pm['up_price']:.3f} DOWN={pm['down_price']:.3f} "
                            f"liq=${pm['liquidity']:,.0f}"
                        )
                except Exception as e:
                    log.debug(f"Polymarket fetch error: {e}", exc_info=True)

        eventlet.sleep(1)


def _update_stats(rs):
    total = rs.wins + rs.losses
    wr    = round(rs.wins / total * 100, 1) if total > 0 else 0
    roi   = round((rs.bankroll - rs.original_bankroll) / rs.original_bankroll * 100, 2)
    _state["stats"] = {
        "trades":     total,
        "wins":       rs.wins,
        "losses":     rs.losses,
        "win_rate":   wr,
        "roi":        roi,
        "bankroll":   round(rs.bankroll, 2),
        "streak_win":  rs.consecutive_win,
        "streak_loss": rs.consecutive_loss,
        "profit_locked": rs.profit_locked,
    }


# ─────────────────────────────────────────────────────────────────────
# Bot loop
# ─────────────────────────────────────────────────────────────────────
def _bot_loop():
    _state["running"] = True
    sio.emit("bot_status", {"running": True, "mode": _state["mode"]})
    log.info(f"Bot loop started mode={_state['mode']} dry_run={_state['dry_run']}")

    while not _stop_event.is_set():
        try:
            can, reason = risk.can_trade(_state["mode"])
            if not can:
                sio.emit("alert", {"type": "halt", "msg": reason})
                if _state["dry_run"]:
                    risk.resume()
                else:
                    break

            window_ts  = market.current_window_ts()
            close_time = window_ts + config.WINDOW_SECONDS
            wake_at    = close_time - config.SNIPE_OFFSET
            now        = time.time()
            sleep_for  = wake_at - now - 0.5

            if sleep_for > 0:
                end = time.time() + sleep_for
                while time.time() < end and not _stop_event.is_set():
                    eventlet.sleep(min(1.0, end - time.time()))

            if _stop_event.is_set():
                break

            _run_snipe_cycle(window_ts)
            eventlet.sleep(2)

        except Exception as e:
            log.error(f"Bot loop error: {e}", exc_info=True)
            sio.emit("alert", {"type": "error", "msg": str(e)})
            eventlet.sleep(5)

    _state["running"] = False
    sio.emit("bot_status", {"running": False})


def _run_snipe_cycle(window_ts: int):
    close_time  = window_ts + config.WINDOW_SECONDS
    mode_name   = _state["mode"]
    mode        = config.MODES[mode_name]
    dry_run     = _state["dry_run"]

    try:
        window_open = price_feed.fetch_window_open_price(window_ts)
    except Exception:
        candles     = price_feed.fetch_klines(limit=5)
        window_open = candles[0]["open"] if candles else 0
    if window_open <= 0:
        return

    _state["window_open"] = window_open
    sio.emit("window_open", {"window_ts": window_ts, "open_price": window_open})

    best_sig:  Optional[SignalResult] = None
    prev_score = 0.0
    deadline   = close_time - config.HARD_DEADLINE
    rs         = risk.get_state()
    threshold  = strategy.get_adaptive_threshold(list(rs.recent_trades))

    while time.time() < deadline and not _stop_event.is_set():
        try:
            candles       = price_feed.fetch_klines(limit=60)
            current_price = price_feed.get_current_price() or candles[-1]["close"]
            ticks         = price_feed.get_tick_history()
        except Exception:
            eventlet.sleep(config.POLL_INTERVAL)
            continue

        sig = strategy.analyze(
            candles           = candles,
            window_open_price = window_open,
            current_price     = current_price,
            tick_history      = ticks,
            mode_name         = mode_name,
            win_streak        = rs.consecutive_win,
            loss_streak       = rs.consecutive_loss,
        )
        if sig is None:
            eventlet.sleep(config.POLL_INTERVAL)
            continue

        _state["signal"] = _sig_to_dict(sig, window_ts, current_price, window_open)
        sio.emit("signal_update", _state["signal"])

        if best_sig and abs(sig.score - prev_score) >= 1.8:
            sio.emit("alert", {"type": "spike", "msg": f"Score spike → firing!"})
            best_sig = sig
            break

        if sig.confidence >= threshold:
            best_sig = sig
            break

        best_sig   = sig if (best_sig is None or abs(sig.score) > abs(best_sig.score)) else best_sig
        prev_score = sig.score
        eventlet.sleep(config.POLL_INTERVAL)

    if best_sig is None:
        return

    direction   = best_sig.direction
    delta_abs   = abs(best_sig.window_delta_pct / 100)
    token_price = strategy.estimate_token_price(delta_abs)
    bet_usd     = risk.bet_size(mode_name, best_sig.kelly_fraction, best_sig.regime)
    shares      = bet_usd / token_price if token_price > 0 else 0

    trade_event = {
        "id":           f"{window_ts}",
        "ts":           time.time(),
        "datetime":     datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "window_ts":    window_ts,
        "direction":    direction,
        "token_price":  round(token_price, 4),
        "shares":       round(shares, 2),
        "bet_usd":      round(bet_usd, 2),
        "confidence":   round(best_sig.confidence, 3),
        "score":        round(best_sig.score, 3),
        "kelly":        round(best_sig.kelly_fraction, 4),
        "delta_pct":    round(best_sig.window_delta_pct, 4),
        "regime":       best_sig.regime,
        "funding_rate": round(best_sig.funding_rate * 100, 4),
        "liq_usd":      round(best_sig.liq_usd, 0),
        "atr":          round(best_sig.atr, 2),
        "reasons":      best_sig.reasons,
        "mode":         mode_name,
        "dry_run":      dry_run,
        "status":       "open",
        "result":       None,
        "profit":       None,
        "actual_dir":   None,
    }
    sio.emit("trade_open", trade_event)
    log.info(
        f"{'[DRY]' if dry_run else '[LIVE]'} {direction} "
        f"conf={best_sig.confidence:.0%} kelly={best_sig.kelly_fraction:.1%} "
        f"regime={best_sig.regime} fund={best_sig.funding_rate*100:+.3f}% "
        f"token=${token_price:.3f} bet=${bet_usd:.2f}"
    )

    wait = max(close_time - time.time() + 2, 0)
    if wait > 0:
        eventlet.sleep(wait)

    actual = price_feed.fetch_window_result(window_ts)
    if actual is None:
        actual = "UP"

    won = direction == actual
    if won:
        profit = shares * (1.0 - token_price)
        risk.record_win(profit, mode_name)
    else:
        profit = -bet_usd
        risk.record_loss(bet_usd, mode_name)

    strategy.record_window_result(window_ts, direction, actual,
                                  best_sig.confidence, best_sig.window_delta_pct)

    rs = risk.get_state()
    _update_stats(rs)
    _state["bankroll_history"].append({"t": time.time(), "b": rs.bankroll})

    trade_event.update({
        "status":     "closed",
        "result":     "WIN" if won else "LOSS",
        "actual_dir": actual,
        "profit":     round(profit, 4),
        "bankroll":   round(rs.bankroll, 2),
    })
    _state["trades"].appendleft(trade_event)
    _state["last_trade"] = trade_event
    sio.emit("trade_closed", trade_event)
    sio.emit("stats_update", _state["stats"])
    log.info(f"{'WIN' if won else 'LOSS'} actual={actual} profit={profit:+.2f} bankroll=${rs.bankroll:.2f}")


def _sig_to_dict(sig: SignalResult, window_ts: int, price: float, window_open: float) -> dict:
    return {
        "direction":    sig.direction,
        "score":        round(sig.score, 3),
        "confidence":   round(sig.confidence, 3),
        "delta_pct":    round(sig.window_delta_pct, 4),
        "ema_fast":     round(sig.ema_fast, 2),
        "ema_slow":     round(sig.ema_slow, 2),
        "rsi":          round(sig.rsi, 1),
        "kelly":        round(sig.kelly_fraction, 4),
        "regime":       sig.regime,
        "atr":          round(sig.atr, 2),
        "vwap":         round(sig.vwap, 2),
        "ob_imbalance": round(sig.ob_imbalance, 3),
        "funding_rate": round(sig.funding_rate * 100, 4),
        "liq_usd":      round(sig.liq_usd, 0),
        "signal_quality": sig.signal_quality,
        "reasons":      sig.reasons,
        "price":        round(price, 2),
        "window_open":  round(window_open, 2),
        "window_ts":    window_ts,
        "ts":           time.time(),
    }


# ─────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────
def start_background_services():
    price_feed.start_feed()
    oracle.start_oracle_monitor()
    eventlet.spawn(_price_ticker)
    log.info("Background services started")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket ULTRA Bot Dashboard")
    parser.add_argument("--port",        type=int, default=5001)
    parser.add_argument("--host",        type=str, default="0.0.0.0")
    parser.add_argument("--mode",        type=str, default="ultra",
                        choices=list(config.MODES.keys()))
    parser.add_argument("--dry-run",     action="store_true", default=True)
    parser.add_argument("--no-dry-run",  dest="dry_run", action="store_false")
    parser.add_argument("--verbose",     action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level   = logging.DEBUG if args.verbose else logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(message)s",
        datefmt = "%H:%M:%S",
    )
    for lib in ("engineio", "socketio", "websocket", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    _state["mode"]    = args.mode
    _state["dry_run"] = args.dry_run

    os.makedirs("templates", exist_ok=True)
    os.makedirs(config.LOG_DIR, exist_ok=True)

    start_background_services()

    print(f"\n  🚀  Polymarket ULTRA BTC Bot")
    print(f"  📊  Dashboard: http://127.0.0.1:{args.port}")
    print(f"  Mode: {args.mode}  Dry-run: {args.dry_run}\n")

    sio.run(app, host=args.host, port=args.port, debug=False)
