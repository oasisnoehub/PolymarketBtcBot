"""
market.py — Polymarket CLOB API wrapper.

Handles:
  • Market discovery (clock-based slug, no scanning needed)
  • Token price fetching from the orderbook
  • Order placement (FOK market buy + GTC limit fallback)
  • Position redemption after resolution
  • Market info caching to reduce API calls
"""

import time
import logging
import requests
from dataclasses import dataclass
from typing import Optional, Tuple

import config

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    condition_id:  str
    up_token_id:   str
    down_token_id: str
    up_price:      float
    down_price:    float
    window_ts:     int
    close_time:    int
    resolved:      bool = False
    winner:        Optional[str] = None   # 'UP' or 'DOWN'


@dataclass
class OrderResult:
    success:     bool
    order_id:    str        = ""
    shares:      float      = 0.0
    price_paid:  float      = 0.0
    error:       str        = ""


# ─────────────────────────────────────────────────────────────
# Clock-based market discovery — no API scanning needed
# ─────────────────────────────────────────────────────────────
def current_window_ts() -> int:
    """Return the Unix timestamp of the start of the current 5-min window."""
    now = int(time.time())
    return now - (now % config.WINDOW_SECONDS)


def next_window_ts() -> int:
    return current_window_ts() + config.WINDOW_SECONDS


def seconds_until_close() -> float:
    """Seconds remaining until the current window closes."""
    close = current_window_ts() + config.WINDOW_SECONDS
    return close - time.time()


def market_slug(window_ts: int) -> str:
    return f"btc-updown-5m-{window_ts}"


# ─────────────────────────────────────────────────────────────
# Market info fetch via Gamma API
# ─────────────────────────────────────────────────────────────
def fetch_market_info(window_ts: int) -> Optional[MarketInfo]:
    """
    Fetch current market info for the given 5-min window.
    Returns None if market not found or API error.
    """
    slug = market_slug(window_ts)
    try:
        resp = requests.get(
            f"{config.GAMMA_API}/events",
            params={"slug": slug},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data if isinstance(data, list) else data.get("data", [data])
        if not events:
            logger.debug(f"No market found for slug {slug}")
            return None

        event = events[0]
        markets = event.get("markets", [])
        if not markets:
            return None

        market     = markets[0]
        condition  = market.get("conditionId", "")
        outcomes   = market.get("outcomes", ["Up", "Down"])
        token_ids  = market.get("clobTokenIds", [])

        if len(token_ids) < 2:
            logger.warning(f"Market {slug} missing token IDs")
            return None

        # Determine which token is UP and which is DOWN
        up_idx   = 0 if "up" in outcomes[0].lower() else 1
        dn_idx   = 1 - up_idx
        up_tid   = token_ids[up_idx]
        dn_tid   = token_ids[dn_idx]

        # Fetch orderbook prices for both tokens
        up_price = _get_best_ask(up_tid)
        dn_price = _get_best_ask(dn_tid)

        return MarketInfo(
            condition_id  = condition,
            up_token_id   = up_tid,
            down_token_id = dn_tid,
            up_price      = up_price,
            down_price    = dn_price,
            window_ts     = window_ts,
            close_time    = window_ts + config.WINDOW_SECONDS,
        )
    except Exception as e:
        logger.error(f"fetch_market_info failed: {e}")
        return None


def _get_best_ask(token_id: str) -> float:
    """Fetch the best ask price for a token from the CLOB orderbook."""
    try:
        resp = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=4,
        )
        resp.raise_for_status()
        book = resp.json()
        asks = book.get("asks", [])
        if asks:
            return float(asks[0]["price"])
    except Exception as e:
        logger.debug(f"get_best_ask failed for {token_id[:8]}…: {e}")
    return 0.50   # default


def refresh_prices(info: MarketInfo) -> MarketInfo:
    """Update the UP/DOWN prices in a cached MarketInfo."""
    info.up_price   = _get_best_ask(info.up_token_id)
    info.down_price = _get_best_ask(info.down_token_id)
    return info


