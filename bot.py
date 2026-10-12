from __future__ import annotations

import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import numpy as np
import pandas as pd
import requests

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    QueryOrderStatus,
)
from alpaca.trading.requests import GetOrdersRequest


# ============================================================
# 1. KONFIGURATION
# ============================================================

API_KEY = "PKNPBIXR7TIVOYWUONFKDOEGF4"
SECRET_KEY = "9JqMriM1LHLsae3MppsBY97PHmWMhHEnHpQBzMvEgFeC"
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1559227552218878103/RmDOyen4Bc3UNhnIKxRMtCKNbdtk13t3iTEfx21wtMHBwkQW951EmRwmPO2U1nN9dIg9"

PAPER_TRADING = True
TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT"]

# Strategieparameter
RSI_PERIOD = 14
RSI_ENTRY_MAX = 35
RSI_EXIT_MIN = 70

EMA_FAST = 20
EMA_SLOW = 50

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.5   # Moderater Stop-Loss über ATR
ATR_TARGET_MULTIPLIER = 3.0 # Gewinnziel (3x Volatilität)

# Risikomanagement
RISK_PER_TRADE = 0.01        # 1 % des Eigenkapitals pro Trade
MAX_POSITION_FRACTION = 0.25  # Max 25 % des Kapitals je Aktie
MAX_TOTAL_EXPOSURE = 0.80     # Max 80 % insgesamt investiert
MAX_OPEN_POSITIONS = 4
MAX_DAILY_LOSS = 0.03         # 3 % Tagesverlust-Limit
MIN_ORDER_VALUE = 50.0
MAX_DATA_AGE_MINUTES = 5

POLL_SECONDS = 60
DATA_LIMIT = 250
STATE_FILE = Path("trading_bot_state.json")

REQUIRE_MARKET_OPEN = True


# ============================================================
# 2. LOGGING UND CLIENTS
# ============================================================

logger = logging.getLogger("smart_bot")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

file_handler = RotatingFileHandler(
    "trading_bot.log",
    maxBytes=2_000_000,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

trading_client = TradingClient(
    API_KEY,
    SECRET_KEY,
    paper=PAPER_TRADING,
)

data_client = StockHistoricalDataClient(
    API_KEY,
    SECRET_KEY,
)

http = requests.Session()


def notify(message: str) -> None:
    logger.info(message)

    if not DISCORD_WEBHOOK_URL:
        return

    try:
        response = http.post(
            DISCORD_WEBHOOK_URL,
            json={"content": f"🤖 **Smart Trading Bot**\n{message}"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Discord-Fehler: %s", exc)


# ============================================================
# 3. ZUSTAND UND TAGESRISIKO
# ============================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Zustand konnte nicht geladen werden: %s", exc)
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def get_account_metrics():
    account = trading_client.get_account()

    equity = float(account.equity)
    buying_power = float(account.buying_power)

    if equity <= 0:
        raise RuntimeError("Ungültiger Eigenkapitalwert.")

    return account, equity, buying_power


def update_daily_risk(equity: float) -> tuple[bool, float]:
    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()

    if state.get("date") != today:
        state = {
            "date": today,
            "starting_equity": equity,
            "halted": False,
            "halt_reason": "",
            "entry_prices": {},
            "high_watermarks": {},
            "last_processed_bar": {},
        }
        save_state(state)

    starting_equity = float(state.get("starting_equity", equity))

    if starting_equity <= 0:
        return False, 0.0

    daily_return = (equity - starting_equity) / starting_equity

    if daily_return <= -MAX_DAILY_LOSS:
        state["halted"] = True
        state["halt_reason"] = "Tagesverlustlimit erreicht"
        save_state(state)

    return not state.get("halted", False), daily_return


# ============================================================
# 4. MARKTDATEN
# ============================================================

def fetch_bars(symbol: str) -> pd.DataFrame | None:
    try:
        now = datetime.now(timezone.utc)

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=DATA_LIMIT * 3),
            end=now - timedelta(minutes=1),
            limit=DATA_LIMIT,
        )

        result = data_client.get_stock_bars(request)
        bars = result.df

        if bars.empty:
            logger.warning("%s: Keine Kerzen erhalten.", symbol)
            return None

        if isinstance(bars.index, pd.MultiIndex):
            if symbol not in bars.index.get_level_values(0):
                return None
            df = bars.xs(symbol, level=0).copy()
        else:
            df = bars.copy()

        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]

        if len(df) < EMA_SLOW + 5:
            logger.warning("%s: Zu wenige historische Kerzen.", symbol)
            return None

        latest_time = df.index[-1]

        if latest_time.tzinfo is None:
            latest_time = latest_time.tz_localize("UTC")

        age = datetime.now(timezone.utc) - latest_time.to_pydatetime()

        if age > timedelta(minutes=MAX_DATA_AGE_MINUTES):
            logger.warning("%s: Veraltete Marktdaten.", symbol)
            return None

        required = ["open", "high", "low", "close", "volume"]

        if df[required].isna().any().any() or (df["close"] <= 0).any():
            return None

        return df

    except Exception:
        logger.exception("%s: Fehler beim Laden der Marktdaten.", symbol)
        return None


