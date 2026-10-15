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
                exit_price = max(o, position["target"]) * (1 - slip)
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
    print("\n=========== BACKTEST v6.0 High-Conviction Power-Trend ===========")
    print(f"Trades: {result['n_trades']}")
    if result["n_trades"]:
        print(f"Trefferquote: {result.get('win_rate', 0):.1%} | Ø Ergebnis: {result.get('avg_r', 0):+.2f}R | Rendite: {result.get('total_return', 0):+.2%} | Max. Drawdown: {result.get('max_drawdown', 0):.2%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Trading Bot v7.0")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    if args.backtest:
        run_backtest(args.days)
    else:
        live_main()


if __name__ == "__main__":
    main()