# ─────────────────────────────────────────────────────────────
# Order placement (requires py-clob-client)
# ─────────────────────────────────────────────────────────────
def _get_clob_client():
    """Lazily import and build the CLOB client (avoids import errors in dry-run)."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key        = config.POLY_API_KEY,
            api_secret     = config.POLY_API_SECRET,
            api_passphrase = config.POLY_API_PASSPHRASE,
        )
        client = ClobClient(
            host            = config.CLOB_API,
            chain_id        = 137,      # Polygon mainnet
            private_key     = config.POLY_PRIVATE_KEY,
            creds           = creds,
            signature_type  = config.POLY_SIGNATURE_TYPE,
            funder          = config.POLY_FUNDER_ADDRESS,
        )
        return client
    except ImportError:
        logger.error("py-clob-client not installed — cannot place live orders")
        return None
    except Exception as e:
        logger.error(f"CLOB client init failed: {e}")
        return None


def place_market_order(
    token_id:   str,
    usd_amount: float,
    token_price: float,
) -> OrderResult:
    """
    Place a FOK (Fill-or-Kill) market buy order.
    Retries up to 3 times on failure.
    """
    client = _get_clob_client()
    if not client:
        return OrderResult(success=False, error="CLOB client unavailable")

    shares = max(usd_amount / token_price, config.MIN_SHARES)
    shares = round(shares, 2)

    for attempt in range(3):
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            order_args = MarketOrderArgs(
                token_id    = token_id,
                amount      = shares,
            )
            resp = client.create_market_order(order_args)
            if resp and resp.get("status") in ("matched", "filled"):
                return OrderResult(
                    success    = True,
                    order_id   = resp.get("orderID", ""),
                    shares     = shares,
                    price_paid = token_price * shares,
                )
            logger.warning(f"Order attempt {attempt+1} status: {resp}")
        except Exception as e:
            logger.warning(f"Market order attempt {attempt+1} failed: {e}")
        time.sleep(3)

    return OrderResult(success=False, error="All 3 market order attempts failed")


def place_limit_order(
    token_id:    str,
    usd_amount:  float,
    limit_price: float = config.LIMIT_BUY_PRICE,
) -> OrderResult:
    """
    GTC limit buy at $0.95 — used when no ask-side liquidity exists.
    Profit = $0.05/share if the token resolves to $1.00.
    """
    client = _get_clob_client()
    if not client:
        return OrderResult(success=False, error="CLOB client unavailable")

    shares = max(usd_amount / limit_price, config.MIN_SHARES)
    shares = round(shares, 2)

    try:
        from py_clob_client.clob_types import LimitOrderArgs, OrderType
        order_args = LimitOrderArgs(
            token_id   = token_id,
            price      = limit_price,
            size       = shares,
            side       = "BUY",
            order_type = OrderType.GTC,
        )
        resp = client.create_order(order_args)
        if resp:
            return OrderResult(
                success    = True,
                order_id   = resp.get("orderID", ""),
                shares     = shares,
                price_paid = limit_price * shares,
            )
    except Exception as e:
        logger.error(f"Limit order failed: {e}")

    return OrderResult(success=False, error="Limit order failed")


def redeem_positions(condition_id: str) -> bool:
    """Redeem winning tokens after market resolution."""
    client = _get_clob_client()
    if not client:
        return False
    try:
        resp = client.redeem_positions(condition_id)
        logger.info(f"Redeem response: {resp}")
        return True
    except Exception as e:
        logger.error(f"Redeem failed for {condition_id}: {e}")
        return False


def check_resolution(info: MarketInfo) -> Optional[str]:
    """
    Poll Polymarket to see if the market has resolved.
    Returns 'UP', 'DOWN', or None if still open.
    """
    try:
        resp = requests.get(
            f"{config.GAMMA_API}/events",
            params={"slug": market_slug(info.window_ts)},
            timeout=5,
        )
        resp.raise_for_status()
        data    = resp.json()
        events  = data if isinstance(data, list) else data.get("data", [data])
        if not events:
            return None
        market = events[0].get("markets", [{}])[0]
        resolved = market.get("resolved", False)
        if resolved:
            winners = market.get("winners", [])
            if winners:
                w = winners[0].lower()
                return "UP" if "up" in w else "DOWN"
    except Exception as e:
        logger.debug(f"check_resolution error: {e}")
    return None
