"""
oracle_lag.py — 专注于 Oracle Lag 套利的高置信度检测模块

核心逻辑：
    1. 每 2 秒轮询一次 Chainlink（比原来的 5 秒快一倍）
    2. 同时从 Binance + OKX + Kraken 获取实时价格做交叉验证
    3. 用 5 个维度计算置信度分数（0.0 ~ 1.0）
    4. 只在置信度 >= HIGH_CONF_THRESHOLD 时发出信号
    5. 根据置信度分层给出建议的下注比例

置信度计算的 5 个维度：
    A. 滞后时长  —— lag 在 12~30s 甜区得高分，太短或太长都减分
    B. 价差大小  —— divergence 越大越好，但超过 0.5% 可能是假数据
    C. 交叉验证  —— 至少 2 个交易所价格方向一致才加分
    D. 动量确认  —— Binance 最近 10 秒的 tick 方向与信号方向一致
    E. 历史准确率 —— 用滑动窗口记录近 50 次信号的实际准确率并折射回分数

用法：
    import oracle_lag
    oracle_lag.start()                    # 启动后台监控
    sig = oracle_lag.get_signal()         # 非阻塞查询
    if sig and sig.confidence >= 0.70:
        direction = sig.direction         # "UP" or "DOWN"
        bet_pct   = sig.suggested_bet_pct # 建议下注比例 0.10 ~ 0.40
"""

import time
import logging
import threading
import statistics
import requests
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

import config
import price_feed

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 阈值配置（可以在 config.py 里覆盖）
# ─────────────────────────────────────────────────────────────────────────────

# 信号置信度门槛
HIGH_CONF_THRESHOLD   = getattr(config, "OL_HIGH_CONF",   0.70)  # 高置信度 → 下注
MEDIUM_CONF_THRESHOLD = getattr(config, "OL_MED_CONF",    0.55)  # 中等 → 小注
MIN_CONF_THRESHOLD    = getattr(config, "OL_MIN_CONF",    0.40)  # 低于此 → 不下注

# Lag 甜区（秒）
LAG_SWEET_MIN  = getattr(config, "OL_LAG_SWEET_MIN",  10)
LAG_SWEET_MAX  = getattr(config, "OL_LAG_SWEET_MAX",  35)
LAG_HARD_MAX   = getattr(config, "OL_LAG_HARD_MAX",   55)   # 超过此值认为 oracle 将要更新

# 价差门槛（%）
DIV_MIN        = getattr(config, "OL_DIV_MIN",        0.03)  # 最小有效价差
DIV_IDEAL_LOW  = getattr(config, "OL_DIV_IDEAL_LOW",  0.08)  # 开始得高分
DIV_IDEAL_HIGH = getattr(config, "OL_DIV_IDEAL_HIGH", 0.35)  # 超过此值可信度存疑
DIV_HARD_MAX   = getattr(config, "OL_DIV_HARD_MAX",   0.60)  # 超过此值可能是数据错误

# 动量确认：最近 N 秒的 tick 方向一致才加分
MOMENTUM_WINDOW_SEC = 10

# 历史准确率窗口
ACCURACY_WINDOW = 50   # 记录最近 50 次信号
ACCURACY_MIN_SAMPLES = 10  # 至少 10 次才参考历史

# Chainlink ABI 调用选择器
_LATEST_ROUND_SIG = "0xfeaf968c"

POLL_INTERVAL = 2.0   # 秒

# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LagSignal:
    """单次 oracle lag 信号，只有置信度足够才会产生"""
    direction:         str    = ""      # "UP" or "DOWN"
    confidence:        float  = 0.0     # 0.0 ~ 1.0
    suggested_bet_pct: float  = 0.0     # 建议下注比例

    # 调试信息
    lag_sec:           float  = 0.0     # Chainlink 滞后秒数
    divergence_pct:    float  = 0.0     # (binance - oracle) / oracle * 100
    oracle_price:      float  = 0.0
    cex_price:         float  = 0.0     # 共识 CEX 价格
    n_exchanges:       int    = 0       # 参与交叉验证的交易所数量
    momentum_aligned:  bool   = False   # tick 动量与信号方向一致？
    hist_accuracy:     float  = -1.0    # 历史信号准确率（-1 表示样本不足）

    # 置信度细分（便于调试）
    score_lag:      float = 0.0
    score_div:      float = 0.0
    score_cross:    float = 0.0
    score_momentum: float = 0.0
    score_history:  float = 0.0

    detected_at:    float = 0.0   # unix timestamp


@dataclass
class _SignalOutcome:
    """记录信号的实际结果，用于历史准确率统计"""
    direction: str
    detected_at: float
    resolved_at: float = 0.0
    correct: Optional[bool] = None   # None = 尚未结算


# ─────────────────────────────────────────────────────────────────────────────
# 模块内部状态
# ─────────────────────────────────────────────────────────────────────────────

_lock    = threading.Lock()
_running = False

# 最新信号（None 表示当前无信号）
_current_signal: Optional[LagSignal] = None

# 历史信号结果，用于计算准确率
_signal_history: deque = deque(maxlen=ACCURACY_WINDOW)

# Chainlink 原始状态
_oracle_price:    float = 0.0
_oracle_updated:  float = 0.0   # on-chain timestamp

# 信号计数
_total_signals = 0
_thread: Optional[threading.Thread] = None


# ─────────────────────────────────────────────────────────────────────────────
# 公开 API
# ─────────────────────────────────────────────────────────────────────────────

def start():
    """启动后台 oracle 监控线程"""
    global _thread, _running
    if _thread and _thread.is_alive():
        return
    _running = True
    _thread = threading.Thread(target=_monitor_loop, daemon=True, name="OracleLag")
    _thread.start()
    logger.info("OracleLag monitor started (poll=%.1fs)", POLL_INTERVAL)


def stop():
    global _running
    _running = False


def get_signal() -> Optional[LagSignal]:
    """
    返回当前置信度最高的信号（如果有）。
    只有置信度 >= MIN_CONF_THRESHOLD 的信号才会被保留。
    主循环调用此函数后应在 5 秒内决策，否则信号可能已过期。
    """
    with _lock:
        return _current_signal


def record_outcome(direction: str, detected_at: float, correct: bool):
    """
    结算后调用，告诉模块这次信号猜对了还是猜错了。
    bot.py 在 _settle_trade() 里调用这个函数。
    """
    with _lock:
        _signal_history.append(_SignalOutcome(
            direction=direction,
            detected_at=detected_at,
            resolved_at=time.time(),
            correct=correct,
        ))
    logger.info(
        "OracleLag outcome recorded: dir=%s correct=%s  history=%d samples",
        direction, correct, len(_signal_history)
    )


