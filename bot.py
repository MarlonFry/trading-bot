"""
Smart Trading Bot v9.1 High-Precision Compounder (24 Top-Ticker, 1H)
Strenge v7.1-Filter (RSI < 52, RelVol >= 1.1, TP 4.5R, SL 1.5R), gleichmäßiges 3.5% Risiko, 1.0x Cash-Compounding.
Einstiegsfenster: 15:45 Uhr bis 21:30 Uhr deutscher Zeit + 15-Minuten Live-Heartbeat.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
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
# 1. KONFIGURATION & API CREDENTIALS
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8661524937:AAF8MQkKVggFYDiFQrYTyjxQLG4zLBVam2Q"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "8859741833"

ALPACA_KEY = os.getenv("ALPACA_API_KEY") or "PKNPBIXR7TIVOYWUONFKDOEGF4"
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY") or "9JqMriM1LHLsae3MppsBY97PHmWMhHEnHpQBzMvEgFeC"

# 24 kuratierte Trend-Aktien (16 Basis-Ticker + bewährte Sektor-Leader wie NOW, LLY, CRWD, PANW)
DEFAULT_TICKERS = (
    "AAPL,NVDA,TSLA,MSFT,AMD,AMZN,META,GOOGL,NFLX,AVGO,PLTR,SMCI,ARM,COIN,MSTR,QQQ,"
    "NOW,LLY,CRWD,PANW,UBER,ORCL,ANET,MU"
)
TICKERS = tuple(dict.fromkeys(
    x.strip().upper() for x in os.getenv("BOT_TICKERS", DEFAULT_TICKERS).split(",") if x.strip()
))
REGIME_SYMBOL = os.getenv("BOT_REGIME_SYMBOL", "SPY").upper()

PAPER = os.getenv("BOT_PAPER", "1").strip() != "0"
DATA_FEED_NAME = os.getenv("BOT_DATA_FEED", "iex").lower()

BAR_MINUTES = 60
TF_INTRADAY = TimeFrame(1, TimeFrameUnit.Hour)

# Strenge v7.1 Qualitäts-Filter
RSI_PERIOD = 14
RSI_ENTRY_MAX = 52
RSI_ENTRY_CEIL = 62
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
VOL_LOOKBACK = 20
MIN_REL_VOLUME = 1.1
MIN_ATR_PCT = 0.002
MAX_ATR_PCT = 0.05

D_EMA_FAST = 20
D_EMA_SLOW = 50
D_SMA_TREND = 200
REGIME_EMA = 50

SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60
ENTRY_AFTER_OPEN_MIN = 15       # Start ab 15:45 Uhr deutscher Zeit
ENTRY_BEFORE_CLOSE_MIN = 30     # Letzter Einstieg 21:30 Uhr deutscher Zeit
NY_TZ = "America/New_York"
NY = ZoneInfo(NY_TZ)

# V7.1 GEWINNER-PARAMETER
ATR_STOP_MULTIPLIER = 1.5       # Enger Stop
ATR_TARGET_MULTIPLIER = 4.5     # 1 : 3 Chance-Risiko-Verhältnis
BREAKEVEN_R = 1.0               # Breakeven ab +1.0R
TRAIL_START_R = 1.8             # Trailing-Stop ab +1.8R
TRAIL_ATR_MULTIPLIER = 1.0
MAX_HOLD_DAYS = 5
COOLDOWN_MINUTES = 60

# GLEICHMÄSSIGES 3.5% COMPOUNDING (OHNE MARGIN-ZINSEN)
RISK_PCT_PER_TRADE = 0.035      # 3.5% festes Risiko pro Trade
MAX_POSITION_FRACTION = 0.95
MAX_TOTAL_EXPOSURE = 0.95       # Reines Cash-Compounding ohne Margin-Kosten
MAX_OPEN_POSITIONS = 5          # Max. 5 parallele Positionen
MAX_DAILY_LOSS = 0.10
MAX_DRAWDOWN = 0.25
MIN_ORDER_VALUE = 10.0
LOSS_STREAK_THROTTLE = 3
LOSS_STREAK_RISK_FACTOR = 0.5

POLL_SECONDS = 120
POLL_SECONDS_CLOSED = 300
MAX_BAR_LAG_MINUTES = 120
INTRADAY_LOOKBACK_DAYS = 30
DAILY_LOOKBACK_DAYS = 430
MIN_BARS = EMA_SLOW + VOL_LOOKBACK + 5

STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "smart_bot_state.json"))
LOG_FILE = os.getenv("BOT_LOG_FILE", "smart_bot.log")
SLIPPAGE_BPS = 5.0
STARTING_CAPITAL = 100_000.0


# ============================================================
# 2. LOGGING & TELEGRAM
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
# 3. STATE MANAGEMENT
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
            "last_heartbeat": "",
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
    symbol_list = list(symbols)
    if not symbol_list:
        return {}
    assert data_client is not None
    feed = DataFeed.SIP if DATA_FEED_NAME == "sip" else DataFeed.IEX

    output: dict[str, pd.DataFrame] = {}
    required = ["open", "high", "low", "close", "volume"]

    for i in range(0, len(symbol_list), 16):
        batch = symbol_list[i:i + 16]
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=timeframe,
            start=start,
            adjustment=Adjustment.ALL,
            feed=feed,
        )
        raw = data_client.get_stock_bars(request).df
        if raw is None or raw.empty:
            continue

        for symbol in batch:
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
        "regime_ok": ((close > sma) & (close > regime_ema)).to_numpy(),
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
# 5. POSITION SIZING (GLEICHMÄSSIGES RISIKO PRO TRADE)
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
    values = [equity, cash, price, atr, risk_factor]
    if not all(math.isfinite(v) for v in values) or min(equity, cash, price, atr) <= 0:
        return 0

    stop_distance = ATR_STOP_MULTIPLIER * atr
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return 0

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
# 6. ORDERS & BROKER
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

    client_id = f"smartv91-{symbol}-{int(bar_time.timestamp())}"[:48]
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
        f"🟢 **GEKAUFT: {symbol}** (1H-Compounder v9.1)\n"
        f"🕒 **Zeitpunkt:** {kauf_zeit}\n"
        f"• **Score:** {score:.0f}/100\n"
        f"• **Menge:** {qty} Stück (${qty * price:,.2f})\n"
        f"• **Kaufpreis:** ${price:.2f}\n"
        f"• **Stop-Loss:** ${stop:.2f}\n"
        f"• **Take-Profit:** ${target:.2f} (4.5R)\n"
