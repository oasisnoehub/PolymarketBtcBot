"""
bot.py — ULTRA main trading engine.

Strategies:
  snipe       — Enter T-50s to T-4s. Window delta + 11-indicator fusion.
  oracle_lag  — Enter when Chainlink lags with 8%+ signal confidence.
  combined    — Oracle opportunistic + snipe fallback (recommended).

Modes: safe | aggressive | degen | oracle_lag | ultra

Usage:
  python bot.py --dry-run --mode ultra          # recommended start
  python bot.py --dry-run --mode aggressive
  python bot.py --strategy combined --mode ultra --dry-run
  python bot.py --mode ultra                    # LIVE
"""

import argparse
import logging
import os
import sys
import time
from typing import Optional, Tuple

import config
import logger as lg
import market
import oracle
import price_feed
import risk
import strategy
from strategy import SignalResult

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Snipe loop
# ─────────────────────────────────────────────────────────────────────
def _run_snipe_loop(
    window_ts:    int,
    window_open:  float,
    mode_name:    str,
    dry_run:      bool,
) -> Optional[SignalResult]:
    """Poll T-50s to T-4s. Returns best signal or None."""
    mode        = config.MODES[mode_name]
    best:       Optional[SignalResult] = None
    prev_score  = 0.0
    close_time  = window_ts + config.WINDOW_SECONDS
    deadline    = close_time - config.HARD_DEADLINE

    rs         = risk.get_state()
    win_streak  = rs.consecutive_win
    loss_streak = rs.consecutive_loss
    recent      = list(rs.recent_trades)
    threshold   = strategy.get_adaptive_threshold(recent)

    log.info(f"Snipe loop started — window={window_ts}  threshold={threshold:.0%}  "
             f"streak={win_streak}W/{loss_streak}L")

    while time.time() < deadline:
        eta = close_time - time.time()
        if eta <= 0:
            break
        try:
            candles       = price_feed.fetch_klines(limit=60)
            current_price = price_feed.get_current_price()
            if current_price <= 0 or not price_feed.is_connected():
                try:
                    current_price = price_feed.fetch_price_rest()
                except Exception:
                    current_price = candles[-1]["close"] if candles else 0
            ticks = price_feed.get_tick_history()
        except Exception as e:
            log.warning(f"Data fetch error: {e}")
            time.sleep(config.POLL_INTERVAL)
            continue

        if not candles or current_price <= 0:
            time.sleep(config.POLL_INTERVAL)
            continue

        # Check for oracle lag override opportunity
        oracle_st = oracle.get_state()
        if (oracle_st.signal and
            oracle_st.signal_confidence >= 0.60 and
            oracle_st.lag_seconds >= config.ORACLE_LAG_MIN_SEC):
            log.info(f"Oracle lag override during snipe! "
                     f"signal={oracle_st.signal} conf={oracle_st.signal_confidence:.2f}")
            delta = (current_price - window_open) / window_open * 100 if window_open > 0 else 0
            # Build minimal signal with oracle-boosted kelly
            oracle_kelly = min(
                strategy._kelly_fraction(0.80, strategy.estimate_token_price(abs(delta) / 100),
                                         mode_name, win_streak, loss_streak),
                config.KELLY_MAX_PCT
            )
            return SignalResult(
                direction        = oracle_st.signal,
                score            = 6.0 if oracle_st.signal == "UP" else -6.0,
                confidence       = 0.80,
                reasons          = [
                    f"Oracle lag arb: lag={oracle_st.lag_seconds:.1f}s "
                    f"div={oracle_st.divergence_pct:+.3f}% "
                    f"conf={oracle_st.signal_confidence:.2f}"
                ],
                window_delta_pct = delta,
                kelly_fraction   = oracle_kelly,
                regime           = "oracle",
                signal_quality   = "high",
            )

        sig = strategy.analyze(
            candles           = candles,
            window_open_price = window_open,
            current_price     = current_price,
            tick_history      = ticks,
            mode_name         = mode_name,
            win_streak        = win_streak,
            loss_streak       = loss_streak,
        )
        if sig is None:
            time.sleep(config.POLL_INTERVAL)
            continue

        # Score spike detection → fire immediately
        if best is not None and abs(sig.score - prev_score) >= 1.8:
            log.info(f"Score spike {prev_score:+.2f}→{sig.score:+.2f} — firing!")
            return sig

        # Threshold check
        if sig.confidence >= threshold:
            log.info(f"Threshold met at T-{eta:.0f}s: conf={sig.confidence:.1%}")
            return sig

        # Keep best seen
        best       = sig if (best is None or abs(sig.score) > abs(best.score)) else best
        prev_score = sig.score

        log.debug(
            f"  T-{eta:.0f}s  {sig.direction}  score={sig.score:+.2f}  "
            f"conf={sig.confidence:.0%}  kelly={sig.kelly_fraction:.1%}  "
            f"fund={sig.funding_rate*100:+.3f}%  regime={sig.regime}"
        )
        time.sleep(config.POLL_INTERVAL)

    return best   # never skip a window