def get_stats() -> dict:
    """返回调试/仪表盘用的统计信息"""
    with _lock:
        resolved = [s for s in _signal_history if s.correct is not None]
        acc = sum(1 for s in resolved if s.correct) / len(resolved) if resolved else -1
        return {
            "total_signals": _total_signals,
            "history_samples": len(resolved),
            "historical_accuracy": round(acc, 3) if acc >= 0 else None,
            "oracle_price": round(_oracle_price, 2),
            "oracle_lag_sec": round(time.time() - _oracle_updated, 1) if _oracle_updated else None,
            "current_signal": _current_signal,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 内部：Chainlink 轮询
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_chainlink() -> Optional[Tuple[float, float]]:
    """
    调用 Polygon RPC，返回 (oracle_price, updated_at_timestamp)
    失败返回 None
    """
    payload = {
        "jsonrpc": "2.0",
        "method":  "eth_call",
        "params":  [
            {"to": config.CHAINLINK_CONTRACT, "data": _LATEST_ROUND_SIG},
            "latest",
        ],
        "id": 1,
    }
    try:
        r = requests.post(config.CHAINLINK_ORACLE_URL, json=payload, timeout=3)
        r.raise_for_status()
        hex_data = r.json().get("result", "")
        return _parse_round_data(hex_data)
    except Exception as e:
        logger.debug("Chainlink fetch failed: %s", e)
        return None


def _parse_round_data(hex_result: str) -> Optional[Tuple[float, float]]:
    """解析 latestRoundData 的 ABI 编码返回值"""
    if not hex_result or hex_result == "0x":
        return None
    data = hex_result[2:]
    if len(data) < 320:
        return None
    try:
        answer     = int(data[64:128], 16)    # slot 1: int256 answer
        updated_at = int(data[192:256], 16)   # slot 3: uint256 updatedAt
        price = answer / 1e8                  # Chainlink BTC/USD = 8 decimals
        if price < 1000 or price > 1_000_000:
            return None   # 明显异常值
        return price, float(updated_at)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 内部：多交易所价格交叉验证
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_okx_price() -> Optional[float]:
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": "BTC-USDT"},
            timeout=2
        )
        data = r.json().get("data", [{}])[0]
        return float(data["last"])
    except Exception:
        return None


def _fetch_kraken_price() -> Optional[float]:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "XBTUSD"},
            timeout=2
        )
        result = r.json().get("result", {})
        if not result:
            return None
        pair_data = next(iter(result.values()))
        return float(pair_data["c"][0])
    except Exception:
        return None


def _get_consensus_price() -> Tuple[Optional[float], int]:
    """
    从 Binance + OKX + Kraken 获取价格，
    返回 (中位数共识价格, 一致的交易所数量)
    """
    prices = []

    binance = price_feed.get_current_price()
    if binance > 0:
        prices.append(binance)

    okx = _fetch_okx_price()
    if okx and okx > 0:
        prices.append(okx)

    kraken = _fetch_kraken_price()
    if kraken and kraken > 0:
        prices.append(kraken)

    if not prices:
        return None, 0

    median = statistics.median(prices)

    # 过滤偏离中位数超过 0.3% 的异常值
    valid = [p for p in prices if abs(p - median) / median < 0.003]

    if not valid:
        return None, 0

    return statistics.median(valid), len(valid)


# ─────────────────────────────────────────────────────────────────────────────
# 内部：动量验证
# ─────────────────────────────────────────────────────────────────────────────

def _check_momentum(direction: str) -> bool:
    """
    检查最近 MOMENTUM_WINDOW_SEC 秒内的 Binance tick 方向
    是否与信号方向一致（超过 60% 的 tick 方向一致则通过）
    """
    ticks = price_feed.get_tick_history()
    if len(ticks) < 4:
        return False

    now = time.time()
    recent = [(ts, p) for ts, p in ticks if now - ts <= MOMENTUM_WINDOW_SEC]
    if len(recent) < 3:
        return False

    # 计算相邻 tick 的涨跌方向
    ups = downs = 0
    for i in range(1, len(recent)):
        delta = recent[i][1] - recent[i-1][1]
        if delta > 0:
            ups += 1
        elif delta < 0:
            downs += 1

    total = ups + downs
    if total == 0:
        return False

    if direction == "UP":
        return ups / total >= 0.60
    else:
        return downs / total >= 0.60


# ─────────────────────────────────────────────────────────────────────────────
# 内部：置信度计算（核心逻辑）
# ─────────────────────────────────────────────────────────────────────────────

