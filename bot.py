"""
Smart Trading Bot v3.1 (Alpaca, VWAP, Dynamisches Risiko, Runner-Stop, Optimierte Parameter)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
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
    from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import (
        GetOrdersRequest,
        MarketOrderRequest,
        ReplaceOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )
    ALPACA_OK = True
    ALPACA_ERR: Exception | None = None
except ImportError as _exc:
    ALPACA_OK = False
    ALPACA_ERR = _exc


# ============================================================
# 1. KONFIGURATION & TELEGRAM KEYS
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8661524937:AAF8MQkKVggFYDiFQrYTyjxQLG4zLBVam2Q"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "8859741833"

TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT"]
REGIME_SYMBOL = "SPY"

BAR_MINUTES = 15
TF_INTRADAY = TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute) if ALPACA_OK else None
DATA_FEED_NAME = os.getenv("BOT_DATA_FEED", "iex").lower()

# --- Optimierte Einstiegsstrategie ---
RSI_PERIOD = 14
RSI_ENTRY_MAX = 48             # Auf 48 erhöht für frühere & mehr Einstiege
RSI_ENTRY_CEIL = 55
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
VOL_LOOKBACK = 20
MIN_REL_VOLUME = 0.7
MIN_ATR_PCT = 0.0015
MAX_ATR_PCT = 0.03

# --- Tageskontext ---
D_EMA_FAST = 20
D_EMA_SLOW = 50
D_SMA_TREND = 200
REGIME_EMA = 50

# --- Handelszeiten (US-Börsenzeit) ---
SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
ENTRY_AFTER_OPEN_MIN = 30
ENTRY_BEFORE_CLOSE_MIN = 30

# --- Ausstieg ---
ATR_STOP_MULTIPLIER = 2.0      # Auf 2.0 erhöht für mehr Puffer bei Volatilität
ATR_TARGET_MULTIPLIER = 4.5
BREAKEVEN_R = 1.0
TRAIL_START_R = 2.0
MAX_HOLD_DAYS = 5
COOLDOWN_MINUTES = 60

# --- Risikomanagement ---
RISK_PER_TRADE = 0.01
MAX_POSITION_FRACTION = 0.25
MAX_TOTAL_EXPOSURE = 0.80
MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
LOSS_STREAK_THROTTLE = 3
LOSS_STREAK_RISK_FACTOR = 0.5
MIN_ORDER_VALUE = 50.0

POLL_SECONDS = 60
POLL_SECONDS_CLOSED = 300
MAX_BAR_LAG_MINUTES = 30
INTRADAY_LOOKBACK_DAYS = 12
DAILY_LOOKBACK_DAYS = 430
MIN_BARS = EMA_SLOW + VOL_LOOKBACK
STATE_FILE = Path("smart_bot_state.json")
LOG_FILE = "smart_bot.log"

STARTING_CAPITAL = 100_000.0
SLIPPAGE_BPS = 5.0

NY_TZ = "America/New_York"
NY = ZoneInfo(NY_TZ)


# ============================================================
# 2. LOGGING, CLIENTS & BENACHRICHTIGUNG
# ============================================================

logger = logging.getLogger("smart_bot")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
if not logger.handlers:
    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    logger.addHandler(_console)
    _file = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _file.setFormatter(_fmt)
    logger.addHandler(_file)

trading_client = None
data_client = None
DRY_RUN = False
http = requests.Session()


def notify(message: str) -> None:
    logger.info(message)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"🤖 **Smart Trading Bot**\n{message}",
        "parse_mode": "Markdown",
    }
    for attempt in range(3):
        try:
            r = http.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2)
            else:
                logger.error("Telegram-Fehler nach 3 Versuchen: %s", exc)


def init_clients() -> None:
    global trading_client, data_client
    if not ALPACA_OK:
        sys.exit(f"alpaca-py fehlt: {ALPACA_ERR}")

    key = os.getenv("ALPACA_API_KEY") or "PKNPBIXR7TIVOYWUONFKDOEGF4"
    secret = os.getenv("ALPACA_SECRET_KEY") or "9JqMriM1LHLsae3MppsBY97PHmWMhHEnHpQBzMvEgFeC"

    if not key or not secret:
        sys.exit("ALPACA Keys fehlen.")
    paper = os.getenv("BOT_PAPER", "1") != "0"
    trading_client = TradingClient(key, secret, paper=paper)
    data_client = StockHistoricalDataClient(key, secret)


def _val(x) -> str:
    return str(getattr(x, "value", x)).lower()

def ny_now() -> datetime:
    return datetime.now(NY)


# ============================================================
# 3. ZUSTAND & DYNAMISCHES RISIKO
# ============================================================

class BotState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "day": {},
            "peak_equity": 0.0,
            "kill_switch": False,
            "kill_reason": "",
            "trades": {},
            "stats": {"wins": 0, "losses": 0, "consecutive_losses": 0, "total_r": 0.0, "recent_results": []},
            "last_bar": {},
            "cooldown": {},
            "summary_date": "",
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            if "recent_results" not in self.data["stats"]:
                self.data["stats"]["recent_results"] = []
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def trades(self) -> dict:
        return self.data["trades"]

    @property
    def stats(self) -> dict:
        return self.data["stats"]

    def risk_factor(self) -> float:
        factor = 1.0
        if self.stats["consecutive_losses"] >= LOSS_STREAK_THROTTLE:
            return LOSS_STREAK_RISK_FACTOR
        
        rec = self.stats.get("recent_results", [])
        if len(rec) >= 5:
            win_rate = sum(1 for r in rec if r > 0) / len(rec)
            if win_rate >= 0.6:
                factor = 1.2
            elif win_rate <= 0.3:
                factor = 0.8
        return factor

    def record_result(self, r_multiple: float) -> None:
        s = self.stats
        s["total_r"] = round(s["total_r"] + r_multiple, 3)
        rec = s.setdefault("recent_results", [])
        rec.append(r_multiple)
        if len(rec) > 10:
            rec.pop(0)

        if r_multiple < -0.05:
            s["losses"] += 1
            s["consecutive_losses"] += 1
        elif r_multiple > 0.05:
            s["wins"] += 1
            s["consecutive_losses"] = 0

    def update_risk(self, equity: float, last_equity: float | None) -> tuple[float, float]:
        today = ny_now().date().isoformat()
        day = self.data["day"]
        if day.get("date") != today:
            base = last_equity if last_equity and last_equity > 0 else equity
            self.data["day"] = day = {"date": today, "base_equity": base, "halted": False}
        daily_ret = equity / day["base_equity"] - 1

        if daily_ret <= -MAX_DAILY_LOSS and not day["halted"]:
            day["halted"] = True
            notify(f"🛑 **Tagesverlustlimit erreicht** ({daily_ret:.2%}).")

        self.data["peak_equity"] = max(self.data["peak_equity"], equity)
        drawdown = equity / self.data["peak_equity"] - 1
        if drawdown <= -MAX_DRAWDOWN and not self.data["kill_switch"]:
            self.data["kill_switch"] = True
            notify(f"🚨 **KILL-SWITCH AKTIVIERT**: Drawdown {drawdown:.2%}.")
        self.save()
        return daily_ret, drawdown

state = BotState(STATE_FILE)


# ============================================================
# 4. INDIKATOREN & VWAP
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi[(avg_loss == 0) & (avg_gain > 0)] = 100
    rsi[(avg_gain == 0) & (avg_loss > 0)] = 0
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50
    df["rsi"] = rsi

    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()

    df["rel_vol"] = vol / vol.rolling(VOL_LOOKBACK).mean()
    
    days = pd.Series(df.index.date, index=df.index)
    df["vwap"] = (close * vol).groupby(days).cumsum() / vol.groupby(days).cumsum()
    
    return df


def regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    ny = pd.DatetimeIndex(df.index).tz_convert(NY_TZ)
    start_min = ny.hour * 60 + ny.minute
    return df[(start_min >= SESSION_OPEN) & (start_min < SESSION_CLOSE)]


def daily_context(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.sort_index()
    idx = pd.DatetimeIndex(d.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    days = idx.tz_convert(NY_TZ).tz_localize(None).normalize()

    close = d["close"]
    ema_f = close.ewm(span=D_EMA_FAST, adjust=False).mean()
    ema_s = close.ewm(span=D_EMA_SLOW, adjust=False).mean()
    sma = close.rolling(D_SMA_TREND).mean()
    out = pd.DataFrame(
        {
            "day": days.values.astype("datetime64[ns]"),
            "trend_ok": ((close > sma) & (ema_f > ema_s)).values,
            "regime_ok": (close > close.ewm(span=REGIME_EMA, adjust=False).mean()).values,
        }
    )
    return out.drop_duplicates("day", keep="last").sort_values("day").reset_index(drop=True)


def attach_context(index: pd.Index, ctx: pd.DataFrame | None, col: str) -> np.ndarray:
    if ctx is None or ctx.empty:
        return np.zeros(len(index), dtype=bool)
    days = pd.DatetimeIndex(index).tz_convert(NY_TZ).tz_localize(None).normalize()
    left = pd.DataFrame({"day": days.values.astype("datetime64[ns]")})
    right = ctx[["day", col]].copy()
    right["day"] = right["day"].astype("datetime64[ns]")
    merged = pd.merge_asof(left, right, on="day", direction="backward", allow_exact_matches=False)
    return merged[col].eq(True).to_numpy()


def build_signals(df15: pd.DataFrame, ctx_symbol: pd.DataFrame | None, ctx_regime: pd.DataFrame | None) -> pd.DataFrame:
    df = add_indicators(df15)
    df["trend_ok"] = attach_context(df.index, ctx_symbol, "trend_ok")
    df["regime_ok"] = attach_context(df.index, ctx_regime, "regime_ok")

    ny = pd.DatetimeIndex(df.index).tz_convert(NY_TZ)
    end_min = ny.hour * 60 + ny.minute + BAR_MINUTES
    time_ok = (end_min >= SESSION_OPEN + ENTRY_AFTER_OPEN_MIN) & (end_min <= SESSION_CLOSE - ENTRY_BEFORE_CLOSE_MIN)

    rsi_prev = df["rsi"].shift(1)
    setup = (rsi_prev < RSI_ENTRY_MAX) & (df["rsi"] > rsi_prev) & (df["rsi"] < RSI_ENTRY_CEIL)
    short_trend = df["ema_fast"] > df["ema_slow"]
    confirm = df["close"] > df["open"]
    vol_ok = df["rel_vol"] >= MIN_REL_VOLUME
    atr_pct = df["atr"] / df["close"]
    atr_ok = (atr_pct >= MIN_ATR_PCT) & (atr_pct <= MAX_ATR_PCT)
    vwap_ok = df["close"] > df["vwap"]

    df["signal"] = (setup & short_trend & confirm & vol_ok & atr_ok & vwap_ok & df["trend_ok"] & df["regime_ok"] & time_ok).fillna(False)

    depth = ((RSI_ENTRY_MAX - rsi_prev) / RSI_ENTRY_MAX).clip(0, 1)
    strength = ((df["ema_fast"] - df["ema_slow"]) / df["atr"]).clip(0, 2) / 2
    volume = (df["rel_vol"] / 2).clip(0, 1)
    df["score"] = (50 * depth + 30 * strength + 20 * volume).fillna(0.0)
    return df


def position_size(equity: float, cash: float, price: float, atr: float, exposure: float, risk_factor: float = 1.0) -> int:
    if min(equity, price, atr) <= 0:
        return 0
    stop_distance = ATR_STOP_MULTIPLIER * atr
    qty_by_risk = int(equity * RISK_PER_TRADE * risk_factor / stop_distance)
    available = max(0.0, equity * MAX_TOTAL_EXPOSURE - exposure)
    allowed = min(available, equity * MAX_POSITION_FRACTION, cash)
    qty = min(qty_by_risk, int(allowed / price))
    if qty < 1 or qty * price < MIN_ORDER_VALUE:
        return 0
    return qty


# ============================================================
# 5. MARKTDATEN
# ============================================================

def fetch_bars(symbols: list[str], timeframe, start: datetime) -> dict[str, pd.DataFrame]:
    feed = DataFeed.SIP if DATA_FEED_NAME == "sip" else DataFeed.IEX
    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=timeframe,
        start=start,
        adjustment=Adjustment.ALL,
        feed=feed,
    )
    raw = data_client.get_stock_bars(req).df
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out

    required = ["open", "high", "low", "close", "volume"]
    for sym in symbols:
        if isinstance(raw.index, pd.MultiIndex):
            if sym not in raw.index.get_level_values(0):
                continue
            df = raw.xs(sym, level=0).copy()
        else:
            df = raw.copy()
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df.dropna(subset=required)
        df = df[df["close"] > 0]
        if not df.empty:
            out[sym] = df
    return out

_daily_cache: dict = {"date": None, "ctx": {}}

def get_daily_contexts() -> dict[str, pd.DataFrame]:
    today = ny_now().date().isoformat()
    if _daily_cache["date"] == today and _daily_cache["ctx"]:
        return _daily_cache["ctx"]
    start = datetime.now(timezone.utc) - timedelta(days=DAILY_LOOKBACK_DAYS)
    bars = fetch_bars(TICKERS + [REGIME_SYMBOL], TimeFrame.Day, start)
    ctx = {sym: daily_context(df) for sym, df in bars.items()}
    _daily_cache.update(date=today, ctx=ctx)
    return ctx


# ============================================================
# 6. BROKER & ORDERS
# ============================================================

def get_positions() -> dict:
    return {p.symbol: p for p in trading_client.get_all_positions()}

def get_open_orders() -> list:
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    flat, seen = [], set()
    for order in trading_client.get_orders(filter=req):
        for o in [order] + list(getattr(order, "legs", None) or []):
            if str(o.id) not in seen:
                seen.add(str(o.id))
                flat.append(o)
    return flat

def find_stop_order(open_orders: list, symbol: str):
    for o in open_orders:
        if o.symbol == symbol and _val(o.side) == "sell" and _val(o.order_type) in ("stop", "stop_limit", "trailing_stop"):
            return o
    return None

def submit_entry(symbol: str, qty: int, price: float, atr: float, bar_time: pd.Timestamp, score: float) -> bool:
    stop = round(price - ATR_STOP_MULTIPLIER * atr, 2)
    target = round(price + ATR_TARGET_MULTIPLIER * atr, 2)
    
    kauf_zeit = ny_now().strftime("%d.%m.%Y um %H:%M Uhr")
    msg = (
        f"🟢 **GEKAUFT: {symbol}** (mit VWAP-Filter)\n"
        f"🕒 **Zeitpunkt:** {kauf_zeit}\n"
        f"• **Menge:** {qty} Stück\n"
        f"• **Kaufpreis:** ${price:.2f}\n"
        f"• **Stop-Loss:** ${stop:.2f}\n"
        f"• **Risiko-Faktor:** {state.risk_factor()}x"
    )
    
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target),
        stop_loss=StopLossRequest(stop_price=stop),
        client_order_id=f"smartbot-{symbol}-{int(bar_time.timestamp())}",
    )
    try:
        submitted = trading_client.submit_order(order_data=order)
    except Exception:
        return False

    state.trades[symbol] = {
        "order_id": str(submitted.id),
        "status": "pending",
        "qty": qty,
        "entry": price,
        "signal_price": price,
        "stop": stop,
        "initial_stop": stop,
        "target": target,
        "atr": atr,
        "risk": price - stop,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    state.save()
    notify(msg)
    return True

def flatten(symbol: str, reason: str, open_orders: list, last_price: float | None = None) -> None:
    try:
        for o in open_orders:
            if o.symbol == symbol:
                try:
                    trading_client.cancel_order_by_id(o.id)
                except Exception:
                    pass
        time.sleep(1.0)
        trading_client.close_position(symbol)
        if symbol in state.trades and last_price:
            state.trades[symbol]["exit_hint"] = last_price
            state.save()
        notify(f"🔴 **VERKAUFT {symbol}**\n• Grund: {reason}")
    except Exception:
        logger.exception("%s: Schließen fehlgeschlagen.", symbol)

def modify_stop(symbol: str, new_stop: float, open_orders: list) -> bool:
    order = find_stop_order(open_orders, symbol)
    if order is None:
        return False
    try:
        trading_client.replace_order_by_id(order.id, ReplaceOrderRequest(stop_price=new_stop))
        return True
    except Exception:
        return False


# ============================================================
# 7. TRADE-VERWALTUNG & RUNNER
# ============================================================

def reconcile_trades(positions: dict, open_orders: list) -> None:
    open_symbols = {o.symbol for o in open_orders}
    now = datetime.now(timezone.utc)

    for symbol, trade in list(state.trades.items()):
        if symbol in positions or symbol in open_symbols:
            continue
        age = (now - datetime.fromisoformat(trade["submitted_at"])).total_seconds()
        if age < 120:
            continue

        entry = trade.get("entry")
        exit_price = trade.get("exit_hint")
        filled = False
        try:
            parent = trading_client.get_order_by_id(trade["order_id"])
            filled = _val(parent.status) == "filled"
            if filled and parent.filled_avg_price:
                entry = float(parent.filled_avg_price)
            for leg in getattr(parent, "legs", None) or []:
                if _val(leg.status) == "filled" and leg.filled_avg_price:
                    exit_price = float(leg.filled_avg_price)
        except Exception:
            continue

        state.trades.pop(symbol, None)
        if not filled:
            state.save()
            continue

        verkauf_zeit = ny_now().strftime("%d.%m.%Y um %H:%M Uhr")
        state.data["cooldown"][symbol] = datetime.now(timezone.utc).isoformat()

        if exit_price and entry and trade["risk"] > 0:
            r = (exit_price - entry) / trade["risk"]
            pnl = (exit_price - entry) * trade["qty"]
            state.record_result(r)

            if pnl >= 0:
                header = f"🟢 **VERKAUFT MIT GEWINN: {symbol}**"
                pnl_str = f"🟩 **Gewinn:** +${pnl:.2f} (+{r:.2f}R)"
            else:
                header = f"🔴 **VERKAUFT MIT VERLUST: {symbol}**"
                pnl_str = f"🟥 **Verlust:** -${abs(pnl):.2f} ({r:.2f}R)"

            notify(f"{header}\n🕒 **Zeitpunkt:** {verkauf_zeit}\n• **Kauf:** ${entry:.2f}\n• **Verkauf:** ${exit_price:.2f}\n• {pnl_str}")
        state.save()

def manage_positions(positions: dict, open_orders: list) -> None:
    now = datetime.now(timezone.utc)
    for symbol, pos in positions.items():
        trade = state.trades.get(symbol)
        if not trade:
            continue
        try:
            price = float(pos.current_price)
            if trade["status"] == "pending":
                fill = float(pos.avg_entry_price)
                trade.update(status="open", entry=fill, risk=max(fill - trade["stop"], 0.01))
                state.save()

            entry, risk, stop = trade["entry"], trade["risk"], trade["stop"]
            r_mult = (price - entry) / risk
            
            if price <= stop * 0.998:
                flatten(symbol, f"Sicherheitsnetz-Stop ({r_mult:+.2f}R)", open_orders, price)
                continue
            
            age_days = (now - datetime.fromisoformat(trade["submitted_at"])).total_seconds() / 86400
            if age_days > MAX_HOLD_DAYS:
                flatten(symbol, f"Zeit-Exit nach {age_days:.1f} Tagen ({r_mult:+.2f}R)", open_orders, price)
                continue

            desired = stop
            if r_mult >= BREAKEVEN_R:
                desired = max(desired, entry + 0.1 * risk)
            
            if r_mult >= TRAIL_START_R:
                desired = max(desired, price - 0.8 * trade["atr"])
            
            desired = round(min(desired, price - 0.05), 2)

            if desired >= stop + 0.01 and modify_stop(symbol, desired, open_orders):
                trade["stop"] = desired
                state.save()
                notify(f"🔼 **{symbol}**: Stop auf ${desired:.2f} nachgezogen ({r_mult:+.2f}R). Runner läuft!")
        except Exception:
            logger.exception("%s: Fehler bei Positionsverwaltung.", symbol)


# ============================================================
# 8. EINSTIEGSSUCHE
# ============================================================

def scan_for_entries(clock, account, positions: dict, open_orders: list) -> None:
    equity = float(account.equity)
    cash = float(account.cash)
    occupied = set(positions) | {o.symbol for o in open_orders}
    slots = MAX_OPEN_POSITIONS - len(occupied)
    if slots <= 0:
        return

    ctx = get_daily_contexts()
    if REGIME_SYMBOL not in ctx:
        return
    regime_ctx = ctx[REGIME_SYMBOL]
    now = datetime.now(timezone.utc)
    candidates_symbols = [s for s in TICKERS if s not in occupied]
    bars = fetch_bars(candidates_symbols, TF_INTRADAY, now - timedelta(days=INTRADAY_LOOKBACK_DAYS))
    candidates = []

    for symbol in candidates_symbols:
        try:
            cd = state.data["cooldown"].get(symbol)
            if cd and now - datetime.fromisoformat(cd) < timedelta(minutes=COOLDOWN_MINUTES):
                continue
            df = bars.get(symbol)
            if df is None:
                continue
            df = regular_hours(df)
            df = df[df.index + timedelta(minutes=BAR_MINUTES) <= now]
            if len(df) < MIN_BARS:
                continue
            lag = now - (df.index[-1] + timedelta(minutes=BAR_MINUTES)).to_pydatetime()
            if lag > timedelta(minutes=MAX_BAR_LAG_MINUTES):
                continue

            sig = build_signals(df, ctx.get(symbol), regime_ctx)
            bar_time = sig.index[-1]
            if state.data["last_bar"].get(symbol) == bar_time.isoformat():
                continue
            state.data["last_bar"][symbol] = bar_time.isoformat()
            state.save()

            last = sig.iloc[-1]
            logger.info(
                "%s | Kurs %.2f | VWAP %.2f | RSI %.1f | Signal=%s",
                symbol, last["close"], last["vwap"], last["rsi"], bool(last["signal"])
            )
            if bool(last["signal"]):
                candidates.append((float(last["score"]), symbol, float(last["close"]), float(last["atr"]), bar_time))
        except Exception:
            pass

    exposure = sum(abs(float(p.market_value)) for p in positions.values())
    for score, symbol, price, atr, bar_time in sorted(candidates, reverse=True):
        if slots <= 0:
            break
        qty = position_size(equity, cash, price, atr, exposure, state.risk_factor())
        if qty < 1:
            continue
        if submit_entry(symbol, qty, price, atr, bar_time, score):
            slots -= 1
            exposure += qty * price
            cash -= qty * price


# ============================================================
# 9. HAUPTSCHLEIFE
# ============================================================

def maybe_daily_summary(account, daily_ret: float, drawdown: float) -> None:
    now = ny_now()
    today = now.date().isoformat()
    if now.weekday() < 5 and now.hour >= 16 and state.data["summary_date"] != today:
        s = state.stats
        notify(
            f"📊 **Tagesbericht**\n"
            f"• Kontostand: ${float(account.equity):,.2f}\n"
            f"• Tagesrendite: {daily_ret:+.2%}\n"
            f"• Max. Rücksetzer: {drawdown:.2%}\n"
            f"• Trades Gesamt: {s['wins']}W / {s['losses']}L\n"
            f"• Performance: {s['total_r']:+.1f}R"
        )
        state.data["summary_date"] = today
        state.save()

def run_cycle() -> int:
    clock = trading_client.get_clock()
    account = trading_client.get_account()
    equity = float(account.equity)
    
    last_equity = float(account.last_equity) if getattr(account, "last_equity", None) else None
    daily_ret, drawdown = state.update_risk(equity, last_equity)

    positions = get_positions()
    open_orders = get_open_orders()
    reconcile_trades(positions, open_orders)
    positions = get_positions()
    manage_positions(positions, open_orders)

    if not clock.is_open:
        maybe_daily_summary(account, daily_ret, drawdown)
        return POLL_SECONDS_CLOSED

    minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    if state.data["kill_switch"] or state.data["day"].get("halted") or minutes_to_close < ENTRY_BEFORE_CLOSE_MIN:
        return POLL_SECONDS

    scan_for_entries(clock, account, get_positions(), get_open_orders())
    return POLL_SECONDS

def live_main() -> None:
    init_clients()
    notify(f"🚀 **Bot gestartet** (v3.1 Optimiert)\nBeobachte: {', '.join(TICKERS)}")

    errors = 0
    try:
        while True:
            sleep_for = POLL_SECONDS
            try:
                sleep_for = run_cycle()
                errors = 0
            except Exception:
                errors += 1
                if errors in (3, 10, 30):
                    notify(f"⚠️ **Achtung**: {errors} Fehler in Folge.")
                sleep_for = min(POLL_SECONDS * 2 ** min(errors, 4), 600)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        notify("⏹️ **Bot manuell gestoppt**.")


# ============================================================
# 10. BACKTEST
# ============================================================

def simulate(frames: dict[str, pd.DataFrame], capital: float = STARTING_CAPITAL, slippage_bps: float = SLIPPAGE_BPS) -> dict:
    slip = slippage_bps / 10_000
    rows = {sym: {ts: (o, h, l, c, a, bool(s), sc) for ts, o, h, l, c, a, s, sc in zip(f.index, f["open"], f["high"], f["low"], f["close"], f["atr"], f["signal"], f["score"])} for sym, f in frames.items()}
    all_ts = sorted(set().union(*[set(f.index) for f in frames.values()]))
    bar = timedelta(minutes=BAR_MINUTES)
    cash, positions, last_close, last_exit, pending, trades, curve = capital, {}, {}, {}, [], [], []
    consec_losses, cur_day, day_start_equity, halted, prev_equity = 0, None, capital, False, capital

    for ts in all_ts:
        for sym in frames:
            if row := rows[sym].get(ts): last_close[sym] = row[3]
        day = ts.tz_convert(NY_TZ).date()
        if day != cur_day: cur_day, day_start_equity, halted = day, prev_equity, False

        for sig_ts, score, sym, sig_close, atr in sorted(pending, key=lambda x: -x[1]):
            if not (row := rows[sym].get(ts)) or sym in positions or len(positions) >= MAX_OPEN_POSITIONS or halted: continue
            if ts - sig_ts > 2 * bar or (sym in last_exit and ts - last_exit[sym] < timedelta(minutes=COOLDOWN_MINUTES)): continue
            entry, stop, target = row[0] * (1 + slip), sig_close - ATR_STOP_MULTIPLIER * atr, sig_close + ATR_TARGET_MULTIPLIER * atr
            if entry <= stop + 0.01 or entry >= target: continue
            exposure = sum(p["qty"] * last_close[s] for s, p in positions.items())
            rf = LOSS_STREAK_RISK_FACTOR if consec_losses >= LOSS_STREAK_THROTTLE else 1.0
            if (qty := position_size(cash + exposure, cash, entry, atr, exposure, rf)) < 1: continue
            cash -= qty * entry
            positions[sym] = dict(qty=qty, entry=entry, stop=stop, target=target, atr=atr, risk=entry - stop, entry_ts=ts)
        pending = []

        for sym, p in list(positions.items()):
            if not (row := rows[sym].get(ts)): continue
            o, h, l, c = row[:4]
            exit_price, limit_exit = None, False
            if l <= p["stop"]: exit_price = min(o, p["stop"])
            elif h >= p["target"]: exit_price, limit_exit = max(o, p["target"]), True
            else:
                r_mult = (c - p["entry"]) / p["risk"]
                if r_mult >= BREAKEVEN_R: p["stop"] = max(p["stop"], p["entry"] + 0.1 * p["risk"])
                if r_mult >= TRAIL_START_R: p["stop"] = max(p["stop"], c - 0.8 * p["atr"])
                if ts - p["entry_ts"] > timedelta(days=MAX_HOLD_DAYS): exit_price = c
            if exit_price is not None:
                fill = exit_price if limit_exit else exit_price * (1 - slip)
                cash += p["qty"] * fill
                r = (fill - p["entry"]) / p["risk"]
                trades.append(dict(symbol=sym, entry_ts=p["entry_ts"], exit_ts=ts, r=r, pnl=(fill - p["entry"]) * p["qty"]))
                if r < -0.05: consec_losses += 1
                elif r > 0.05: consec_losses = 0
                last_exit[sym], _ = ts, positions.pop(sym)

        equity = cash + sum(p["qty"] * last_close[s] for s, p in positions.items())
        if equity / day_start_equity - 1 <= -MAX_DAILY_LOSS: halted = True
        prev_equity = equity
        curve.append((ts, equity))

        for sym in frames:
            if (row := rows[sym].get(ts)) and row[5] and sym not in positions:
                pending.append((ts, row[6], sym, row[3], row[4]))

    eq = pd.Series([v for _, v in curve], index=[t for t, _ in curve]) if curve else pd.Series(dtype=float)
    tdf = pd.DataFrame(trades)
    result = {"equity": eq, "trades": tdf, "open_positions": len(positions), "n_trades": len(tdf)}
    if len(eq): result.update(total_return=eq.iloc[-1] / capital - 1, max_drawdown=(eq / eq.cummax() - 1).min())
    if len(tdf):
        wins, losses = tdf[tdf["pnl"] > 0], tdf[tdf["pnl"] <= 0]
        result.update(win_rate=len(wins) / len(tdf), avg_r=tdf["r"].mean(), profit_factor=wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() != 0 else float("inf"))
    return result

def run_backtest(days: int) -> None:
    init_clients()
    now = datetime.now(timezone.utc)
    intraday = fetch_bars(TICKERS, TF_INTRADAY, now - timedelta(days=days + 14))
    daily = fetch_bars(TICKERS + [REGIME_SYMBOL], TimeFrame.Day, now - timedelta(days=days + DAILY_LOOKBACK_DAYS))
    if REGIME_SYMBOL not in daily: sys.exit("Keine Tagesdaten.")
    regime_ctx = daily_context(daily[REGIME_SYMBOL])

    frames, bench = {}, []
    cutoff = now - timedelta(days=days)
    for sym in TICKERS:
        if sym not in intraday or sym not in daily: continue
        sig = build_signals(regular_hours(intraday[sym]), daily_context(daily[sym]), regime_ctx)
        sig = sig[sig.index >= cutoff]
        if sig.empty: continue
        frames[sym] = sig
        bench.append(sig["close"].iloc[-1] / sig["close"].iloc[0] - 1)
    
    if not frames: sys.exit("Keine Daten für Backtest.")
    res = simulate(frames)
    print("\n=========== BACKTEST ===========")
    print(f"Trades: {res['n_trades']}")
    if res["n_trades"]:
        print(f"Trefferquote: {res['win_rate']:.1%} | Ø Ergebnis: {res['avg_r']:+.2f}R | Rendite: {res.get('total_return', 0):+.2%} | Max. Drawdown: {res.get('max_drawdown', 0):.2%}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    DRY_RUN = args.dry_run
    if args.backtest: run_backtest(args.days)
    else: live_main()