# ─────────────────────────────────────────────────────────────────────
# Oracle lag strategy
# ─────────────────────────────────────────────────────────────────────
def _oracle_lag_signal(window_open: float, current_price: float,
                       mode_name: str) -> Optional[SignalResult]:
    st = oracle.get_state()
    if not st.signal or st.lag_seconds < config.ORACLE_LAG_MIN_SEC:
        return None
    delta = (current_price - window_open) / window_open * 100 if window_open > 0 else 0
    kelly = strategy._kelly_fraction(
        0.80, strategy.estimate_token_price(abs(delta) / 100),
        mode_name,
        risk.get_state().consecutive_win,
        risk.get_state().consecutive_loss,
    )
    return SignalResult(
        direction        = st.signal,
        score            = 6.0 if st.signal == "UP" else -6.0,
        confidence       = max(0.75, st.signal_confidence),
        reasons          = [
            f"Oracle lag: lag={st.lag_seconds:.1f}s div={st.divergence_pct:+.3f}% "
            f"conf={st.signal_confidence:.2f} xval={st.cross_validated}"
        ],
        window_delta_pct = delta,
        kelly_fraction   = kelly,
        regime           = "oracle",
        signal_quality   = "high",
    )


# ─────────────────────────────────────────────────────────────────────
# Execute trade
# ─────────────────────────────────────────────────────────────────────
def _execute_trade(
    mkt:         market.MarketInfo,
    direction:   str,
    sig:         SignalResult,
    mode_name:   str,
    dry_run:     bool,
) -> Tuple[float, float]:
    rs          = risk.get_state()
    bet_usd     = risk.bet_size(
        mode_name      = mode_name,
        kelly_fraction = sig.kelly_fraction,
        regime         = sig.regime,
    )
    delta_abs   = abs(sig.window_delta_pct / 100)
    token_price = strategy.estimate_token_price(delta_abs)

    if not dry_run:
        token_price = mkt.up_price if direction == "UP" else mkt.down_price
        if token_price <= 0:
            token_price = strategy.estimate_token_price(delta_abs)

    mode = config.MODES[mode_name]
    if token_price > mode.max_token_price:
        log.warning(f"Token ${token_price:.3f} > max ${mode.max_token_price:.3f} — skip")
        return 0, 0

    shares = bet_usd / token_price if token_price > 0 else 0
    lg.print_signal(
        window_ts    = mkt.window_ts,
        direction    = direction,
        confidence   = sig.confidence,
        score        = sig.score,
        window_delta = sig.window_delta_pct,
        token_price  = token_price,
        bet_usd      = bet_usd,
        reasons      = sig.reasons,
        dry_run      = dry_run,
        mode         = mode_name,
    )

    if dry_run:
        return bet_usd, token_price

    token_id = mkt.up_token_id if direction == "UP" else mkt.down_token_id
    result   = market.place_market_order(token_id, bet_usd, token_price)
    if result.success:
        log.info(f"Order filled: {result.shares:.2f} shares @ ${token_price:.3f}")
        return bet_usd, token_price

    log.info("Market order failed — trying limit @ $0.95")
    result = market.place_limit_order(token_id, bet_usd)
    if result.success:
        return bet_usd, config.LIMIT_BUY_PRICE

    log.error("Both order types failed")
    return 0, 0