def _compute_confidence(
    lag: float,
    div_abs: float,
    n_exchanges: int,
    momentum_aligned: bool,
) -> Tuple[float, dict]:
    """
    返回 (total_confidence_0_to_1, score_breakdown_dict)

    5 个维度各自评分，然后加权求和：
        Lag score       权重 30%
        Divergence      权重 30%
        Cross-exchange  权重 20%
        Momentum        权重 12%
        History         权重  8%
    """

    # ── A. Lag 分数 ────────────────────────────────────────────
    # 理想区间 [LAG_SWEET_MIN, LAG_SWEET_MAX]，边缘衰减
    if lag < LAG_SWEET_MIN:
        # 太早：oracle 可能还没真正滞后
        s_lag = (lag / LAG_SWEET_MIN) * 0.6
    elif lag <= LAG_SWEET_MAX:
        # 甜区：满分
        s_lag = 1.0
    elif lag <= LAG_HARD_MAX:
        # 开始老化：线性衰减到 0.2
        ratio = (lag - LAG_SWEET_MAX) / (LAG_HARD_MAX - LAG_SWEET_MAX)
        s_lag = 1.0 - 0.8 * ratio
    else:
        # 太老：oracle 已快要更新，机会窗口关闭
        s_lag = 0.0

    # ── B. 价差分数 ─────────────────────────────────────────────
    if div_abs < DIV_MIN:
        s_div = 0.0
    elif div_abs < DIV_IDEAL_LOW:
        # 从 DIV_MIN 到 DIV_IDEAL_LOW 线性增长到 0.7
        s_div = 0.7 * (div_abs - DIV_MIN) / (DIV_IDEAL_LOW - DIV_MIN)
    elif div_abs <= DIV_IDEAL_HIGH:
        # 理想区间：满分
        s_div = 1.0
    elif div_abs <= DIV_HARD_MAX:
        # 过大可能是数据噪声：线性衰减到 0.5
        ratio = (div_abs - DIV_IDEAL_HIGH) / (DIV_HARD_MAX - DIV_IDEAL_HIGH)
        s_div = 1.0 - 0.5 * ratio
    else:
        # 超过硬上限：很可能是数据错误
        s_div = 0.0

    # ── C. 交叉验证分数 ─────────────────────────────────────────
    if n_exchanges >= 3:
        s_cross = 1.0
    elif n_exchanges == 2:
        s_cross = 0.65
    elif n_exchanges == 1:
        s_cross = 0.30
    else:
        s_cross = 0.0

    # ── D. 动量分数 ─────────────────────────────────────────────
    s_momentum = 1.0 if momentum_aligned else 0.0

    # ── E. 历史准确率分数 ──────────────────────────────────────
    with _lock:
        resolved = [s for s in _signal_history if s.correct is not None]

    if len(resolved) < ACCURACY_MIN_SAMPLES:
        # 样本不足时取中性分数，不惩罚也不奖励
        s_history = 0.60
    else:
        acc = sum(1 for s in resolved if s.correct) / len(resolved)
        # 准确率 55% → 0.0 分，70% → 1.0 分（线性）
        s_history = max(0.0, min(1.0, (acc - 0.55) / 0.15))

    # ── 加权合并 ────────────────────────────────────────────────
    weights = {
        "lag":      0.30,
        "div":      0.30,
        "cross":    0.20,
        "momentum": 0.12,
        "history":  0.08,
    }
    total = (
        s_lag      * weights["lag"] +
        s_div      * weights["div"] +
        s_cross    * weights["cross"] +
        s_momentum * weights["momentum"] +
        s_history  * weights["history"]
    )

    breakdown = {
        "score_lag":      round(s_lag, 3),
        "score_div":      round(s_div, 3),
        "score_cross":    round(s_cross, 3),
        "score_momentum": round(s_momentum, 3),
        "score_history":  round(s_history, 3),
    }

    return round(min(total, 1.0), 4), breakdown


# ─────────────────────────────────────────────────────────────────────────────
# 内部：建议下注比例
# ─────────────────────────────────────────────────────────────────────────────

