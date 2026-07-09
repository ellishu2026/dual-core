#!/usr/bin/env python3
"""
双核 (Dual Core) — NVDA / LLY EMA state-machine engine
========================================================
Pulls daily OHLC data for NVDA and LLY from yfinance, computes the EMA
ladder (5/9/20/60/120/180/195/225), replays the entry / stop-loss /
take-profit rules day by day to find the CURRENT position state, and
writes everything the dashboard needs to data/signals.json.

Rules (as specified):
  Entry:
    price <= EMA180                -> 观察 (watch only, no position)
    price <= EMA195                -> 开仓 30%
    price <= EMA225                -> 加仓至 75%
  Stop loss:
    price <= 0.9 * EMA225          -> 清仓 (full exit, resets everything)
  Take profit (only once position > 0):
    1) wait for bullish alignment: EMA5 > EMA9 > EMA20 > EMA60
    2) after alignment, price < EMA5  -> 减仓一半
    3) after alignment, price < EMA9  -> 清仓 (full exit, resets everything)

Position size only ever *increases* via the entry ladder and only ever
*decreases* via stop-loss or take-profit — never on a mere bounce back
above an EMA.
"""

import json
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

VERSION = "1.0.5"
TICKERS = ["NVDA", "LLY"]
EMA_PERIODS = [5, 9, 20, 60, 120, 180, 195, 225]
LOOKBACK = "5y"  # enough for EMA225 to stabilize and for a trailing-1y high check


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    for p in EMA_PERIODS:
        df[f"ema{p}"] = df["Close"].ewm(span=p, adjust=False).mean()
    return df


CHART_POINTS = 20  # default number of points shown per timeframe


def build_chart_series(df: pd.DataFrame) -> dict:
    """Sample the daily EMA series at day/week/month granularity for
    charting. These are the SAME daily-computed EMAs the strategy trades
    on — 'week'/'month' just pick the last trading day of each week/month
    so the chart can zoom out without inventing weekly/monthly EMAs."""
    d = df.copy()
    d["dt"] = pd.to_datetime(d["date_str"])

    def pack(sub: pd.DataFrame) -> dict:
        sub = sub.tail(CHART_POINTS)
        return {
            "dates": sub["date_str"].tolist(),
            "close": [round(float(v), 2) for v in sub["Close"]],
            "ema": {
                str(p): [round(float(v), 2) for v in sub[f"ema{p}"]]
                for p in EMA_PERIODS
            },
        }

    day = pack(d)

    d["week_period"] = d["dt"].dt.to_period("W")
    week = pack(d.groupby("week_period", as_index=False).tail(1))

    d["month_period"] = d["dt"].dt.to_period("M")
    month = pack(d.groupby("month_period", as_index=False).tail(1))

    return {"day": day, "week": week, "month": month}


def compute_new_high_flag(df: pd.DataFrame) -> pd.DataFrame:
    """Flags days where the close is a new trailing-1-year high (strictly
    above every close in the prior ~252 trading days)."""
    df["prior_1y_max"] = df["Close"].rolling(window=252, min_periods=1).max().shift(1)
    df["is_new_high"] = df["Close"] > df["prior_1y_max"]
    return df


