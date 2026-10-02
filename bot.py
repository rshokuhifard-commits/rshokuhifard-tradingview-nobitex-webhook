#!/usr/bin/env python3
import os
import time
import json
import urllib.request
import base64
import logging
import requests
from datetime import datetime, timezone
from nacl.signing import SigningKey

# =========================
# CONFIG
# =========================
BASE = os.getenv("NOBITEX_BASE_URL", "https://apiv2.nobitex.ir")
SYMBOL = os.getenv("SYMBOL", "BCHUSDT").upper()

API_KEY = os.getenv("NOBITEX_API_KEY", "").strip()
API_SECRET = os.getenv("NOBITEX_API_SECRET", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in (
    "1", "true", "yes", "on"
)

HISTORY_1M = int(os.getenv("HISTORY_1M", "1500"))
MIN_3M = int(os.getenv("MIN_3M", "350"))
POLL = int(os.getenv("POLL_SECONDS", "15"))

LEVERAGE = os.getenv("LEVERAGE", "2")
COLLATERAL = float(os.getenv("COLLATERAL", "10"))

SL = float(os.getenv("SL_PCT", "0.012"))
TP = float(os.getenv("TP_PCT", "0.024"))
TRAIL = float(os.getenv("TRAIL_PCT", "0.010"))
TRAIL_ACTIVATION = float(os.getenv("TRAIL_ACTIVATION_PCT", "0.010"))

# Pine MA5/MA8/MA256 + pivot SL
PINE_SL_TICKS = int(os.getenv("PINE_SL_TICKS", "10"))

# Nobitex price precision / Pine syminfo.mintick equivalent.
# Values obtained from Nobitex /v2/options.
PINE_TICK_SIZES = {
    "BCHUSDT": 0.01,
    "POLUSDT": 0.0001,
    "SOLUSDT": 0.001,
    "UNIUSDT": 0.001,
    "AAVEUSDT": 0.01,
}

PINE_TICK_SIZE = PINE_TICK_SIZES.get(SYMBOL, 0.0)
PINE_SL_DISTANCE = PINE_SL_TICKS * PINE_TICK_SIZE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("NobitexMarginV2")

session = requests.Session()
session.headers.update({
    "User-Agent": "TraderBot/Nobitex-EMA256-Margin-V2",
    "Accept": "application/json",
})

# =========================
# SYMBOL
# =========================
def split_symbol(symbol):
    symbol = symbol.upper()

    known_quotes = ("USDT", "IRT", "RLS", "BTC", "ETH")

    for quote in known_quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)].lower(), quote.lower()

    raise ValueError("Cannot split SYMBOL: " + symbol)


SRC, DST = split_symbol(SYMBOL)


# =========================
# ED25519 AUTH
# =========================
def secret_to_seed(secret):
    raw = secret.strip()

    # API secret is URL-safe base64.
    raw += "=" * (-len(raw) % 4)

    seed = base64.urlsafe_b64decode(raw)

    if len(seed) != 32:
        raise ValueError(
            "NOBITEX_API_SECRET must decode to 32 bytes"
        )

    return seed


def make_signature(timestamp, method, path, body):
    if not API_SECRET:
        raise RuntimeError("NOBITEX_API_SECRET is empty")

    seed = secret_to_seed(API_SECRET)
    signing_key = SigningKey(seed)

    message = (
        str(timestamp)
        + method.upper()
        + path
        + body
    ).encode()

    signature = signing_key.sign(message).signature

    return base64.b64encode(signature).decode()


def auth_headers(method, path, body=""):
    if not API_KEY:
        raise RuntimeError("NOBITEX_API_KEY is empty")

    timestamp = int(time.time() * 1000)

    signature = make_signature(
        timestamp,
        method,
        path,
        body
    )

    return {
        "Nobitex-Key": API_KEY,
        "Nobitex-Timestamp": str(timestamp),
        "Nobitex-Signature": signature,
        "Accept": "application/json",
        "User-Agent": "TraderBot/Nobitex-EMA256-Margin-V2",
    }


# =========================
# API
# =========================