def _suggested_bet(confidence: float) -> float:
    """
    根据置信度返回建议的下注比例（占当前余额的比例）。
    不使用 Kelly 公式，而是简单的分层规则——
    在历史准确率不确定时，这比 Kelly 更安全。

    注意：这只是建议值，最终大小由 risk.py 的硬上限管理。
    """
    if confidence >= 0.85:
        return 0.35   # 极高置信度 → 下 35%
    elif confidence >= 0.75:
        return 0.25   # 高置信度 → 下 25%
    elif confidence >= 0.65:
        return 0.15   # 中高置信度 → 下 15%
    elif confidence >= MIN_CONF_THRESHOLD:
        return 0.08   # 勉强达标 → 只下 8%，观察用
    else:
        return 0.0    # 不达标 → 不下注


# ─────────────────────────────────────────────────────────────────────────────
# 内部：主监控循环
# ─────────────────────────────────────────────────────────────────────────────

def _monitor_loop():
    global _running, _oracle_price, _oracle_updated, _current_signal, _total_signals

    logger.info("OracleLag loop running (interval=%.1fs)", POLL_INTERVAL)

    # 用于去重：避免同一个 lag 窗口内重复发出信号
    _last_signal_oracle_ts: float = 0.0

    while _running:
        loop_start = time.time()
        new_signal: Optional[LagSignal] = None

        try:
            # 1. 获取 Chainlink 最新价格
            chainlink_result = _fetch_chainlink()
            if not chainlink_result:
                _maybe_clear_signal()
                time.sleep(POLL_INTERVAL)
                continue

            oracle_price, oracle_updated_at = chainlink_result
            now = time.time()
            lag = now - oracle_updated_at

            with _lock:
                _oracle_price   = oracle_price
                _oracle_updated = oracle_updated_at

            # 2. 获取 CEX 共识价格
            cex_price, n_exchanges = _get_consensus_price()
            if not cex_price or n_exchanges == 0:
                _maybe_clear_signal()
                time.sleep(POLL_INTERVAL)
                continue

            # 3. 计算价差
            divergence = (cex_price - oracle_price) / oracle_price * 100
            div_abs = abs(divergence)

            # 4. 快速过滤：价差不够直接跳过
            if div_abs < DIV_MIN:
                _maybe_clear_signal()
                time.sleep(POLL_INTERVAL)
                continue

            # 5. 确定信号方向
            direction = "UP" if divergence > 0 else "DOWN"

            # 6. 动量确认
            momentum_ok = _check_momentum(direction)

            # 7. 计算置信度
            confidence, breakdown = _compute_confidence(
                lag=lag,
                div_abs=div_abs,
                n_exchanges=n_exchanges,
                momentum_aligned=momentum_ok,
            )

            # 8. 只在置信度 >= 门槛时发出信号
            if confidence >= MIN_CONF_THRESHOLD:

                # 去重：同一个 oracle 轮次不重复发出信号
                if oracle_updated_at == _last_signal_oracle_ts:
                    # oracle 没更新，当前信号仍有效，无需重新发出
                    time.sleep(POLL_INTERVAL)
                    continue

                bet_pct = _suggested_bet(confidence)

                new_signal = LagSignal(
                    direction         = direction,
                    confidence        = confidence,
                    suggested_bet_pct = bet_pct,
                    lag_sec           = round(lag, 1),
                    divergence_pct    = round(divergence, 4),
                    oracle_price      = round(oracle_price, 2),
                    cex_price         = round(cex_price, 2),
                    n_exchanges       = n_exchanges,
                    momentum_aligned  = momentum_ok,
                    score_lag         = breakdown["score_lag"],
                    score_div         = breakdown["score_div"],
                    score_cross       = breakdown["score_cross"],
                    score_momentum    = breakdown["score_momentum"],
                    score_history     = breakdown["score_history"],
                    detected_at       = now,
                )

                _last_signal_oracle_ts = oracle_updated_at
                _total_signals += 1

                level = "HIGH" if confidence >= HIGH_CONF_THRESHOLD else "MEDIUM"
                logger.info(
                    "OracleLag [%s] dir=%-4s  conf=%.2f  lag=%5.1fs  div=%+.3f%%  "
                    "cex=$%.2f  oracle=$%.2f  n_ex=%d  mom=%s  bet=%.0f%%",
                    level, direction, confidence, lag, divergence,
                    cex_price, oracle_price, n_exchanges,
                    "ok" if momentum_ok else "no",
                    bet_pct * 100,
                )
            else:
                new_signal = None
                _last_signal_oracle_ts = 0.0   # 置信度不足时重置，允许下次重新发出

        except Exception as e:
            logger.warning("OracleLag loop error: %s", e, exc_info=True)
            new_signal = None

        with _lock:
            _current_signal = new_signal

        # 精确控制轮询间隔
        elapsed = time.time() - loop_start
        sleep_time = max(0.1, POLL_INTERVAL - elapsed)
        time.sleep(sleep_time)