# ============================================================
# 5. TECHNISCHE INDIKATOREN
# ============================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    df.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0
    df.loc[(avg_gain == 0) & (avg_loss == 0), "rsi"] = 50

    # Trend
    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    # ATR
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = true_range.ewm(alpha=1 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()

    return df


# ============================================================
# 6. SIGNALBEWERTUNG
# ============================================================

def evaluate_signal(df: pd.DataFrame) -> tuple[bool, str, float]:
    if df is None or len(df) < EMA_SLOW + 5:
        return False, "Zu wenige Daten", 0.0

    previous = df.iloc[-2]
    latest = df.iloc[-1]

    required = ["rsi", "ema_fast", "ema_slow", "atr"]

    if any(pd.isna(latest[key]) for key in required):
        return False, "Indikatoren noch nicht verfügbar", 0.0

    price = float(latest["close"])
    atr = float(latest["atr"])

    if price <= 0 or atr <= 0:
        return False, "Ungültiger Preis oder ATR", 0.0

    # RSI Erholung aus überverkauftem Bereich
    rsi_recovery = (
        float(previous["rsi"]) < RSI_ENTRY_MAX
        and float(latest["rsi"]) > float(previous["rsi"])
    )

    # Schneller Trend zeigt nach oben
    uptrend = float(latest["ema_fast"]) > float(latest["ema_slow"])

    buy = rsi_recovery and uptrend

    reason = f"RSI={latest['rsi']:.1f} | Trend={uptrend} | ATR={atr:.2f}"

    return buy, reason, atr


# ============================================================
# 7. ORDER- UND PORTFOLIO-SCHUTZ
# ============================================================

def get_positions() -> dict:
    positions = {}
    for position in trading_client.get_all_positions():
        positions[position.symbol] = position
    return positions


def get_open_order_symbols() -> set[str]:
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    orders = trading_client.get_orders(filter=request)
    return {order.symbol for order in orders if getattr(order, "symbol", None)}


def market_is_open() -> bool:
    try:
        clock = trading_client.get_clock()
        return bool(clock.is_open)
    except Exception:
        logger.exception("Marktstatus konnte nicht ermittelt werden.")
        return False


def calculate_quantity(
    equity: float,
    buying_power: float,
    price: float,
    atr: float,
    positions: dict,
) -> int:
    if price <= 0 or atr <= 0 or equity <= 0:
        return 0

    stop_distance = ATR_STOP_MULTIPLIER * atr
    if stop_distance <= 0:
        return 0

    risk_budget = equity * RISK_PER_TRADE
    qty_by_risk = int(risk_budget / stop_distance)

    current_exposure = sum(
        abs(float(position.market_value)) for position in positions.values()
    )

    available_exposure = max(0.0, equity * MAX_TOTAL_EXPOSURE - current_exposure)
    max_position_value = equity * MAX_POSITION_FRACTION
    allowed_value = min(available_exposure, max_position_value, buying_power)

    qty_by_capital = int(allowed_value / price)
    qty = min(qty_by_risk, qty_by_capital)

    if qty < 1 or qty * price < MIN_ORDER_VALUE:
        return 0

    return qty


def submit_buy(
    symbol: str,
    qty: int,
    price: float,
    atr: float,
    bar_time: str,
) -> None:
    state = load_state()
    state.setdefault("pending_entries", {})
    state["pending_entries"][symbol] = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "bar_time": bar_time,
        "qty": qty,
    }
    save_state(state)

    try:
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        submitted = trading_client.submit_order(order_data=order)

        state = load_state()
        state.setdefault("orders", {})
        state["orders"][symbol] = {
            "order_id": str(submitted.id),
            "stop": round(price - ATR_STOP_MULTIPLIER * atr, 4),
            "target": round(price + ATR_TARGET_MULTIPLIER * atr, 4),
            "signal_price": price,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)

        notify(
            f"🟢 BUY {symbol}: {qty} Stück angefordert. "
            f"Preis ${price:.2f} | Stop ${price - ATR_STOP_MULTIPLIER * atr:.2f} | "
            f"Ziel ${price + ATR_TARGET_MULTIPLIER * atr:.2f}."
        )

    except Exception:
        logger.exception("%s: Kauf fehlgeschlagen.", symbol)