def run_state_machine(df: pd.DataFrame) -> dict:
    """Replay the rules day by day. Returns current state + event log."""
    position_pct = 0.0
    aligned = False          # bullish alignment (5>9>20>60) seen since entry
    new_high = False          # new all-time high seen since entry
    tp_half_done = False     # already took the half-reduce
    armed = True              # entry ladder is "live"; disarmed right after a
                               # stop-loss so a falling knife can't re-trigger
                               # a fresh 75% entry the very next day
    events = []               # list of {date, action, position_pct}

    def log(date, action, pct):
        events.append({"date": date, "action": action, "position_pct": pct})

    for _, row in df.iterrows():
        date = row["date_str"]
        price = row["Close"]
        e5, e9, e20, e60 = row["ema5"], row["ema9"], row["ema20"], row["ema60"]
        e180, e195, e225 = row["ema180"], row["ema195"], row["ema225"]

        # re-arm once price recovers back above EMA225 (fully clears the
        # distress zone) so a fresh decline can trigger the ladder again
        if not armed and price > e225:
            armed = True

        # ---- 1) Stop loss (highest priority) ----
        if position_pct > 0 and price <= 0.9 * e225:
            position_pct = 0.0
            aligned = False
            new_high = False
            tp_half_done = False
            armed = False
            log(date, "Stop-Loss Exit", position_pct)
            continue

        # ---- 2) Entry / scale-in ladder ----
        if armed:
            if price <= e225:
                if position_pct < 75:
                    position_pct = 75.0
                    log(date, "Add to 75%", position_pct)
            elif price <= e195:
                if position_pct < 30:
                    position_pct = 30.0
                    log(date, "Enter 30%", position_pct)
            # price <= e180 alone is watch-only, no position change, no log spam

        # ---- 3) Take profit (only relevant once in a position) ----
        if position_pct > 0:
            if not aligned and (e5 > e9 > e20 > e60):
                aligned = True
                log(date, "Bullish Stack Confirmed", position_pct)

            if not new_high and bool(row["is_new_high"]):
                new_high = True
                log(date, "New 1-Year High", position_pct)

            # take-profit ladder only arms once BOTH bullish alignment
            # AND a fresh all-time high have occurred since entry
            tp_ready = aligned and new_high

            if tp_ready and not tp_half_done and price < e5:
                position_pct = position_pct / 2.0
                tp_half_done = True
                log(date, "TP Half Exit", position_pct)

            if tp_ready and tp_half_done and price < e9:
                position_pct = 0.0
                aligned = False
                new_high = False
                tp_half_done = False
                log(date, "TP Full Exit", position_pct)

    last = df.iloc[-1]
    price = last["Close"]
    e180, e195, e225 = last["ema180"], last["ema195"], last["ema225"]

    # keep every state-change event from the trailing 1 year, not just a
    # fixed count — a quiet year shows few events, an active one shows more
    one_year_ago = pd.to_datetime(last["date_str"]) - pd.Timedelta(days=365)
    recent_events = [e for e in events if pd.to_datetime(e["date"]) >= one_year_ago]

    # current display status — anything that isn't currently in a position
    # (i.e. no entry/stop/take-profit condition is active) just reads 观察
    if position_pct == 0:
        status = "Watching"
    elif tp_half_done:
        status = f"Holding {position_pct:.0f}% (TP Half Taken)"
    else:
        status = f"Holding {position_pct:.0f}%"

    return {
        "date": last["date_str"],
        "price": round(float(price), 2),
        "ema": {str(p): round(float(last[f"ema{p}"]), 2) for p in EMA_PERIODS},
        "position_pct": position_pct,
        "status": status,
        "aligned": aligned,
        "new_high": new_high,
        "tp_half_done": tp_half_done,
        "stop_level": round(float(0.9 * e225), 2),
        "last_events": recent_events,  # all state changes within the trailing 1 year
        "chart": build_chart_series(df),
    }


def fetch_one(ticker: str) -> dict:
    hist = yf.Ticker(ticker).history(period=LOOKBACK, interval="1d", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker}")
    hist = hist.reset_index()
    hist["date_str"] = hist["Date"].dt.strftime("%Y-%m-%d")
    hist = compute_emas(hist)
    hist = compute_new_high_flag(hist)
    hist = hist.dropna(subset=[f"ema{p}" for p in EMA_PERIODS]).reset_index(drop=True)
    if hist.empty:
        raise RuntimeError(f"Not enough history for {ticker} to compute EMA225")
    return run_state_machine(hist)


def main():
    result = {
        "version": VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tickers": {},
    }
    for t in TICKERS:
        try:
            result["tickers"][t] = fetch_one(t)
            print(f"[ok] {t}: {result['tickers'][t]['status']} @ {result['tickers'][t]['price']}")
        except Exception as e:
            print(f"[error] {t}: {e}", file=sys.stderr)
            sys.exit(1)

    with open("data/signals.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("Wrote data/signals.json")


if __name__ == "__main__":
    main()
