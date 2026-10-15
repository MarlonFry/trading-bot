"""
Smart Trading Bot v5.0 Aggressive Power-Trend (Alpaca, 8 Ticker, Dynamic %-Sizing)
Entworfen für hohes Renditepotenzial (Zinseszins durch 2.5% Risiko pro Trade).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import (
        OrderClass, OrderSide, QueryOrderStatus, TimeInForce
    )
    from alpaca.trading.requests import (
        GetOrdersRequest, MarketOrderRequest, ReplaceOrderRequest,
        StopLossRequest, TakeProfitRequest,
    )
    ALPACA_OK = True
except ImportError as exc:
    raise SystemExit(
        "Fehlende Abhängigkeiten. Bitte installieren mit: pip install alpaca-py pandas numpy requests python-dotenv"
    ) from exc


# ============================================================
# 1. KONFIGURATION & CREDENTIALS
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8661524937:AAF8MQkKVggFYDiFQrYTyjxQLG4zLBVam2Q"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "8859741833"

ALPACA_KEY = os.getenv("ALPACA_API_KEY") or "PKNPBIXR7TIVOYWUONFKDOEGF4"
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY") or "9JqMriM1LHLsae3MppsBY97PHmWMhHEnHpQBzMvEgFeC"

TICKERS = tuple(x.strip().upper() for x in os.getenv(
    "BOT_TICKERS", "AAPL,NVDA,TSLA,MSFT,AMD,AMZN,META,GOOGL"
).split(",") if x.strip())
REGIME_SYMBOL = os.getenv("BOT_REGIME_SYMBOL", "SPY").upper()

PAPER = os.getenv("BOT_PAPER", "1").strip() != "0"
DATA_FEED_NAME = os.getenv("BOT_DATA_FEED", "iex").lower()
BAR_MINUTES = 15
TF_INTRADAY = TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute)

# Aggressive Indikatoren & Ausstieg
RSI_PERIOD = 14
RSI_ENTRY_MAX = 52
RSI_ENTRY_CEIL = 60
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
VOL_LOOKBACK = 20
MIN_REL_VOLUME = 1.0            # Hohe Volumenschwelle: Nur echte Ausbrüche
MIN_ATR_PCT = 0.0015
MAX_ATR_PCT = 0.05

D_EMA_FAST = 20
D_EMA_SLOW = 50
D_SMA_TREND = 200
REGIME_EMA = 50

SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60
ENTRY_AFTER_OPEN_MIN = 30
ENTRY_BEFORE_CLOSE_MIN = 30
NY_TZ = "America/New_York"
NY = ZoneInfo(NY_TZ)

ATR_STOP_MULTIPLIER = 2.0
ATR_TARGET_MULTIPLIER = 4.0     # Optimaler R-Multiple für starke Trends
BREAKEVEN_R = 1.0
TRAIL_START_R = 1.8
TRAIL_ATR_MULTIPLIER = 1.0
MAX_HOLD_DAYS = 5
COOLDOWN_MINUTES = 20

# DYNAMISCHES PROZENT-RISIKO (High-Reward Sizing)
RISK_PCT_PER_TRADE = 0.025      # 2.5% des Gesamtkontos pro Trade riskieren
MAX_POSITION_FRACTION = 0.95
MAX_TOTAL_EXPOSURE = 0.95
MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS = 0.10
MAX_DRAWDOWN = 0.25
MIN_ORDER_VALUE = 10.0
LOSS_STREAK_THROTTLE = 3
LOSS_STREAK_RISK_FACTOR = 0.5

POLL_SECONDS = 60
POLL_SECONDS_CLOSED = 300
MAX_BAR_LAG_MINUTES = 30
INTRADAY_LOOKBACK_DAYS = 12
DAILY_LOOKBACK_DAYS = 430
MIN_BARS = EMA_SLOW + VOL_LOOKBACK + 5

STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "smart_bot_state.json"))
LOG_FILE = os.getenv("BOT_LOG_FILE", "smart_bot.log")
SLIPPAGE_BPS = 5.0
STARTING_CAPITAL = 100_000.0


# ============================================================
# 2. LOGGING & TELEGRAM-PROXIES
# ============================================================

logger = logging.getLogger("smart_bot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ny_now() -> datetime:
    return datetime.now(NY)


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def notify(message: str) -> None:
    logger.info("%s", message)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"🤖 **Smart Trading Bot**\n{message}",
        "parse_mode": "Markdown",
    }
    
    proxies = {
        "http": "http://proxy.server:3128",
        "https": "http://proxy.server:3128",
    }
    
    for attempt in range(5):
        try:
            response = requests.post(url, json=payload, proxies=proxies, timeout=15)
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            if attempt < 4:
                time.sleep(3 * (attempt + 1))
            else:
                logger.error("Telegram-Fehler nach 5 Versuchen: %s", exc)


# ============================================================
# 3. STATE-MANAGEMENT
# ============================================================

class BotState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "day": {"date": "", "base_equity": 0.0, "halted": False},
            "peak_equity": 0.0,
            "kill_switch": False,
            "kill_reason": "",
            "trades": {},
            "stats": {
                "wins": 0, "losses": 0, "consecutive_losses": 0,
                "total_r": 0.0, "recent_results": []
            },
            "last_bar": {},
            "cooldown": {},
            "summary_date": "",
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if key in self.data and isinstance(value, dict) and isinstance(self.data[key], dict):
                        self.data[key].update(value)
                    else:
                        self.data[key] = value
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception("Fehler beim Laden des State-Files: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def trades(self) -> dict[str, Any]:
        return self.data["trades"]

    @property
    def stats(self) -> dict[str, Any]:
        return self.data["stats"]

    def risk_factor(self) -> float:
        if self.stats["consecutive_losses"] >= LOSS_STREAK_THROTTLE:
            return LOSS_STREAK_RISK_FACTOR
        return 1.0

    def record_result(self, r_multiple: float) -> None:
        stats = self.stats
        stats["total_r"] = round(float(stats["total_r"]) + r_multiple, 3)
        recent = stats.setdefault("recent_results", [])
        recent.append(float(r_multiple))
        del recent[:-10]
        if r_multiple < -0.05:
            stats["losses"] += 1
            stats["consecutive_losses"] += 1
        elif r_multiple > 0.05:
            stats["wins"] += 1
            stats["consecutive_losses"] = 0

    def update_risk(self, equity: float, last_equity: float | None) -> tuple[float, float]:
        today = ny_now().date().isoformat()
        day = self.data["day"]
        if day.get("date") != today:
            base = last_equity if last_equity and last_equity > 0 else equity
            self.data["day"] = {"date": today, "base_equity": base, "halted": False}
            day = self.data["day"]

        base_equity = float(day.get("base_equity") or equity)
        daily_return = equity / base_equity - 1 if base_equity > 0 else 0.0
        if daily_return <= -MAX_DAILY_LOSS and not day.get("halted", False):
            day["halted"] = True
            notify(f"🛑 **Tagesverlustlimit erreicht** ({daily_return:.2%}). Keine neuen Einstiege heute.")

        self.data["peak_equity"] = max(float(self.data["peak_equity"]), equity)
        peak = float(self.data["peak_equity"])
        drawdown = equity / peak - 1 if peak > 0 else 0.0
        if drawdown <= -MAX_DRAWDOWN and not self.data["kill_switch"]:
            self.data["kill_switch"] = True
            self.data["kill_reason"] = f"Drawdown {drawdown:.2%}"
            notify(f"🚨 **KILL-SWITCH AKTIVIERT**: Max. Drawdown {drawdown:.2%} erreicht.")
        self.save()
        return daily_return, drawdown


state = BotState(STATE_FILE)
trading_client: TradingClient | None = None
data_client: StockHistoricalDataClient | None = None


def init_clients() -> None:
    global trading_client, data_client
    trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER)
    data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    account = trading_client.get_account()
    logger.info("Alpaca verbunden. Paper=%s, Status=%s", PAPER, account.status)


# ============================================================
# 4. MARKTDATEN & INDIKATOREN
# ============================================================

def fetch_bars(symbols: list[str] | tuple[str, ...], timeframe: Any, start: datetime) -> dict[str, pd.DataFrame]:
    if not symbols:
        return {}
    assert data_client is not None
    feed = DataFeed.SIP if DATA_FEED_NAME == "sip" else DataFeed.IEX
    request = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=timeframe,
        start=start,
        adjustment=Adjustment.ALL,
        feed=feed,
    )
    raw = data_client.get_stock_bars(request).df
    if raw is None or raw.empty:
        return {}

    output: dict[str, pd.DataFrame] = {}
    required = ["open", "high", "low", "close", "volume"]
    for symbol in symbols:
        try:
            frame = raw.xs(symbol, level=0).copy() if isinstance(raw.index, pd.MultiIndex) else raw.copy()
        except KeyError:
            continue
        frame = frame.sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = frame.dropna(subset=required)
        frame = frame[(frame["close"] > 0) & (frame["volume"] >= 0)]
        if not frame.empty:
            output[symbol] = frame
    return output


def regular_hours(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    ny_idx = idx.tz_convert(NY_TZ)
    minutes = ny_idx.hour * 60 + ny_idx.minute
    return frame.loc[(minutes >= SESSION_OPEN_MIN) & (minutes < SESSION_CLOSE_MIN)].copy()


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

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

    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False, min_periods=EMA_FAST).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False, min_periods=EMA_SLOW).mean()

    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low, (high - previous_close).abs(), (low - previous_close).abs()
    ], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
    avg_volume = volume.rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean()
    df["rel_vol"] = volume / avg_volume.replace(0, np.nan)

    typical_price = (high + low + close) / 3.0
    session_key = pd.Series(pd.DatetimeIndex(df.index).tz_convert(NY_TZ).date, index=df.index)
    pv = typical_price * volume
    df["vwap"] = pv.groupby(session_key).cumsum() / volume.groupby(session_key).cumsum().replace(0, np.nan)
    return df


def daily_context(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(columns=["day", "trend_ok", "regime_ok"])
    d = daily.sort_index().copy()
    idx = pd.DatetimeIndex(d.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    days = idx.tz_convert(NY_TZ).tz_localize(None).normalize()
    close = d["close"]
    ema_fast = close.ewm(span=D_EMA_FAST, adjust=False, min_periods=D_EMA_FAST).mean()
    ema_slow = close.ewm(span=D_EMA_SLOW, adjust=False, min_periods=D_EMA_SLOW).mean()
    sma = close.rolling(D_SMA_TREND, min_periods=D_SMA_TREND).mean()
    regime_ema = close.ewm(span=REGIME_EMA, adjust=False, min_periods=REGIME_EMA).mean()
    result = pd.DataFrame({
        "day": days.values.astype("datetime64[ns]"),
        "trend_ok": ((close > sma) & (ema_fast > ema_slow)).to_numpy(),
        "regime_ok": (close > regime_ema).to_numpy(),
    })
    return result.drop_duplicates("day", keep="last").sort_values("day").reset_index(drop=True)


def attach_context(index: pd.Index, context: pd.DataFrame | None, column: str) -> np.ndarray:
    if context is None or context.empty:
        return np.zeros(len(index), dtype=bool)
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    days = idx.tz_convert(NY_TZ).tz_localize(None).normalize()
    left = pd.DataFrame({"day": days.values.astype("datetime64[ns]")})
    right = context[["day", column]].copy()
    right["day"] = pd.to_datetime(right["day"]).astype("datetime64[ns]")
    merged = pd.merge_asof(
        left.sort_values("day"), right.sort_values("day"),
        on="day", direction="backward", allow_exact_matches=False
    )
    return merged[column].eq(True).to_numpy()


def build_signals(
    intraday: pd.DataFrame,
    symbol_context: pd.DataFrame | None,
    regime_context: pd.DataFrame | None,
) -> pd.DataFrame:
    df = add_indicators(intraday)
    df["trend_ok"] = attach_context(df.index, symbol_context, "trend_ok")
    df["regime_ok"] = attach_context(df.index, regime_context, "regime_ok")

    idx = pd.DatetimeIndex(df.index).tz_convert(NY_TZ)
    bar_end_minutes = idx.hour * 60 + idx.minute + BAR_MINUTES
    time_ok = (
        (bar_end_minutes >= SESSION_OPEN_MIN + ENTRY_AFTER_OPEN_MIN)
        & (bar_end_minutes <= SESSION_CLOSE_MIN - ENTRY_BEFORE_CLOSE_MIN)
    )

    rsi_previous = df["rsi"].shift(1)
    setup = (
        (rsi_previous < RSI_ENTRY_MAX)
        & (df["rsi"] > rsi_previous)
        & (df["rsi"] < RSI_ENTRY_CEIL)
    )
    trend_ok = df["ema_fast"] > df["ema_slow"]
    bullish_bar = df["close"] > df["open"]
    volume_ok = df["rel_vol"] >= MIN_REL_VOLUME
    atr_pct = df["atr"] / df["close"]
    volatility_ok = (atr_pct >= MIN_ATR_PCT) & (atr_pct <= MAX_ATR_PCT)
    vwap_ok = df["close"] > df["vwap"]

    df["signal"] = (
        setup & trend_ok & bullish_bar & volume_ok & volatility_ok & vwap_ok
        & df["trend_ok"] & df["regime_ok"] & time_ok
    ).fillna(False)

    depth = ((RSI_ENTRY_MAX - rsi_previous) / RSI_ENTRY_MAX).clip(0, 1)
    trend_strength = ((df["ema_fast"] - df["ema_slow"]) / df["atr"].replace(0, np.nan)).clip(0, 2) / 2
    volume_strength = (df["rel_vol"] / 2).clip(0, 1)
    df["score"] = (50 * depth + 30 * trend_strength + 20 * volume_strength).fillna(0.0)
    return df


# ============================================================
# 5. DYNAMISCHES PROZENT-SIZING (ZINSESZINS)
# ============================================================

def position_size(
    equity: float,
    cash: float,
    price: float,
    atr: float,
    current_exposure: float,
    risk_factor: float,
    score: float,
) -> int:
    """
    Berechnet die Stückzahl dynamisch auf Basis von 2.5% des Konto-Gesamtkapitals.
    """
    values = [equity, cash, price, atr, risk_factor]
    if not all(math.isfinite(v) for v in values) or min(equity, cash, price, atr) <= 0:
        return 0

    stop_distance = ATR_STOP_MULTIPLIER * atr
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return 0

    # Dynamisches Prozentrisiko (2.5% von Equity)
    target_risk = equity * RISK_PCT_PER_TRADE * risk_factor
    qty_by_risk = int(target_risk / stop_distance)

    exposure_budget = max(0.0, equity * MAX_TOTAL_EXPOSURE - current_exposure)
    position_cap = equity * MAX_POSITION_FRACTION
    spend_budget = min(cash, exposure_budget, position_cap)
    qty_by_cash = int(spend_budget / price)

    qty = min(qty_by_risk, qty_by_cash)
    if qty < 1 or qty * price < MIN_ORDER_VALUE:
        return 0
    return qty


# ============================================================
# 6. BROKER & ORDER MANAGEMENT
# ============================================================

def require_clients() -> tuple[TradingClient, StockHistoricalDataClient]:
    if trading_client is None or data_client is None:
        raise RuntimeError("Clients sind nicht initialisiert.")
    return trading_client, data_client


def get_positions() -> dict[str, Any]:
    tc, _ = require_clients()
    return {p.symbol: p for p in tc.get_all_positions()}


def get_open_orders() -> list[Any]:
    tc, _ = require_clients()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)
    result = []
    seen: set[str] = set()
    for order in tc.get_orders(filter=request):
        for item in [order] + list(getattr(order, "legs", None) or []):
            order_id = str(getattr(item, "id", ""))
            if order_id and order_id not in seen:
                seen.add(order_id)
                result.append(item)
    return result


def find_stop_order(open_orders: list[Any], symbol: str) -> Any | None:
    for order in open_orders:
        if getattr(order, "symbol", None) == symbol and enum_value(getattr(order, "side", "")) == "sell":
            if enum_value(getattr(order, "order_type", getattr(order, "type", ""))) in ("stop", "stop_limit", "trailing_stop"):
                return order
    return None


def submit_entry(symbol: str, qty: int, price: float, atr: float, bar_time: pd.Timestamp, score: float) -> bool:
    tc, _ = require_clients()
    stop = round(price - ATR_STOP_MULTIPLIER * atr, 2)
    target = round(price + ATR_TARGET_MULTIPLIER * atr, 2)
    if qty <= 0 or stop <= 0 or stop >= price or target <= price:
        return False

    client_id = f"smartv50-{symbol}-{int(bar_time.timestamp())}"[:48]
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target),
        stop_loss=StopLossRequest(stop_price=stop),
        client_order_id=client_id,
    )
    try:
        submitted = tc.submit_order(order_data=request)
    except Exception:
        logger.exception("%s: Order-Übermittlung fehlgeschlagen.", symbol)
        return False

    kauf_zeit = ny_now().strftime("%d.%m.%Y um %H:%M Uhr")
    max_risk_val = abs(price - stop) * qty

    state.trades[symbol] = {
        "order_id": str(submitted.id),
        "client_order_id": client_id,
        "status": "pending",
        "qty": qty,
        "entry_estimate": price,
        "entry": price,
        "stop": stop,
        "initial_stop": stop,
        "target": target,
        "atr": atr,
        "risk_per_share": price - stop,
        "submitted_at": utc_now().isoformat(),
        "signal_bar": bar_time.isoformat(),
        "score": float(score),
    }
    state.save()

    notify(
        f"🟢 **GEKAUFT: {symbol}**\n"
        f"🕒 **Zeitpunkt:** {kauf_zeit}\n"
        f"• **Score:** {score:.0f}/100\n"
        f"• **Menge:** {qty} Stück\n"
        f"• **Kaufpreis:** ${price:.2f}\n"
        f"• **Stop-Loss:** ${stop:.2f}\n"
        f"• **Take-Profit:** ${target:.2f}\n"
        f"• **Max-Risiko:** ${max_risk_val:.2f} (2.5% Equity)"
    )
    return True


def cancel_symbol_orders(symbol: str, open_orders: list[Any]) -> None:
    tc, _ = require_clients()
    for order in open_orders:
        if getattr(order, "symbol", None) == symbol:
            try:
                tc.cancel_order_by_id(order.id)
            except Exception:
                pass


def flatten(symbol: str, reason: str, open_orders: list[Any], last_price: float | None = None) -> None:
    tc, _ = require_clients()
    try:
        cancel_symbol_orders(symbol, open_orders)
        time.sleep(1.0)
        tc.close_position(symbol)
        trade = state.trades.get(symbol)
        if trade:
            trade["exit_reason"] = reason
            trade["exit_requested_at"] = utc_now().isoformat()
            state.save()
        notify(f"🔴 **VERKAUFT: {symbol}**\n• **Grund:** {reason}")
    except Exception:
        logger.exception("%s: Schließen fehlgeschlagen.", symbol)


def modify_stop(symbol: str, new_stop: float, open_orders: list[Any]) -> bool:
    tc, _ = require_clients()
    order = find_stop_order(open_orders, symbol)
    if order is None:
        return False
    try:
        tc.replace_order_by_id(order.id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))
        return True
    except Exception:
        return False


# ============================================================
# 7. POSITIONS-MONITORING
# ============================================================

def reconcile_trades(positions: dict[str, Any], open_orders: list[Any]) -> None:
    tc, _ = require_clients()
    symbols_with_orders = {getattr(o, "symbol", None) for o in open_orders}
    now = utc_now()

    for symbol, trade in list(state.trades.items()):
        if symbol in positions or symbol in symbols_with_orders:
            continue
        try:
            submitted_at = datetime.fromisoformat(trade["submitted_at"])
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        except Exception:
            submitted_at = now
        if (now - submitted_at).total_seconds() < 120:
            continue

        entry = float(trade.get("entry", trade.get("entry_estimate", 0)) or 0)
        exit_price = None
        filled = False
        try:
            parent = tc.get_order_by_id(trade["order_id"])
            filled = enum_value(parent.status) == "filled"
            if getattr(parent, "filled_avg_price", None):
                entry = float(parent.filled_avg_price)
            for leg in getattr(parent, "legs", None) or []:
                if enum_value(leg.status) == "filled" and getattr(leg, "filled_avg_price", None):
                    exit_price = float(leg.filled_avg_price)
        except Exception:
            continue

        state.trades.pop(symbol, None)
        if not filled:
            state.save()
            continue

        state.data["cooldown"][symbol] = now.isoformat()
        risk = float(trade.get("risk_per_share", 0) or 0)
        qty = int(trade.get("qty", 0) or 0)
        verkauf_zeit = ny_now().strftime("%d.%m.%Y um %H:%M Uhr")

        if exit_price and entry > 0 and risk > 0 and qty > 0:
            r_multiple = (exit_price - entry) / risk
            pnl = (exit_price - entry) * qty
            state.record_result(r_multiple)

            if pnl >= 0:
                header = f"🟢 **VERKAUFT MIT GEWINN: {symbol}**"
                pnl_str = f"🟩 **Gewinn:** +${pnl:.2f} (+{r_multiple:.2f}R)"
            else:
                header = f"🔴 **VERKAUFT MIT VERLUST: {symbol}**"
                pnl_str = f"🟥 **Verlust:** -${abs(pnl):.2f} ({r_multiple:.2f}R)"

            notify(
                f"{header}\n"
                f"🕒 **Zeitpunkt:** {verkauf_zeit}\n"
                f"• **Kauf:** ${entry:.2f}\n"
                f"• **Verkauf:** ${exit_price:.2f}\n"
                f"• {pnl_str}"
            )
        state.save()


def manage_positions(positions: dict[str, Any], open_orders: list[Any]) -> None:
    now = utc_now()
    for symbol, position in positions.items():
        trade = state.trades.get(symbol)
        if not trade:
            continue
        try:
            price = float(position.current_price)
            entry = float(position.avg_entry_price)
            if price <= 0 or entry <= 0:
                continue
            if trade.get("status") == "pending":
                trade["status"] = "open"
                trade["entry"] = entry
                trade["risk_per_share"] = max(entry - float(trade["initial_stop"]), 0.01)
                state.save()

            risk = max(float(trade.get("risk_per_share", 0.01)), 0.01)
            stop = float(trade["stop"])
            r_multiple = (price - entry) / risk

            submitted_at = datetime.fromisoformat(trade["submitted_at"])
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            age_days = (now - submitted_at).total_seconds() / 86400

            if age_days > MAX_HOLD_DAYS:
                flatten(symbol, f"Zeit-Exit nach {age_days:.1f} Tagen ({r_multiple:+.2f}R)", open_orders, price)
                continue

            desired = stop
            if r_multiple >= BREAKEVEN_R:
                desired = max(desired, entry + 0.1 * risk)
            if r_multiple >= TRAIL_START_R:
                desired = max(desired, price - TRAIL_ATR_MULTIPLIER * float(trade["atr"]))

            desired = round(min(desired, price - 0.02), 2)
            if desired >= stop + 0.01:
                if modify_stop(symbol, desired, open_orders):
                    trade["stop"] = desired
                    state.save()
                    notify(f"🔼 **{symbol}**: Stop auf ${desired:.2f} nachgezogen ({r_multiple:+.2f}R).")
        except Exception:
            logger.exception("%s: Fehler bei Positionsverwaltung.", symbol)


# ============================================================
# 8. EINSTIEGSSUCHE & ANPASSUNG
# ============================================================

_daily_cache: dict[str, Any] = {"date": None, "contexts": {}}

def get_daily_contexts() -> dict[str, pd.DataFrame]:
    today = ny_now().date().isoformat()
    if _daily_cache["date"] == today and _daily_cache["contexts"]:
        return _daily_cache["contexts"]
    start = utc_now() - timedelta(days=DAILY_LOOKBACK_DAYS)
    bars = fetch_bars(list(TICKERS) + [REGIME_SYMBOL], TimeFrame.Day, start)
    contexts = {symbol: daily_context(frame) for symbol, frame in bars.items()}
    _daily_cache.update(date=today, contexts=contexts)
    return contexts


def scan_for_entries(account: Any, positions: dict[str, Any], open_orders: list[Any]) -> None:
    equity = float(account.equity)
    cash = float(account.cash)
    occupied = set(positions) | {getattr(o, "symbol", None) for o in open_orders}
    occupied.discard(None)
    slots = MAX_OPEN_POSITIONS - len(occupied)
    if slots <= 0:
        return

    contexts = get_daily_contexts()
    regime_context = contexts.get(REGIME_SYMBOL)
    if regime_context is None or regime_context.empty:
        return

    now = utc_now()
    candidates_symbols = [symbol for symbol in TICKERS if symbol not in occupied]
    bars = fetch_bars(candidates_symbols, TF_INTRADAY, now - timedelta(days=INTRADAY_LOOKBACK_DAYS))
    candidates: list[tuple[float, str, float, float, pd.Timestamp]] = []

    for symbol in candidates_symbols:
        try:
            cooldown = state.data["cooldown"].get(symbol)
            if cooldown:
                cooldown_dt = datetime.fromisoformat(cooldown)
                if cooldown_dt.tzinfo is None:
                    cooldown_dt = cooldown_dt.replace(tzinfo=timezone.utc)
                if now - cooldown_dt < timedelta(minutes=COOLDOWN_MINUTES):
                    continue

            frame = bars.get(symbol)
            if frame is None:
                continue
            frame = regular_hours(frame)
            frame = frame[frame.index + timedelta(minutes=BAR_MINUTES) <= now]
            if len(frame) < MIN_BARS:
                continue
            last_bar_end = (frame.index[-1] + timedelta(minutes=BAR_MINUTES)).to_pydatetime()
            if last_bar_end.tzinfo is None:
                last_bar_end = last_bar_end.replace(tzinfo=timezone.utc)
            if now - last_bar_end > timedelta(minutes=MAX_BAR_LAG_MINUTES):
                continue

            signals = build_signals(frame, contexts.get(symbol), regime_context)
            bar_time = signals.index[-1]
            bar_key = bar_time.isoformat()
            if state.data["last_bar"].get(symbol) == bar_key:
                continue
            state.data["last_bar"][symbol] = bar_key
            state.save()

            last = signals.iloc[-1]
            if bool(last["signal"]):
                candidates.append((
                    float(last["score"]), symbol, float(last["close"]),
                    float(last["atr"]), bar_time
                ))
        except Exception:
            pass

    exposure = sum(abs(float(p.market_value)) for p in positions.values())

    for score, symbol, price, atr, bar_time in sorted(candidates, reverse=True):
        if slots <= 0:
            break
        qty = position_size(
            equity=equity, cash=cash, price=price, atr=atr,
            current_exposure=exposure, risk_factor=state.risk_factor(), score=score
        )
        if qty < 1:
            continue
        if submit_entry(symbol, qty, price, atr, bar_time, score):
            slots -= 1
            cost = qty * price
            exposure += cost
            cash -= cost


# ============================================================
# 9. EXAKTER TELEGRAM-TAGESBERICHT
# ============================================================

def maybe_daily_summary(account: Any, daily_return: float, drawdown: float) -> None:
    now = ny_now()
    today = now.date().isoformat()
    if now.weekday() < 5 and now.hour >= 16 and state.data["summary_date"] != today:
        equity = float(account.equity)
        base_equity = float(state.data["day"].get("base_equity") or equity)
        pnl = equity - base_equity

        if pnl >= 0:
            profit_str = f"🟩 **Tagesprofit:** +${pnl:,.2f}"
            loss_line = "ff"
            loss_str = "🟥 **Tagesverlust:** $0.00"
        else:
            profit_str = "🟩 **Tagesprofit:** $0.00"
            loss_str = f"🟥 **Tagesverlust:** -${abs(pnl):,.2f}"

        stats = state.stats
        notify(
            f"📊 **Tagesbericht**\n"
            f"• **Kontostand:** ${equity:,.2f}\n"
            f"• {profit_str}\n"
            f"• {loss_str}\n"
            f"• **Max. Rücksetzer:** {drawdown:.2%}\n"
            f"• **Trades Gesamt:** {stats['wins']}W / {stats['losses']}L\n"
            f"• **Performance:** {stats['total_r']:+.1f}R"
        )
        state.data["summary_date"] = today
        state.save()


def run_cycle() -> int:
    tc, _ = require_clients()
    clock = tc.get_clock()
    account = tc.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity) if getattr(account, "last_equity", None) else None
    daily_return, drawdown = state.update_risk(equity, last_equity)

    positions = get_positions()
    orders = get_open_orders()
    reconcile_trades(positions, orders)
    positions = get_positions()
    orders = get_open_orders()
    manage_positions(positions, orders)

    if not clock.is_open:
        maybe_daily_summary(account, daily_return, drawdown)
        return POLL_SECONDS_CLOSED

    minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    if state.data["kill_switch"] or state.data["day"].get("halted") or minutes_to_close < ENTRY_BEFORE_CLOSE_MIN:
        return POLL_SECONDS

    scan_for_entries(account, get_positions(), get_open_orders())
    return POLL_SECONDS


def live_main() -> None:
    init_clients()
    notify(f"🚀 **Bot gestartet** (v5.0 Aggressive %-Sizing)\nBeobachte: {', '.join(TICKERS)}")
    consecutive_errors = 0
    while True:
        sleep_for = POLL_SECONDS
        try:
            sleep_for = run_cycle()
            consecutive_errors = 0
        except KeyboardInterrupt:
            notify("⏹️ **Bot manuell gestoppt**.")
            return
        except Exception:
            consecutive_errors += 1
            logger.exception("Fehler in Hauptschleife.")
            sleep_for = min(POLL_SECONDS * (2 ** min(consecutive_errors, 4)), 600)
        time.sleep(sleep_for)


# ============================================================
# 10. BACKTEST
# ============================================================

def simulate(
    frames: dict[str, pd.DataFrame],
    capital: float = STARTING_CAPITAL,
    slippage_bps: float = SLIPPAGE_BPS,
) -> dict[str, Any]:
    if not frames:
        return {"equity": pd.Series(dtype=float), "trades": pd.DataFrame(), "n_trades": 0}

    slip = slippage_bps / 10_000
    rows: dict[str, dict[pd.Timestamp, tuple]] = {}
    for symbol, frame in frames.items():
        rows[symbol] = {}
        for ts, row in frame.iterrows():
            rows[symbol][ts] = (
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["atr"]), bool(row["signal"]),
                float(row["score"])
            )

    timestamps = sorted(set().union(*(set(frame.index) for frame in frames.values())))
    cash = float(capital)
    positions: dict[str, dict[str, Any]] = {}
    last_close: dict[str, float] = {}
    last_exit: dict[str, pd.Timestamp] = {}
    pending: list[tuple] = []
    trades: list[dict[str, Any]] = []
    curve: list[tuple[pd.Timestamp, float]] = []
    consecutive_losses = 0
    current_day = None
    day_start_equity = capital
    halted = False

    for ts in timestamps:
        for symbol in frames:
            row = rows[symbol].get(ts)
            if row:
                last_close[symbol] = row[3]

        day = ts.tz_convert(NY_TZ).date() if ts.tzinfo else ts.date()
        if day != current_day:
            current_day = day
            day_start_equity = curve[-1][1] if curve else capital
            halted = False

        for signal_ts, score, symbol, signal_close, signal_atr in sorted(pending, key=lambda item: -item[1]):
            row = rows[symbol].get(ts)
            if row is None or symbol in positions or len(positions) >= MAX_OPEN_POSITIONS or halted:
                continue
            if ts <= signal_ts or ts - signal_ts > timedelta(minutes=2 * BAR_MINUTES):
                continue
            if symbol in last_exit and ts - last_exit[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                continue

            entry = row[0] * (1 + slip)
            stop = signal_close - ATR_STOP_MULTIPLIER * signal_atr
            target = signal_close + ATR_TARGET_MULTIPLIER * signal_atr
            if entry <= stop + 0.01 or target <= entry:
                continue

            equity_now = cash + sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in positions.items())
            exposure = sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in positions.items())
            risk_factor = LOSS_STREAK_RISK_FACTOR if consecutive_losses >= LOSS_STREAK_THROTTLE else 1.0
            qty = position_size(
                equity_now, cash, entry, signal_atr, exposure, risk_factor, score
            )
            if qty < 1:
                continue
            cash -= qty * entry
            positions[symbol] = {
                "qty": qty, "entry": entry, "stop": stop, "target": target,
                "atr": signal_atr, "risk": max(entry - stop, 0.01), "entry_ts": ts
            }
        pending = []

        for symbol, position in list(positions.items()):
            row = rows[symbol].get(ts)
            if row is None:
                continue
            o, high, low, close = row[:4]
            exit_price = None
            if low <= position["stop"]:
                exit_price = min(o, position["stop"]) * (1 - slip)
            elif high >= position["target"]:
                exit_price = max(o, position["target"])
            else:
                r_mult = (close - position["entry"]) / position["risk"]
                if r_mult >= BREAKEVEN_R:
                    position["stop"] = max(position["stop"], position["entry"] + 0.1 * position["risk"])
                if r_mult >= TRAIL_START_R:
                    position["stop"] = max(position["stop"], close - TRAIL_ATR_MULTIPLIER * position["atr"])
                if ts - position["entry_ts"] > timedelta(days=MAX_HOLD_DAYS):
                    exit_price = close * (1 - slip)

            if exit_price is not None:
                qty = position["qty"]
                cash += qty * exit_price
                pnl = (exit_price - position["entry"]) * qty
                r_multiple = (exit_price - position["entry"]) / position["risk"]
                trades.append({
                    "symbol": symbol, "entry_ts": position["entry_ts"], "exit_ts": ts,
                    "r": r_multiple, "pnl": pnl
                })
                if r_multiple < -0.05:
                    consecutive_losses += 1
                elif r_multiple > 0.05:
                    consecutive_losses = 0
                last_exit[symbol] = ts
                del positions[symbol]

        equity = cash + sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in positions.items())
        if day_start_equity > 0 and equity / day_start_equity - 1 <= -MAX_DAILY_LOSS:
            halted = True
        curve.append((ts, equity))

        for symbol in frames:
            row = rows[symbol].get(ts)
            if row and row[5] and symbol not in positions:
                pending.append((ts, row[6], symbol, row[3], row[4]))

    equity_curve = pd.Series([value for _, value in curve], index=[ts for ts, _ in curve])
    trade_frame = pd.DataFrame(trades)
    result: dict[str, Any] = {
        "equity": equity_curve, "trades": trade_frame,
        "open_positions": len(positions), "n_trades": len(trade_frame)
    }
    if not equity_curve.empty:
        result["total_return"] = equity_curve.iloc[-1] / capital - 1
        result["max_drawdown"] = (equity_curve / equity_curve.cummax() - 1).min()
    if not trade_frame.empty:
        wins = trade_frame.loc[trade_frame["pnl"] > 0, "pnl"]
        losses = trade_frame.loc[trade_frame["pnl"] <= 0, "pnl"]
        result["win_rate"] = len(wins) / len(trade_frame)
        result["avg_r"] = float(trade_frame["r"].mean())
    return result


def run_backtest(days: int) -> None:
    init_clients()
    now = utc_now()
    intraday = fetch_bars(list(TICKERS), TF_INTRADAY, now - timedelta(days=days + 20))
    daily = fetch_bars(list(TICKERS) + [REGIME_SYMBOL], TimeFrame.Day,
                       now - timedelta(days=days + DAILY_LOOKBACK_DAYS))
    if REGIME_SYMBOL not in daily:
        raise SystemExit("Keine SPY Tagesdaten verfügbar.")
    regime_context = daily_context(daily[REGIME_SYMBOL])
    cutoff = now - timedelta(days=days)
    frames = {}
    for symbol in TICKERS:
        if symbol not in intraday or symbol not in daily:
            continue
        regular = regular_hours(intraday[symbol])
        signals = build_signals(regular, daily_context(daily[symbol]), regime_context)
        signals = signals.loc[signals.index >= cutoff]
        if not signals.empty:
            frames[symbol] = signals
    if not frames:
        raise SystemExit("Keine Daten für Backtest verfügbar.")

    result = simulate(frames)
    print("\n=========== BACKTEST v5.0 Aggressive Power-Trend ===========")
    print(f"Trades: {result['n_trades']}")
    if result["n_trades"]:
        print(f"Trefferquote: {result.get('win_rate', 0):.1%} | Ø Ergebnis: {result.get('avg_r', 0):+.2f}R | Rendite: {result.get('total_return', 0):+.2%} | Max. Drawdown: {result.get('max_drawdown', 0):.2%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Trading Bot v5.0")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    if args.backtest:
        run_backtest(args.days)
    else:
        live_main()


if __name__ == "__main__":
    main()
