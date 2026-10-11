import time
import requests
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# 1. Zugangsdaten & Einstellungen
API_KEY = "PKGH6S4LYZMDR3PPRVYKXA5UX6"
SECRET_KEY = "BNpVQLSsp8LdRsMSi9Z3PzyUGYsRDfHeKhBU36Q37Mgu"

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1558899105349107712/CGbhVCnvDDZGuVRYwvdrCbXtZRMNcSHrRUrXUUO3Pp2vIhx1TeCfv9YciubAbjp4Ksf"
 

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# MULTI-AKTIEN WATCHLIST
TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT"]
STUECKZAHL = 1

# Strategie & Risiko-Management Einstellungen
TAKE_PROFIT_PROZENT = 0.02   # 2% Gewinnziel -> Automatisch verkaufen
STOP_LOSS_PROZENT = 0.01     # 1% Verlustgrenze -> Automatisch verkaufen

def sende_discord_nachricht(text):
    """Sendet eine Push-Benachrichtigung an deinen Discord-Kanal."""
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"content": f"🤖 **Trading-Bot Update:**\n{text}"}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"Fehler beim Senden der Discord-Nachricht: {e}")

def hole_markt_daten(symbol):
    """Holt Kurse und berechnet den 5-Tage-Durchschnitt."""
    aktie = yf.Ticker(symbol)
    historie = aktie.history(period="5d", interval="1m")
    
    aktueller_kurs = historie['Close'].iloc[-1]
    durchschnitt_kurs = historie['Close'].mean()
    
    return aktueller_kurs, durchschnitt_kurs

def hole_position_details(symbol):
    """Holt Details zur aktuellen Position (Kaufpreis, Menge)."""
    try:
        pos = trading_client.get_open_position(symbol)
        return {
            "im_besitz": True,
            "menge": int(pos.qty),
            "kaufpreis": float(pos.avg_entry_price),
            "aktueller_wert": float(pos.market_value)
        }
    except Exception:
        return {"im_besitz": False, "menge": 0, "kaufpreis": 0.0, "aktueller_wert": 0.0}

def kaufe_aktie(symbol, menge, kurs):
    """Führt eine Kauf-Order aus und benachrichtigt via Discord."""
    req = MarketOrderRequest(
        symbol=symbol,
        qty=menge,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC
    )
    order = trading_client.submit_order(order_data=req)
    msg = f"🟢 **KAUF ERFOLGREICH!**\nAktie: `{symbol}`\nMenge: `{menge}`\nEinstiegskurs: `{kurs:.2f} USD`\nOrder-ID: `{order.id}`"
    print(f"\n[KAUF] {msg}")
    sende_discord_nachricht(msg)

def verkaufe_aktie(symbol, menge, kurs, grund):
    """Führt eine Verkaufs-Order aus und benachrichtigt via Discord."""
    req = MarketOrderRequest(
        symbol=symbol,
        qty=menge,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC
    )
    order = trading_client.submit_order(order_data=req)
    msg = f"🔴 **VERKAUF ERFOLGREICH!**\nAktie: `{symbol}`\nMenge: `{menge}`\nVerkaufskurs: `{kurs:.2f} USD`\nGrund: `{grund}`\nOrder-ID: `{order.id}`"
    print(f"\n[VERKAUF] {msg}")
    sende_discord_nachricht(msg)

# Haupt-Schleife
print(f"=== STARTE MULTI-TICKER TRADING BOT FÜR {TICKERS} ===")
sende_discord_nachricht(f"🚀 Bot gestartet für **{', '.join(TICKERS)}** mit Take-Profit (+{TAKE_PROFIT_PROZENT*100}%) & Stop-Loss (-{STOP_LOSS_PROZENT*100}%).")

while True:
    print(f"\n==================== Check um {time.strftime('%H:%M:%S')} ====================")
    for ticker in TICKERS:
        try:
            kurs, durchschnitt = hole_markt_daten(ticker)
            pos = hole_position_details(ticker)
            
            print(f"\n--- [{ticker}] Kurs: {kurs:.2f} USD | Schnitt: {durchschnitt:.2f} USD ---")
            
            if pos["im_besitz"]:
                kaufpreis = pos["kaufpreis"]
                gewinn_verlust_prozent = (kurs - kaufpreis) / kaufpreis
                print(f"Im Depot: Ja | Einstieg: {kaufpreis:.2f} USD | P/L: {gewinn_verlust_prozent*100:+.2f}%")
                
                # 1. Take-Profit (+2%)
                if gewinn_verlust_prozent >= TAKE_PROFIT_PROZENT:
                    verkaufe_aktie(ticker, pos["menge"], kurs, f"Take-Profit erreicht (+{gewinn_verlust_prozent*100:.2f}%) 🎯")
                
                # 2. Stop-Loss (-1%)
                elif gewinn_verlust_prozent <= -STOP_LOSS_PROZENT:
                    verkaufe_aktie(ticker, pos["menge"], kurs, f"Stop-Loss ausgelöst ({gewinn_verlust_prozent*100:.2f}%) 🛡️")
                
                # 3. Strategie-Verkauf
                elif kurs < durchschnitt:
                    verkaufe_aktie(ticker, pos["menge"], kurs, "Trendwende: Kurs unter Durchschnitt 📉")
                    
                else:
                    print("-> Halte Position.")
                    
            else:
                print("Im Depot: Nein")
                if kurs > durchschnitt:
                    print(f"-> SIGNAL: Aufwärtstrend für {ticker} erkannt! Kaufe...")
                    kaufe_aktie(ticker, STUECKZAHL, kurs)
                else:
                    print("-> Kein Kaufsignal.")

        except Exception as e:
            print(f"Fehler bei {ticker}: {e}")
            
    time.sleep(60)
