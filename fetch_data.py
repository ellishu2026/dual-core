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
    price <= EMA180                -> 开仓 10%
    price <= EMA195                -> 加仓至 35%
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

VERSION = "1.1.7"
TICKERS = ["NVDA", "LLY", "SOXL"]  # SOXL added as a watch-only third ticker —
                                     # runs the same entry/stop/TP engine,
                                     # just for reference/observation
EMA_PERIODS = [5, 9, 20, 60, 120, 180, 195, 225]
LOOKBACK = "10y"  # need real burn-in room now (see WARMUP_DAYS below), not
                    # just enough for EMA225/1y-high math to have inputs
WARMUP_DAYS = 600   # ~2.7y at ~225 trading days/year — comfortably more than
                     # 3x the longest EMA span, the common rule-of-thumb for
                     # an EMA to converge away from its seed value. Without
                     # this, day 0 of any pulled window has ema[0]==price[0]
                     # for every period (an artifact of the adjust=False
                     # recursive formula, not a real signal), which can
                     # spuriously trigger "Add to 75%" on day 1 and then
                     # ride along as fake state for the rest of the replay
                     # if no real stop-loss/take-profit cycle happens to
                     # reset it first. Discarding the state-machine's
                     # warm-up window (EMAs themselves still use full
                     # history for accuracy) closes that gap.


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


TP_HIGH_MULTIPLIER = 1.09  # take-profit's "high" trigger = trailing 1y high x 1.09
TP_HIGH_EXCLUDE_DAYS = 21   # freeze the reference high as of 3 weeks ago —
                             # excluding the most recent 3 weeks stops a
                             # smooth rally from dragging its own reference
                             # high up with it day by day (see README note)


def compute_tp_trigger_flag(df: pd.DataFrame) -> pd.DataFrame:
    """Flags days where the close breaks 9% above the 1-year high computed
    over the window from 252 trading days ago to 21 trading days ago —
    i.e. the trailing 1-year high with the most recent 3 weeks excluded.

    Implementation note: shift THEN take a (252-21)-day rolling max, not
    the other way around. Shifting a 252-day rolling max by 21 days would
    reach back 272 days total (252 + 21), pulling in extra older history
    and overstating the reference high. Shifting first and then taking a
    231-day (252-21) window on the shifted series gives exactly the
    intended 252-trading-day span ending 21 days ago."""
    window = 252 - TP_HIGH_EXCLUDE_DAYS
    df["prior_1y_max"] = df["Close"].shift(TP_HIGH_EXCLUDE_DAYS).rolling(window=window, min_periods=1).max()
    df["tp_trigger_level"] = df["prior_1y_max"] * TP_HIGH_MULTIPLIER
    df["hit_tp_high"] = df["Close"] >= df["tp_trigger_level"]
    return df


def run_state_machine(df: pd.DataFrame) -> dict:
    """Replay the rules day by day. Returns current state + event log."""
    position_pct = 0.0
    aligned = False          # bullish alignment (5>9>20>60) seen since entry
    tp_high_hit = False       # price broke 1y-high*1.09 since entry
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
            tp_high_hit = False
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
                if position_pct < 35:
                    position_pct = 35.0
                    log(date, "Add to 35%", position_pct)
            elif price <= e180:
                if position_pct < 10:
                    position_pct = 10.0
                    log(date, "Enter 10%", position_pct)

        # ---- 3) Take profit (only relevant once in a position) ----
        if position_pct > 0:
            if not aligned and (e5 > e9 > e20 > e60):
                aligned = True
                log(date, "Bullish Stack Confirmed", position_pct)

            if not tp_high_hit and bool(row["hit_tp_high"]):
                tp_high_hit = True
                log(date, "TP High Trigger (+9%)", position_pct)

            # take-profit ladder only arms once BOTH bullish alignment
            # AND a fresh all-time high have occurred since entry
            tp_ready = aligned and tp_high_hit

            if tp_ready and not tp_half_done and price < e5:
                position_pct = position_pct / 2.0
                tp_half_done = True
                log(date, "TP Half Exit", position_pct)

            if tp_ready and tp_half_done and price < e9:
                position_pct = 0.0
                aligned = False
                tp_high_hit = False
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
        "tp_high_hit": tp_high_hit,
        "tp_half_done": tp_half_done,
        "stop_level": round(float(0.9 * e225), 2),
        "tp_level": round(float(last["tp_trigger_level"]), 2),
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
    hist = compute_tp_trigger_flag(hist)
    hist = hist.dropna(subset=[f"ema{p}" for p in EMA_PERIODS]).reset_index(drop=True)
    if hist.empty:
        raise RuntimeError(f"Not enough history for {ticker} to compute EMA225")
    # drop the warm-up window (see WARMUP_DAYS comment above) before replaying
    # the state machine — EMAs above were already computed on the FULL
    # history, so this only affects which rows count as "state-eligible",
    # not the accuracy of the EMA values themselves
    if len(hist) > WARMUP_DAYS:
        hist = hist.iloc[WARMUP_DAYS:].reset_index(drop=True)
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
