import time
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


API_KEY = "PKNPBIXR7TIVOYWUONFKDOEGF4"
SECRET_KEY = "9JqMriM1LHLsae3MppsBY97PHmWMhHEnHpQBzMvEgFeC"

DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1559227552218878103/RmDOyen4Bc3UNhnIKxRMtCKNbdtk13t3iTEfx21wtMHBw..."



trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Watchlist & Risikomanagement
TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT"]
TAKE_PROFIT_PROZENT = 0.04  # 4% Gewinnziel
STOP_LOSS_PROZENT = 0.02    # 2% Verlustgrenze
MAX_PORTFOLIO_EINSATZ_PRO_TRADE = 0.25  # Max 25% des freien Kapitals pro Trade

def sende_discord_nachricht(text):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"content": f"🚀 **Trading-Bot Update:**\n{text}"}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Fehler beim Senden der Discord-Nachricht: {e}")

def berechne_rsi(df, periods=14):
    close_delta = df['close'].diff()
    up = close_delta.clip(lower=0)
    down = -1 * close_delta.clip(upper=0)
    ma_up = up.ewm(com=periods - 1, adjust=True, min_periods=periods).mean()
    ma_down = down.ewm(com=periods - 1, adjust=True, min_periods=periods).mean()
    rsi = ma_up / ma_down
    rsi = 100 - (100 / (1 + rsi))
    return rsi.iloc[-1]

def hole_rsi_signal(symbol):
    try:
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            limit=50
        )
        bars = data_client.get_stock_bars(request_params)
        df = bars.df
        if symbol in df.index.levels[0]:
            df_symbol = df.loc[symbol].copy()
            rsi = berechne_rsi(df_symbol)
            return rsi
    except Exception as e:
        print(f"Fehler beim Laden der Marktdaten für {symbol}: {e}")
    return None

def hole_position_details(symbol):
    try:
        position = trading_client.get_open_position(symbol)
        qty = float(position.qty)
        avg_price = float(position.avg_entry_price)
        current_price = float(position.current_price)
        pl_pct = (current_price - avg_price) / avg_price
        return True, qty, avg_price, current_price, pl_pct
    except Exception:
        return False, 0, 0, 0, 0

def kaufe_aktie(symbol):
    try:
        account = trading_client.get_account()
        buying_power = float(account.buying_power)
        
        # Kaufe nur für maximal 25% des verfügbaren Kapitals
        einsatz = buying_power * MAX_PORTFOLIO_EINSATZ_PRO_TRADE
        if einsatz < 50:
            print(f"[{symbol}] Nicht genügend freie Kaufkraft für einen Einstieg.")
            return

        # Aktuellen Preis abfragen
        request_params = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, limit=1)
        bars = data_client.get_stock_bars(request_params)
        aktueller_preis = float(bars.df.iloc[-1]['close'])
        
        stueckzahl = int(einsatz // aktueller_preis)
        if stueckzahl < 1:
            stueckzahl = 1

        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=stueckzahl,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC
        )
        trading_client.submit_order(order_data)
        msg = f"🟢 **KAUF ERFOLGT:** {stueckzahl}x **{symbol}** zu je ~{aktueller_preis:.2f} USD."
        print(msg)
        sende_discord_nachricht(msg)
    except Exception as e:
        print(f"Fehler beim Kauf von {symbol}: {e}")

def verkaufe_aktie(symbol, qty, grund):
    try:
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
        trading_client.submit_order(order_data)
        msg = f"🔴 **VERKAUF ERFOLGT ({grund}):** {qty}x **{symbol}** im Markt platziert."
        print(msg)
        sende_discord_nachricht(msg)
    except Exception as e:
        print(f"Fehler beim Verkauf von {symbol}: {e}")

print("=== INTELLIGENTER RSI-TRADING-BOT GESTARTET ===")
sende_discord_nachricht("🤖 **Trading-Bot gestartet!** RSI-Strategie & Risikomanagement (2% SL / 4% TP) aktiv.")

while True:
    for symbol in TICKERS:
        im_depot, qty, avg_price, current_price, pl_pct = hole_position_details(symbol)
        
        if im_depot:
            print(f"[{symbol}] Im Depot: {qty} Stk. | Einstieg: {avg_price:.2f} $ | Aktuell: {current_price:.2f} $ | P/L: {pl_pct*100:+.2f}%")
            
            # Take-Profit Prüfung
            if pl_pct >= TAKE_PROFIT_PROZENT:
                verkaufe_aktie(symbol, qty, f"TAKE PROFIT (+{pl_pct*100:.2f}%)")
            # Stop-Loss Prüfung
            elif pl_pct <= -STOP_LOSS_PROZENT:
                verkaufe_aktie(symbol, qty, f"STOP LOSS ({pl_pct*100:.2f}%)")
            else:
                print(f"-> Halte {symbol}. Warten auf TP (+4%) oder SL (-2%).")
        else:
            rsi = hole_rsi_signal(symbol)
            if rsi is not None:
                print(f"[{symbol}] Nicht im Depot. Aktueller RSI (1Min): {rsi:.2f}")
                
                # Kauf-Signal: Aktie überverkauft (RSI < 30)
                if rsi < 30:
                    print(f"🚀 Kauf-Signal für {symbol} erkannt! (RSI: {rsi:.2f} < 30)")
                    kaufe_aktie(symbol)
                else:
                    print(f"-> Kein Signal für {symbol}. RSI muss unter 30 fallen.")
            else:
                print(f"[{symbol}] Konnte RSI-Wert nicht berechnen.")

    print("\nWarte 60 Sekunden bis zum nächsten Check...\n")
    time.sleep(60)