def _maybe_clear_signal():
    """无有效 oracle 数据时，清除当前信号"""
    global _current_signal
    with _lock:
        _current_signal = None


# ─────────────────────────────────────────────────────────────────────────────
# 便捷函数：供 bot.py 直接调用
# ─────────────────────────────────────────────────────────────────────────────

def is_high_confidence() -> bool:
    """最简单的接口：当前是否有高置信度信号"""
    sig = get_signal()
    return sig is not None and sig.confidence >= HIGH_CONF_THRESHOLD


def wait_for_signal(timeout_sec: float = 60.0, min_conf: float = HIGH_CONF_THRESHOLD) -> Optional[LagSignal]:
    """
    阻塞等待，直到出现满足置信度要求的信号或超时。

    用法（在 bot.py 的窗口循环里）：
        sig = oracle_lag.wait_for_signal(timeout_sec=50, min_conf=0.70)
        if sig:
            ... place bet ...
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        sig = get_signal()
        if sig and sig.confidence >= min_conf:
            return sig
        time.sleep(0.5)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 命令行测试入口
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    print("=" * 60)
    print("Oracle Lag 实时监控  (Ctrl+C 退出)")
    print(f"  置信度门槛: HIGH={HIGH_CONF_THRESHOLD:.0%}  MED={MEDIUM_CONF_THRESHOLD:.0%}")
    print(f"  Lag 甜区:   {LAG_SWEET_MIN}s ~ {LAG_SWEET_MAX}s")
    print(f"  价差门槛:   {DIV_MIN:.2%} ~ {DIV_IDEAL_HIGH:.2%}")
    print("=" * 60)

    # 启动价格推送
    price_feed.start_feed()
    time.sleep(2)   # 等待 WS 连接

    # 启动 oracle 监控
    start()

    try:
        while True:
            time.sleep(3)
            stats = get_stats()
            sig   = stats["current_signal"]

            lag_str = f"{stats['oracle_lag_sec']:.1f}s" if stats["oracle_lag_sec"] else "N/A"
            acc_str = (
                f"{stats['historical_accuracy']:.1%}"
                if stats["historical_accuracy"] is not None
                else "N/A (样本不足)"
            )

            print(
                f"\r  oracle=${stats['oracle_price']:,.2f}  "
                f"lag={lag_str}  "
                f"signals={stats['total_signals']}  "
                f"acc={acc_str}  ",
                end="",
                flush=True,
            )

            if sig:
                print(f"\n  >>> SIGNAL {sig.direction}  conf={sig.confidence:.2f}  "
                      f"bet={sig.suggested_bet_pct:.0%}  "
                      f"[lag={sig.score_lag:.2f} div={sig.score_div:.2f} "
                      f"cross={sig.score_cross:.2f} mom={sig.score_momentum:.2f}]")

    except KeyboardInterrupt:
        print("\n退出")
        stop()