def close_position(symbol: str, qty: float, reason: str) -> None:
    try:
        trading_client.close_position(symbol)
        notify(f"🔴 SCHLIESSEN {symbol}: {reason}.")
    except Exception:
        logger.exception("%s: Schließung fehlgeschlagen.", symbol)


# ============================================================
# 8. POSITIONEN ÜBERWACHEN
# ============================================================

def manage_positions(positions: dict) -> None:
    state = load_state()
    orders = state.setdefault("orders", {})

    for symbol, position in positions.items():
        try:
            qty = float(position.qty)
            avg_price = float(position.avg_entry_price)
            current_price = float(position.current_price)

            if qty <= 0 or avg_price <= 0 or current_price <= 0:
                continue

            levels = orders.get(symbol, {})

            if not levels:
                # Fallback für Positionen ohne gespeichertes ATR-Level
                stop = avg_price * 0.98   # 2% Stop Loss
                target = avg_price * 1.04 # 4% Take Profit
            else:
                stop = float(levels["stop"])
                target = float(levels["target"])

            pl_pct = (current_price - avg_price) / avg_price

            logger.info(
                "%s | Menge %.2f | Einstieg %.2f | Jetzt %.2f | P/L %+.2f%%",
                symbol, qty, avg_price, current_price, pl_pct * 100,
            )

            if current_price <= stop:
                close_position(symbol, qty, f"STOP-LOSS ERREICHT ({pl_pct*100:.2f}%)")
                orders.pop(symbol, None)

            elif current_price >= target:
                close_position(symbol, qty, f"TAKE-PROFIT ERREICHT (+{pl_pct*100:.2f}%)")
                orders.pop(symbol, None)

        except Exception:
            logger.exception("%s: Fehler bei Positionsüberwachung.", symbol)

    state["orders"] = orders
    save_state(state)


# ============================================================
# 9. HAUPTSCHLEIFE
# ============================================================

def run_cycle() -> None:
    account, equity, buying_power = get_account_metrics()
    risk_allowed, daily_return = update_daily_risk(equity)

    logger.info(
        "Eigenkapital=$%.2f | Kaufkraft=$%.2f | Tagesrendite=%+.2f%%",
        equity, buying_power, daily_return * 100,
    )

    positions = get_positions()
    manage_positions(positions)

    if not risk_allowed:
        notify(f"🛑 Tagesverlustlimit aktiv ({daily_return:.2%}). Keine neuen Käufe.")
        return

    if REQUIRE_MARKET_OPEN and not market_is_open():
        logger.info("US-Markt geschlossen. Keine neuen Käufe.")
        return

    open_symbols = get_open_order_symbols()
    positions = get_positions()

    state = load_state()
    last_processed = state.setdefault("last_processed_bar", {})

    for symbol in TICKERS:
        try:
            if symbol in positions or symbol in open_symbols:
                continue

            if len(positions) >= MAX_OPEN_POSITIONS:
                break

            df = fetch_bars(symbol)
            if df is None:
                continue

            df = calculate_indicators(df)
            latest = df.iloc[-1]
            bar_time = df.index[-1].isoformat()

            if last_processed.get(symbol) == bar_time:
                continue

            last_processed[symbol] = bar_time
            save_state(state)

            buy, reason, atr = evaluate_signal(df)

            logger.info("%s | Preis %.2f | RSI %.2f | BUY=%s", symbol, float(latest["close"]), float(latest["rsi"]), buy)

            if not buy:
                continue

            _, equity, buying_power = get_account_metrics()
            positions = get_positions()

            qty = calculate_quantity(equity, buying_power, float(latest["close"]), atr, positions)

            if qty < 1:
                continue

            submit_buy(symbol, qty, float(latest["close"]), atr, bar_time)

        except Exception:
            logger.exception("%s: Fehler im Zyklus.", symbol)


def main() -> None:
    notify(f"Bot gestartet. Watchlist={', '.join(TICKERS)}")

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            notify("Bot manuell gestoppt.")
            break
        except Exception:
            logger.exception("Unerwarteter Fehler im Hauptzyklus.")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