def api_get_public(url, params=None):
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=10
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def api_get(path, params=None, auth=False):

    headers = None

    if auth:
        from urllib.parse import urlencode
        signed_path = path
        if params:
            signed_path += "?" + urlencode(params)
        headers = auth_headers("GET", signed_path, "")

    r = session.get(
        BASE + path,
        params=params,
        headers=headers,
        timeout=20
    )

    r.raise_for_status()

    data = r.json()

    if data.get("status") == "failed":
        raise RuntimeError(str(data))

    return data


def api_post(path, payload):

    body = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False
    )

    headers = auth_headers(
        "POST",
        path,
        body
    )

    headers["Content-Type"] = "application/json"

    r = session.post(
        BASE + path,
        data=body.encode(),
        headers=headers,
        timeout=20
    )

    r.raise_for_status()

    data = r.json()

    if data.get("status") == "failed":
        raise RuntimeError(str(data))

    return data


# =========================
# PUBLIC MARKET DATA
# =========================
_1M_CACHE = {}

def _parse_1m_response(data):
    rows = {}
    if data.get("s") != "ok":
        return rows

    for i, timestamp in enumerate(data["t"]):
        rows[int(timestamp)] = (
            float(data["o"][i]),
            float(data["h"][i]),
            float(data["l"][i]),
            float(data["c"][i]),
            float(data["v"][i])
        )
    return rows


def fetch_1m():
    global _1M_CACHE

    now = int(time.time())
    pages = (HISTORY_1M + 499) // 500

    # Initial load: get enough history for SMA256/Pine calculations.
    if not _1M_CACHE:
        for page in range(1, pages + 1):
            data = api_get(
                "/market/udf/history",
                {
                    "symbol": SYMBOL,
                    "resolution": "1",
                    "to": now,
                    "page": page
                }
            )

            new_rows = _parse_1m_response(data)
            _1M_CACHE.update(new_rows)

            log.info(
                "1m initial page %d/%d -> %d candles",
                page,
                pages,
                len(new_rows)
            )

            time.sleep(0.15)

    else:
        # Subsequent polls: refresh only the newest 500 candles.
        data = api_get(
            "/market/udf/history",
            {
                "symbol": SYMBOL,
                "resolution": "1",
                "to": now,
                "page": 1
            }
        )

        new_rows = _parse_1m_response(data)
        _1M_CACHE.update(new_rows)

        log.info(
            "1m refresh page 1/1 -> %d candles | cache=%d",
            len(new_rows),
            len(_1M_CACHE)
        )

    # Keep enough history for SMA256 plus a safety margin.
    keep = max(HISTORY_1M, 1600)
    keys = sorted(_1M_CACHE)

    if len(keys) > keep:
        for k in keys[:-keep]:
            del _1M_CACHE[k]

    return [
        (timestamp, *_1M_CACHE[timestamp])
        for timestamp in sorted(_1M_CACHE)
    ]

