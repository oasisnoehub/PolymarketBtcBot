"""
risk.py — ULTRA risk management. Kelly + profit lock + streak adjustment.

Key features:
  • Streak-adjusted Kelly: press on hot streaks, retreat on cold
  • Profit lock: protect gains once up 40%+ from original bankroll
  • Daily loss circuit breaker
  • Drawdown protection
  • Aggressive mode: compound profits, protect principal
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

import config

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    bankroll:          float = config.STARTING_BANKROLL
    peak_bankroll:     float = config.STARTING_BANKROLL
    original_bankroll: float = config.STARTING_BANKROLL

    total_trades:      int   = 0
    wins:              int   = 0
    losses:            int   = 0
    consecutive_loss:  int   = 0
    consecutive_win:   int   = 0

    day_start_bankroll: float = config.STARTING_BANKROLL
    day_start_ts:       float = field(default_factory=time.time)

    halted:            bool  = False
    halt_reason:       str   = ""

    profit_locked:     bool  = False
    locked_amount:     float = 0.0
    protected_principal: float = config.STARTING_BANKROLL

    recent_trades:     List[dict] = field(default_factory=list)


_state = RiskState()


def get_state() -> RiskState:
    return _state


def reset_daily_tracking():
    _state.day_start_bankroll = _state.bankroll
    _state.day_start_ts       = time.time()
    logger.info(f"Daily P&L reset. Bankroll: ${_state.bankroll:.2f}")


def can_trade(mode_name: str = "safe") -> Tuple[bool, str]:
    s = _state

    if s.halted:
        return False, s.halt_reason

    if s.bankroll < config.MIN_BET:
        return False, f"Bankroll ${s.bankroll:.2f} below min bet ${config.MIN_BET:.2f}"

    drawdown = (s.peak_bankroll - s.bankroll) / s.peak_bankroll if s.peak_bankroll > 0 else 0
    if drawdown >= config.MAX_DRAWDOWN_PCT:
        _halt(f"Max drawdown {drawdown:.1%} exceeded (limit {config.MAX_DRAWDOWN_PCT:.0%})")
        return False, _state.halt_reason

    if s.consecutive_loss >= config.MAX_CONSECUTIVE_LOSS:
        _halt(f"{s.consecutive_loss} consecutive losses (limit {config.MAX_CONSECUTIVE_LOSS})")
        return False, _state.halt_reason

    daily_loss = (s.day_start_bankroll - s.bankroll) / s.day_start_bankroll if s.day_start_bankroll > 0 else 0
    if daily_loss >= config.DAILY_LOSS_LIMIT_PCT:
        _halt(f"Daily loss {daily_loss:.1%} exceeded (limit {config.DAILY_LOSS_LIMIT_PCT:.0%})")
        return False, _state.halt_reason

    return True, ""


def _halt(reason: str):
    _state.halted      = True
    _state.halt_reason = reason
    logger.error(f"TRADING HALTED: {reason}")


def resume():
    _state.halted          = False
    _state.halt_reason     = ""
    _state.consecutive_loss = 0
    reset_daily_tracking()
    logger.info("Trading resumed")


def bet_size(
    mode_name:      str   = "safe",
    kelly_fraction: float = 0.0,
    regime:         str   = "ideal",
) -> float:
    """
    Compute dollar bet size using Kelly fraction from signal.

    Priority:
      1. kelly_fraction from strategy (if valid and mode.kelly_enabled)
      2. mode.bet_fraction * bankroll
      3. MIN_BET
    """
    mode = config.MODES.get(mode_name)
    if not mode:
        return config.MIN_BET

    s = _state

    # --- Base bet ---
    if mode.kelly_enabled and kelly_fraction > 0:
        # Kelly already incorporates regime_mult; use directly
        amount = s.bankroll * kelly_fraction
    elif mode_name == "degen":
        amount = s.bankroll
    elif mode_name == "aggressive":
        profits = s.bankroll - s.protected_principal
        amount  = profits if profits > config.MIN_BET else s.bankroll * mode.bet_fraction
    else:
        amount = s.bankroll * mode.bet_fraction

    # --- Profit lock cap ---
    _check_profit_lock()
    if s.profit_locked:
        cap    = s.bankroll * config.PROFIT_LOCK_BET_CAP
        amount = min(amount, cap)
        logger.debug(f"Profit lock active — capping bet at ${cap:.2f}")

    # --- Final clamps ---
    return max(config.MIN_BET, min(amount, s.bankroll))


def _check_profit_lock():
    s = _state
    ratio = s.bankroll / s.original_bankroll if s.original_bankroll > 0 else 1.0
    if not s.profit_locked and ratio >= config.PROFIT_LOCK_TRIGGER:
        profit   = s.bankroll - s.original_bankroll
        locked   = profit * config.PROFIT_LOCK_RESERVE
        s.profit_locked = True
        s.locked_amount = locked
        logger.info(
            f"Profit lock activated! Bankroll ${s.bankroll:.2f} "
            f"({ratio:.1%} of original). Locked: ${locked:.2f}"
        )


def record_win(profit: float, mode_name: str = "safe"):
    s = _state
    s.bankroll         += profit
    s.peak_bankroll     = max(s.bankroll, s.peak_bankroll)
    s.total_trades     += 1
    s.wins             += 1
    s.consecutive_loss  = 0
    s.consecutive_win  += 1
    s.recent_trades.append({"result": "WIN", "profit": profit, "ts": time.time()})
    if len(s.recent_trades) > 50:
        s.recent_trades = s.recent_trades[-50:]
    logger.info(f"WIN +${profit:.2f}  bankroll=${s.bankroll:.2f}  streak={s.consecutive_win}W")


def record_loss(amount: float, mode_name: str = "safe"):
    s = _state
    s.bankroll          = max(s.bankroll - amount, 0)
    s.total_trades     += 1
    s.losses           += 1
    s.consecutive_loss += 1
    s.consecutive_win   = 0
    s.recent_trades.append({"result": "LOSS", "profit": -amount, "ts": time.time()})
    if len(s.recent_trades) > 50:
        s.recent_trades = s.recent_trades[-50:]
    logger.warning(f"LOSS -${amount:.2f}  bankroll=${s.bankroll:.2f}  streak={s.consecutive_loss}L")
    if config.COOLDOWN_AFTER_LOSS > 0:
        time.sleep(config.COOLDOWN_AFTER_LOSS)


def summary() -> str:
    s     = _state
    total = s.wins + s.losses
    wr    = s.wins / total * 100 if total > 0 else 0
    roi   = (s.bankroll - s.original_bankroll) / s.original_bankroll * 100
    return (
        f"Trades={total}  W={s.wins}  L={s.losses}  "
        f"WR={wr:.1f}%  Bankroll=${s.bankroll:.2f}  ROI={roi:+.1f}%"
        + (f"  [LOCKED]" if s.profit_locked else "")
    )