# ─────────────────────────────────────────────────────────────────────
# Settle trade
# ─────────────────────────────────────────────────────────────────────
def _settle_trade(
    mkt:         market.MarketInfo,
    direction:   str,
    bet_usd:     float,
    token_price: float,
    sig:         SignalResult,
    mode_name:   str,
    dry_run:     bool,
):
    wait = max(mkt.close_time - time.time() + 2, 0)
    if wait > 0:
        log.info(f"Waiting {wait:.0f}s for window close…")
        time.sleep(wait)

    actual = price_feed.fetch_window_result(mkt.window_ts)
    if actual is None and not dry_run:
        for _ in range(6):
            actual = market.check_resolution(mkt)
            if actual:
                break
            time.sleep(5)

    if actual is None:
        log.warning("Outcome unknown — skipping P&L")
        return

    won    = direction == actual
    shares = bet_usd / token_price if token_price > 0 else 0
    if won:
        profit = shares * (1.0 - token_price)
        risk.record_win(profit, mode_name)
    else:
        risk.record_loss(bet_usd, mode_name)
        profit = -bet_usd

    # Feed cross-window momentum
    strategy.record_window_result(
        mkt.window_ts, direction, actual, sig.confidence, sig.window_delta_pct
    )

    state = risk.get_state()
    lg.print_result(direction, actual, profit, state.bankroll)
    lg.log_trade(
        window_ts      = mkt.window_ts,
        direction      = direction,
        token_price    = token_price,
        shares         = shares,
        bet_usd        = bet_usd,
        confidence     = sig.confidence,
        score          = sig.score,
        window_delta   = sig.window_delta_pct,
        reasons        = sig.reasons,
        mode           = mode_name,
        dry_run        = dry_run,
        result         = "WIN" if won else "LOSS",
        actual_dir     = actual,
        profit         = profit,
        bankroll_after = state.bankroll,
    )

    if won and not dry_run:
        market.redeem_positions(mkt.condition_id)

    log.info(risk.summary())


# ─────────────────────────────────────────────────────────────────────
# One trade cycle
# ─────────────────────────────────────────────────────────────────────
def run_cycle(mode_name: str = "ultra", strategy_name: str = "combined",
              dry_run: bool = True) -> bool:
    can, reason = risk.can_trade(mode_name)
    if not can:
        log.warning(f"Trade skipped: {reason}")
        return False

    window_ts    = market.current_window_ts()
    close_time   = window_ts + config.WINDOW_SECONDS
    seconds_left = close_time - time.time()

    log.info(
        f"\n{'═'*60}\n"
        f"  Window {window_ts}  closes in {seconds_left:.0f}s\n"
        f"  Mode: {mode_name}  Strategy: {strategy_name}  "
        f"{'[DRY]' if dry_run else '[LIVE]'}\n"
        f"{'═'*60}"
    )

    mkt = market.fetch_market_info(window_ts)
    if mkt is None:
        log.warning("Market info unavailable — skipping")
        return False

    try:
        window_open = price_feed.fetch_window_open_price(window_ts)
    except Exception as e:
        log.warning(f"Window open price error: {e}")
        candles     = price_feed.fetch_klines(limit=5)
        window_open = candles[0]["open"] if candles else 0
    if window_open <= 0:
        log.error("No window open price")
        return False

    log.info(f"BTC window open: ${window_open:,.2f}")

    direction: Optional[str]  = None
    sig:       Optional[SignalResult] = None

    if strategy_name == "oracle_lag":
        current = price_feed.get_current_price()
        sig = _oracle_lag_signal(window_open, current, mode_name)
        if sig is None:
            log.info("No oracle lag signal — skipping")
            return False
        direction = sig.direction

    elif strategy_name in ("snipe", "combined"):
        # Combined: check oracle lag first, then snipe
        if strategy_name == "combined":
            current  = price_feed.get_current_price()
            sig      = _oracle_lag_signal(window_open, current, mode_name)
            if sig:
                direction = sig.direction
                log.info(f"Using oracle lag signal: {direction}")

        if direction is None:
            wait_until = close_time - config.SNIPE_OFFSET
            wait_secs  = wait_until - time.time()
            if wait_secs > 0:
                log.info(f"Sleeping {wait_secs:.0f}s until T-{config.SNIPE_OFFSET}s…")
                time.sleep(max(wait_secs, 0))

            sig = _run_snipe_loop(window_ts, window_open, mode_name, dry_run)
            if sig is None:
                log.warning("Snipe loop: no signal")
                return False
            direction = sig.direction

    bet_usd, token_price = _execute_trade(mkt, direction, sig, mode_name, dry_run)
    if bet_usd <= 0:
        return False

    _settle_trade(mkt, direction, bet_usd, token_price, sig, mode_name, dry_run)
    return True