def make_3m(rows):

    buckets = {}

    for row in rows:

        timestamp, o, h, l, c, v = row

        bucket = (timestamp // 180) * 180

        buckets.setdefault(bucket, []).append(row)

    now = int(time.time())

    result = []

    for bucket, group in sorted(buckets.items()):

        if bucket + 180 > now:
            continue

        if len({x[0] for x in group}) < 3:
            continue

        result.append((
            bucket,
            group[0][1],
            max(x[2] for x in group),
            min(x[3] for x in group),
            group[-1][4],
            sum(x[5] for x in group)
        ))

    return result


def make_15m(rows):

    result = []

    for j in range(0, len(rows) - 14, 15):

        group = rows[j:j + 15]

        if len(group) != 15:
            continue

        result.append((
            group[0][0],
            group[0][1],
            max(x[2] for x in group),
            min(x[3] for x in group),
            group[-1][4],
            sum(x[5] for x in group)
        ))

    return result


# =========================
# INDICATORS
# =========================
def ema(values, period):

    if len(values) < period:
        return [None] * len(values)

    alpha = 2 / (period + 1)

    result = [None] * (period - 1)

    e = sum(values[:period]) / period

    result.append(e)

    for value in values[period:]:

        e = alpha * value + (1 - alpha) * e

        result.append(e)

    return result


def rsi(values, period=14):

    if len(values) <= period:
        return [None] * len(values)

    gains = [
        max(values[i] - values[i - 1], 0)
        for i in range(1, len(values))
    ]

    losses = [
        max(values[i - 1] - values[i], 0)
        for i in range(1, len(values))
    ]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * period

    result.append(
        100 if avg_loss == 0
        else 100 - 100 / (1 + avg_gain / avg_loss)
    )

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        result.append(
            100 if avg_loss == 0
            else 100 - 100 / (1 + avg_gain / avg_loss)
        )

    return result


def macd(values):

    fast = ema(values, 12)
    slow = ema(values, 26)

    line = [
        None
        if fast[i] is None or slow[i] is None
        else fast[i] - slow[i]
        for i in range(len(values))
    ]

    valid = [x for x in line if x is not None]

    signal_valid = ema(valid, 9)

    signal = (
        [None] * (len(line) - len(valid))
        + signal_valid
    )

    return line, signal



def calculate_mexc(c3):
    closes = [float(x[4]) for x in c3]
    highs = [float(x[2]) for x in c3]
    lows = [float(x[3]) for x in c3]

    n = len(closes)
    if n < 257:
        return {
            "price": closes[-1] if closes else 0.0,
            "prev": closes[-2] if n >= 2 else 0.0,
            "ma5": None,
            "ma8": None,
            "ma256": None,
            "pma5": None,
            "pma8": None,
            "pma256": None,
            "high": highs[-1] if highs else 0.0,
            "low": lows[-1] if lows else 0.0,
            "lastHigh": None,
            "lastLow": None,
            "signal": None,
            "stop_loss": None,
        }

    def sma(values, period):
        out = [None] * len(values)
        running = 0.0

        for j, value in enumerate(values):
            running += value

            if j >= period:
                running -= values[j - period]

            if j >= period - 1:
                out[j] = running / period

        return out

    ma5 = sma(closes, 5)
    ma8 = sma(closes, 8)
    ma256 = sma(closes, 256)

    # Pine ta.pivothigh(high, 3, 3) / ta.pivotlow(low, 3, 3)
    # A pivot at candidate j is confirmed at bar j+3.
    last_high = None
    last_low = None

    bearish_cross_started = False
    bullish_cross_started = False

    position = 0
    latest_signal = None
    latest_stop = None

    for j in range(1, n):
        # Confirm pivot from 3 bars ago.
        pivot_index = j - 3

        if pivot_index >= 3:
            ph = highs[pivot_index]
            pl = lows[pivot_index]

            if all(
                ph > highs[k]
                for k in range(pivot_index - 3, pivot_index)
            ) and all(
                ph >= highs[k]
                for k in range(pivot_index + 1, pivot_index + 4)
            ):
                last_high = ph

            if all(
                pl < lows[k]
                for k in range(pivot_index - 3, pivot_index)
            ) and all(
                pl <= lows[k]
                for k in range(pivot_index + 1, pivot_index + 4)
            ):
                last_low = pl

        if (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and closes[j - 1] >= ma256[j - 1]
            and closes[j] < ma256[j]
        ) or (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and ma5[j - 1] is not None
            and ma5[j] is not None
            and ma5[j - 1] >= ma256[j - 1]
            and ma5[j] < ma256[j]
        ) or (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and ma8[j - 1] is not None
            and ma8[j] is not None
            and ma8[j - 1] >= ma256[j - 1]
            and ma8[j] < ma256[j]
        ):
            bearish_cross_started = True
            bullish_cross_started = False

        if (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and closes[j - 1] <= ma256[j - 1]
            and closes[j] > ma256[j]
        ) or (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and ma5[j - 1] is not None
            and ma5[j] is not None
            and ma5[j - 1] <= ma256[j - 1]
            and ma5[j] > ma256[j]
        ) or (
            ma256[j] is not None
            and ma256[j - 1] is not None
            and ma8[j - 1] is not None
            and ma8[j] is not None
            and ma8[j - 1] <= ma256[j - 1]
            and ma8[j] > ma256[j]
        ):
            bullish_cross_started = True
            bearish_cross_started = False

        price_down = closes[j] < closes[j - 1]
        ma5_down = ma5[j] is not None and ma5[j - 1] is not None and ma5[j] < ma5[j - 1]
        ma8_down = ma8[j] is not None and ma8[j - 1] is not None and ma8[j] < ma8[j - 1]

        price_up = closes[j] > closes[j - 1]
        ma5_up = ma5[j] is not None and ma5[j - 1] is not None and ma5[j] > ma5[j - 1]
        ma8_up = ma8[j] is not None and ma8[j - 1] is not None and ma8[j] > ma8[j - 1]

        bearish_setup = (
            bearish_cross_started
            and ma256[j] is not None
            and closes[j] < ma256[j]
            and ma5[j] < ma256[j]
            and ma8[j] < ma256[j]
            and price_down
            and ma5_down
            and ma8_down
        )

        bullish_setup = (
            bullish_cross_started
            and ma256[j] is not None
            and closes[j] > ma256[j]
            and ma5[j] > ma256[j]
            and ma8[j] > ma256[j]
            and price_up
            and ma5_up
            and ma8_up
        )

        new_sell = bearish_setup and position != -1
        if new_sell:
            position = -1

            # Only expose a signal when it occurs on the latest
            # closed candle. Historical signals update Pine state
            # but must never be replayed as a new live signal.
            if j == n - 1:
                latest_signal = "sell"
                latest_stop = (
                    last_high + PINE_SL_DISTANCE
                    if last_high is not None
                    else highs[j] + PINE_SL_DISTANCE
                )

            bullish_cross_started = False

        new_buy = bullish_setup and position != 1
        if new_buy:
            position = 1

            # Only expose a signal when it occurs on the latest
            # closed candle. Historical signals update Pine state
            # but must never be replayed as a new live signal.
            if j == n - 1:
                latest_signal = "buy"
                latest_stop = (
                    last_low - PINE_SL_DISTANCE
                    if last_low is not None
                    else lows[j] - PINE_SL_DISTANCE
                )

            bearish_cross_started = False

    i = n - 1
    p = n - 2

    # Only a signal generated on the current/latest closed bar is returned.
    # Historical signals are intentionally not replayed as new signals.
    signal_now = latest_signal
    stop_now = latest_stop

    return {
        "price": closes[i],
        "prev": closes[p],
        "ma5": ma5[i],
        "ma8": ma8[i],
        "ma256": ma256[i],
        "pma5": ma5[p],
        "pma8": ma8[p],
        "pma256": ma256[p],
        "high": highs[i],
        "low": lows[i],
        "lastHigh": last_high,
        "lastLow": last_low,
        "signal": signal_now,
        "stop_loss": stop_now,
    }


def calculate(c3, c15):

    prices = [x[4] for x in c3]
    volumes = [x[5] for x in c3]

    i = len(prices) - 1

    e5 = ema(prices, 5)
    e8 = ema(prices, 8)
    e25 = ema(prices, 25)
    e256 = ema(prices, 256)

    rsi_values = rsi(prices)

    macd_line, macd_signal = macd(prices)

    prices15 = [x[4] for x in c15]

    e15 = ema(prices15, 25)

    return {
        "price": prices[i],
        "prev": prices[i - 1],

        "e5": e5[i],
        "e8": e8[i],
        "e25": e25[i],
        "e256": e256[i],

        "pe5": e5[i - 1],
        "pe8": e8[i - 1],
        "pe256": e256[i - 1],

        "rsi": rsi_values[i],

        "macd": macd_line[i],
        "ms": macd_signal[i],

        "vol": volumes[i],

        "avgvol": (
            sum(volumes[-20:])
            / min(20, len(volumes))
        ),

        "t15up": (
            e15[-1] is not None
            and prices15[-1] > e15[-1]
        ),

        "t15dn": (
            e15[-1] is not None
            and prices15[-1] < e15[-1]
        )
    }


# =========================
# SIGNAL
# =========================
def signal(a):
    return a.get("signal")


# =========================
# MARGIN POSITION
# =========================
def get_active_position():
    data = api_get(
        "/positions/list",
        auth=True
    )

    positions = data.get("positions", [])

    for position in positions:
        src = str(position.get("srcCurrency", "")).upper()
        dst = str(position.get("dstCurrency", "")).upper()
        status = str(position.get("status", "")).lower()

        if (
            src == SRC.upper()
            and dst == DST.upper()
            and status in ("open", "active")
        ):
            return position

    return None

# =========================
# MARGIN LIMIT
# =========================
def get_delegation_limit(side):

    data = api_get(
        "/margin/v2/delegation-limit",
        {
            "market": SYMBOL
        },
        auth=True
    )

    limits = data.get("limits", {})

    side_limits = limits.get(side, [])

    wanted = str(LEVERAGE)

    for item in side_limits:

        if str(item.get("leverage")) == wanted:

            return float(item["limit"])

    raise RuntimeError(
        "No delegation limit for "
        + side
        + " leverage "
        + wanted
    )


# =========================
# OPEN MARGIN ORDER
# =========================
def calculate_amount(price):

    # COLLATERAL is margin in USDT.
    # Position notional = collateral * leverage.
    notional = COLLATERAL * float(LEVERAGE)

    return notional / price


def open_margin(side, price):

    amount = calculate_amount(price)

    limit = get_delegation_limit(side)

    if side == "buy":

        # Buy delegation limit is destination currency (USDT).
        requested_value = amount * price

        if requested_value > limit:
            raise RuntimeError(
                "Buy delegation limit exceeded: "
                f"{requested_value:.4f} > {limit:.4f}"
            )

    else:

        # Sell delegation limit is source currency (HBAR).
        if amount > limit:
            raise RuntimeError(
                "Sell delegation limit exceeded: "
                f"{amount:.4f} > {limit:.4f}"
            )

    payload = {
        "execution": "market",
        "srcCurrency": SRC,
        "dstCurrency": DST,
        "type": side,
        "leverage": str(LEVERAGE),
        "amount": amount,
        "price": price
    }

    log.info(
        "LIVE OPEN REQUEST | %s | amount=%.8f | price=%.8f | leverage=%s",
        side,
        amount,
        price,
        LEVERAGE
    )

    return api_post(
        "/margin/orders/add",
        payload
    )


# =========================
# CLOSE MARGIN POSITION
# =========================
def close_margin(position, price):

    position_id = position.get("id")
    side = str(position.get("side", "")).lower()

    if not position_id:
        raise RuntimeError("Position ID is missing")

    # For both LONG and SHORT positions, the close amount is
    # the position liability / delegated amount in the source currency.
    if side in ("sell", "buy"):
        amount = float(
            position.get("liability")
            or position.get("delegatedAmount")
            or 0
        )
    else:
        raise RuntimeError("Unknown position side: " + side)

    if amount <= 0:
        raise RuntimeError("Invalid close amount")

    path = f"/positions/{position_id}/close"

    payload = {
        "execution": "market",
        "amount": amount,
        "price": price
    }

    log.warning(
        "LIVE CLOSE REQUEST | position=%s | side=%s | amount=%.10f | price=%.8f",
        position_id,
        side,
        amount,
        price
    )

    return api_post(path, payload)


def wait_until_closed(position_id, timeout=30):

    started = time.time()

    while time.time() - started < timeout:

        data = api_get(
            f"/positions/{position_id}/status",
            auth=True
        )

        position = data.get("position")

        if not position:
            return True

        status = str(
            position.get("status", "")
        ).lower()

        if status in (
            "closed",
            "liquidated",
            "expired"
        ):
            return True

        time.sleep(1)

    return False


# =========================
# DRY RUN POSITION
# =========================
def dry_run_open(side, price):

    return {
        "side": side,
        "entry": price
    }


# =========================
# MAIN
# =========================
def main():

    log.info(
        "Nobitex EMA256 Margin V2 | %s | "
        "DRY_RUN=%s | leverage=%s | collateral=%.2f",
        SYMBOL,
        DRY_RUN,
        LEVERAGE,
        COLLATERAL
    )

    if not API_KEY or not API_SECRET:

        raise SystemExit(
            "Set NOBITEX_API_KEY and "
            "NOBITEX_API_SECRET first."
        )

    last_mexc_candle = None

    dry_position = None
    dry_extreme = None
    dry_pine_stop_loss = None

    # LIVE close/reverse state
    pending_close_position_id = None
    pending_close_reason = None
    pending_reverse_side = None

    # LIVE trailing state
    live_extreme = None
    live_trailing_active = False
    live_position_id = None
    live_pine_stop_loss = None

    while True:

        try:

            candles1m = fetch_1m()

            if len(candles1m) == 0:
                time.sleep(POLL)
                continue

            # ==========================================
            # LIVE INTRACANDLE TRAILING
            # ==========================================
            if not DRY_RUN:
                live_position = get_active_position()

                if live_position is not None:
                    live_price = float(candles1m[-1][4])
                    live_side = str(
                        live_position.get("side", "")
                    ).lower()
                    live_id = live_position.get("id")

                    live_entry = float(
                        live_position.get("entryPrice")
                        or live_price
                    )

                    # New position detected
                    if live_id != live_position_id:
                        live_position_id = live_id
                        live_extreme = live_price
                        live_trailing_active = False
                        live_position_needs_pine_sl_restore = True

                    if live_side == "buy":
                        live_extreme = max(
                            live_extreme or live_price,
                            live_price
                        )

                        if live_extreme >= live_entry * (
                            1 + TRAIL_ACTIVATION
                        ):
                            live_trailing_active = True

                        trail_hit = (
                            live_trailing_active
                            and live_price <= live_extreme * (
                                1 - TRAIL
                            )
                        )

                    else:
                        live_extreme = min(
                            live_extreme or live_price,
                            live_price
                        )

                        if live_extreme <= live_entry * (
                            1 - TRAIL_ACTIVATION
                        ):
                            live_trailing_active = True

                        trail_hit = (
                            live_trailing_active
                            and live_price >= live_extreme * (
                                1 + TRAIL
                            )
                        )

                    if trail_hit:
                        log.warning(
                            "LIVE TRAILING CLOSE | "
                            "id=%s side=%s entry=%.8f "
                            "price=%.8f extreme=%.8f",
                            live_id,
                            live_side,
                            live_entry,
                            live_price,
                            live_extreme
                        )

                        close_result = close_margin(
                            live_position,
                            live_price
                        )

                        log.warning(
                            "TRAIL CLOSE RESPONSE: %s",
                            close_result
                        )

                        closed = wait_until_closed(
                            live_id,
                            timeout=30
                        )

                        if closed:
                            log.info(
                                "LIVE TRAILING: "
                                "POSITION CLOSED CONFIRMED"
                            )

                            live_extreme = None
                            live_trailing_active = False
                            live_position_id = None
                            live_pine_stop_loss = None
                        else:
                            log.error(
                                "LIVE TRAILING: "
                                "POSITION %s WAS NOT CONFIRMED CLOSED",
                                live_id
                            )

            # ==========================================
            # NOBITEX CLOSED 3m SIGNAL SOURCE
            # ==========================================
            # Signal data comes from the same Nobitex 1m
            # candles used by the execution/risk layer.
            nobitex_candles3m = make_3m(candles1m)

            if len(nobitex_candles3m) < MIN_3M:
                log.warning(
                    "Nobitex insufficient 3m candles: %d / %d",
                    len(nobitex_candles3m),
                    MIN_3M
                )
                time.sleep(POLL)
                continue

            nobitex_timestamp = nobitex_candles3m[-1][0]

            if nobitex_timestamp == last_mexc_candle:
                time.sleep(POLL)
                continue

            last_mexc_candle = nobitex_timestamp

            a = calculate_mexc(nobitex_candles3m)
            sig = signal(a)

            # Pine SL generated by the latest NEW signal.
            pine_signal_stop = a.get("stop_loss")

            # Execution/live price is also from Nobitex.
            price = float(candles1m[-1][4])

            log.info(
                "NOBITEX CLOSED 3m %s | "
                "CLOSE=%.8f prev=%.8f | "
                "SMA5=%.8f prev=%.8f | "
                "SMA8=%.8f prev=%.8f | "
                "SMA256=%.8f prev=%.8f | "
                "PINE_SL=%.8f | "
                "signal=%s | NOBITEX_EXEC_PRICE=%.8f",
                datetime.fromtimestamp(
                    nobitex_timestamp,
                    timezone.utc
                ).isoformat(),
                a["price"],
                a["prev"],
                a["ma5"],
                a["pma5"],
                a["ma8"],
                a["pma8"],
                a["ma256"],
                a["pma256"],
                float(pine_signal_stop or 0),
                sig,
                price
            )

            # ==================================
            # DRY RUN
            # ==================================
            if DRY_RUN:


                if dry_position:

                    side = dry_position["side"]
                    entry = dry_position["entry"]

                    if side == "buy":

                        dry_extreme = max(
                            dry_extreme,
                            price
                        )

                        reverse = sig == "sell"

                        trailing_active = dry_extreme >= entry * (1 + TRAIL_ACTIVATION)
                        exit_now = (
                            reverse
                            or (dry_pine_stop_loss is not None and price <= dry_pine_stop_loss)
                            or (trailing_active and price <= dry_extreme * (1 - TRAIL))
                        )

                    else:

                        dry_extreme = min(
                            dry_extreme,
                            price
                        )

                        reverse = sig == "buy"

                        trailing_active = dry_extreme <= entry * (1 - TRAIL_ACTIVATION)
                        exit_now = (
                            reverse
                            or (dry_pine_stop_loss is not None and price >= dry_pine_stop_loss)
                            or (trailing_active and price >= dry_extreme * (1 + TRAIL))
                        )

                    if exit_now:

                        log.info(
                            "DRY CLOSE %s | entry=%.8f | exit=%.8f",
                            side,
                            entry,
                            price
                        )

                        dry_position = None
                        dry_extreme = None
                        dry_pine_stop_loss = None

                        if reverse:

                            dry_position = dry_run_open(
                                sig,
                                price
                            )

                            dry_extreme = price
                            dry_pine_stop_loss = pine_signal_stop

                            log.info(
                                "DRY REVERSE -> OPEN %s @ %.8f | PINE_SL=%s",
                                sig,
                                price,
                                dry_pine_stop_loss
                            )

                elif sig:

                    dry_position = dry_run_open(
                        sig,
                        price
                    )

                    dry_extreme = price
                    dry_pine_stop_loss = pine_signal_stop

                    log.info(
                        "DRY OPEN %s @ %.8f | PINE_SL=%s",
                        sig,
                        price,
                        dry_pine_stop_loss
                    )

                continue

            # ==================================
            # LIVE MODE
            # ==================================
            position = get_active_position()


            if position is None:

                if sig:

                    log.info(
                        "LIVE: flat -> OPEN %s",
                        sig
                    )

                    result = open_margin(
                        sig,
                        price
                    )
                    live_pine_stop_loss = pine_signal_stop
                    live_position_id = None
                    live_extreme = price
                    live_trailing_active = False

                    log.info(
                        "OPEN RESPONSE: %s",
                        result
                    )

            else:

                current_side = str(
                    position.get("side", "")
                ).lower()

                position_id = position.get("id")

                log.info(
                    "LIVE POSITION | id=%s | side=%s | "
                    "entry=%s | pnl=%s",
                    position_id,
                    current_side,
                    position.get("entryPrice"),
                    position.get("unrealizedPNL")
                )

                # ----------------------------------
                # LIVE STOP / TAKE PROFIT
                # ----------------------------------
                if live_pine_stop_loss is None:
                    if current_side == "buy":
                        live_pine_stop_loss = float(a["lastLow"]) - PINE_SL_DISTANCE if a.get("lastLow") is not None else float(a["low"]) - PINE_SL_DISTANCE
                    elif current_side == "sell":
                        live_pine_stop_loss = float(a["lastHigh"]) + PINE_SL_DISTANCE if a.get("lastHigh") is not None else float(a["high"]) + PINE_SL_DISTANCE
                    log.warning("PINE SL RESTORED | position=%s | side=%s | SL=%s", position_id, current_side, live_pine_stop_loss)

                entry_price = float(
                    position.get("entryPrice") or price
                )

                if current_side == "buy":
                    stop_hit = (live_pine_stop_loss is not None and price <= live_pine_stop_loss)
                    take_hit = False
                else:
                    stop_hit = (live_pine_stop_loss is not None and price >= live_pine_stop_loss)
                    take_hit = False

                reverse_signal = (
                    sig
                    and sig != current_side
                )

                should_close = (
                    stop_hit
                    or take_hit
                    or reverse_signal
                )

                if should_close:
                    reason = (
                        "REVERSE"
                        if reverse_signal
                        else "STOP"
                        if stop_hit
                        else "TAKE_PROFIT"
                    )

                    # Prevent repeated CLOSE requests for the same position.
                    if pending_close_position_id == position_id:
                        log.warning(
                            "CLOSE ALREADY PENDING | position=%s | reason=%s",
                            position_id,
                            pending_close_reason
                        )

                        still_open = get_active_position()

                        if still_open is None:
                            log.info(
                                "PENDING CLOSE CONFIRMED | position=%s",
                                position_id
                            )

                            pending_close_position_id = None
                            pending_close_reason = None

                            if pending_reverse_side:
                                reverse_side = pending_reverse_side
                                pending_reverse_side = None

                                log.warning(
                                    "OPENING REVERSE %s AFTER CONFIRMED CLOSE",
                                    reverse_side
                                )

                                open_result = open_margin(
                                    reverse_side,
                                    price
                                )
                                live_pine_stop_loss = pine_signal_stop
                                live_position_id = None
                                live_extreme = price
                                live_trailing_active = False

                                log.warning(
                                    "REVERSE OPEN RESPONSE: %s",
                                    open_result
                                )
                        else:
                            live_pine_stop_loss = None
                            log.warning(
                                "POSITION %s STILL OPEN | NO DUPLICATE CLOSE",
                                position_id
                            )

                    else:
                        log.warning(
                            "LIVE CLOSE | reason=%s | position=%s | side=%s",
                            reason,
                            position_id,
                            current_side
                        )

                        pending_close_position_id = position_id
                        pending_close_reason = reason
                        pending_reverse_side = (
                            sig if reverse_signal else None
                        )

                        close_result = close_margin(
                            position,
                            price
                        )

                        log.warning(
                            "CLOSE RESPONSE: %s",
                            close_result
                        )

                        closed = wait_until_closed(
                            position_id,
                            timeout=30
                        )

                        if closed:
                            log.info(
                                "POSITION %s CLOSED CONFIRMED",
                                position_id
                            )

                            pending_close_position_id = None
                            pending_close_reason = None

                            if pending_reverse_side:
                                reverse_side = pending_reverse_side
                                pending_reverse_side = None

                                log.warning(
                                    "POSITION %s CLOSED. "
                                    "OPENING REVERSE %s",
                                    position_id,
                                    reverse_side
                                )

                                open_result = open_margin(
                                    reverse_side,
                                    price
                                )
                                live_pine_stop_loss = pine_signal_stop
                                live_position_id = None
                                live_extreme = price
                                live_trailing_active = False

                                log.warning(
                                    "REVERSE OPEN RESPONSE: %s",
                                    open_result
                                )
                        else:
                            log.error(
                                "POSITION %s WAS NOT CONFIRMED CLOSED. "
                                "NO REVERSE ORDER WILL BE SENT.",
                                position_id
                            )

        except KeyboardInterrupt:

            log.info("Stopped.")

            return

        except Exception as exc:

            log.exception(
                "Loop error: %s",
                exc
            )

        time.sleep(POLL)


if __name__ == "__main__":
    main()