# ─────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC ULTRA Bot")
    parser.add_argument("--mode",        default="ultra",    choices=list(config.MODES.keys()))
    parser.add_argument("--strategy",    default="combined", choices=["snipe", "oracle_lag", "combined"])
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--once",        action="store_true")
    parser.add_argument("--max-trades",  type=int, default=0)
    parser.add_argument("--verbose",     action="store_true")
    args = parser.parse_args()

    lg.setup_logging(verbose=args.verbose)
    lg.print_trade_header()

    mode_cfg = config.MODES[args.mode]
    log.info(f"Mode: {args.mode} — {mode_cfg.description}")
    log.info(f"Strategy: {args.strategy}")
    log.info(f"Bankroll: ${risk.get_state().bankroll:.2f}")
    if args.dry_run:
        log.info("DRY RUN — no real money at risk")
    else:
        log.warning("LIVE TRADING — real USDC will be spent")
        if not config.POLY_PRIVATE_KEY:
            log.error("POLY_PRIVATE_KEY not set — cannot trade live")
            sys.exit(1)

    log.info("Starting Binance price feed…")
    price_feed.start_feed()
    time.sleep(1)

    log.info("Starting Chainlink oracle monitor…")
    oracle.start_oracle_monitor()
    time.sleep(2)

    trades_placed = 0
    log.info(f"Bot running (Ctrl+C to stop)\n")

    try:
        while True:
            ok, reason = risk.can_trade(args.mode)
            if not ok:
                log.error(f"Trading halted: {reason}")
                if args.dry_run:
                    risk.resume()
                    log.info("[dry run] Bankroll reset — continuing")
                else:
                    break

            if args.strategy == "oracle_lag":
                eta = market.seconds_until_close()
                if 8 <= eta <= 290:
                    placed = run_cycle(mode_name=args.mode,
                                       strategy_name="oracle_lag",
                                       dry_run=args.dry_run)
                    if placed:
                        trades_placed += 1
                time.sleep(4)
            else:
                window_ts  = market.current_window_ts()
                close_time = window_ts + config.WINDOW_SECONDS
                wake_at    = close_time - config.SNIPE_OFFSET
                now        = time.time()
                if now < wake_at - 1:
                    sleep_for = wake_at - now - 0.5
                    log.info(f"Next snipe in {sleep_for:.0f}s…")
                    time.sleep(max(sleep_for, 0))

                placed = run_cycle(mode_name=args.mode,
                                   strategy_name=args.strategy,
                                   dry_run=args.dry_run)
                if placed:
                    trades_placed += 1

                if args.once:
                    break
                if args.max_trades > 0 and trades_placed >= args.max_trades:
                    break

                time.sleep(2)

    except KeyboardInterrupt:
        log.info("\nBot stopped by user")

    log.info(f"\nTrades placed: {trades_placed}")
    log.info(risk.summary())
    log.info(f"Trade log: {config.TRADE_LOG_FILE}")


if __name__ == "__main__":
    main()
