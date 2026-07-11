#!/usr/bin/env python3
"""
双核 (Dual Core) — NVDA / LLY / JPM / SOXL / AMBA EMA state-machine engine
============================================================================
Pulls daily OHLC data from yfinance, computes the EMA ladder
(5/9/20/60/120/180/195/225), replays the entry / stop-loss / take-profit
rules day by day to find the CURRENT position state, and writes everything
the dashboard needs to data/signals.json.

Rules (standard values — see params.csv for per-ticker overrides):
  Entry:
    price <= EMA180                -> 开仓 10%
    price <= EMA195                -> 加仓至 35%
    price <= EMA225                -> 加仓至 75%
  Stop loss:
    price <= 0.9 * EMA225          -> 清仓 (full exit, resets everything)
  Take profit (only once position > 0):
    1) wait for bullish alignment: EMA5 > EMA9 > EMA20 > EMA60
    2) wait for price >= (1y high, 30 trading days stale) * 1.09
    3) once both are true, price < EMA5  -> 减仓一半
    4) once both are true, price < EMA9  -> 清仓 (full exit, resets everything)

Position size only ever *increases* via the entry ladder and only ever
*decreases* via stop-loss or take-profit — never on a mere bounce back
above an EMA.

PER-TICKER PARAMETERS: every threshold above (entry position sizes, stop
multiplier, TP multiplier/lag, and which price — close vs intraday
low/high — each rule checks against) is read from params.csv. Each row is
one parameter; the "standard" column is the default, and any ticker's own
column overrides it for that ticker only. Leave a ticker's cell blank to
just use the standard value. Bullish-stack alignment (EMA5>9>20>60) is
always Close-based and not currently configurable.
"""

import csv
import json
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

VERSION = "1.3.0"
TICKERS = ["NVDA", "LLY", "JPM", "SOXL", "AMBA"]
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

PARAMS_FILE = "params.csv"
PRICE_MODE_PARAMS = {"entry_price_mode", "stop_price_mode", "tp_high_price_mode", "tp_exit_price_mode"}
INT_PARAMS = {"tp_exclude_days"}

DEFAULT_PARAMS = {
    "entry_ema180_pct": 10.0,
    "entry_ema195_pct": 35.0,
    "entry_ema225_pct": 75.0,
    "entry_price_mode": "close",
    "stop_multiplier": 0.9,
    "stop_price_mode": "close",
    "tp_multiplier": 1.09,
    "tp_exclude_days": 30,
    "tp_high_price_mode": "close",
    "tp_exit_price_mode": "close",
}


def _cast_param(name: str, raw: str):
    if name in PRICE_MODE_PARAMS:
        return raw.strip().lower()
    if name in INT_PARAMS:
        return int(float(raw))
    return float(raw)


def _read_params_csv() -> list:
    """Returns the raw CSV rows (list of dicts), or [] if the file is
    missing/unreadable — callers fall back to DEFAULT_PARAMS in that case."""
    try:
        with open(PARAMS_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def load_ticker_params(ticker: str, rows: list) -> dict:
    """Effective parameters for one ticker: standard value, overridden by
    that ticker's own column if it's non-blank."""
    params = dict(DEFAULT_PARAMS)
    for row in rows:
        name = (row.get("param") or "").strip()
        if name not in DEFAULT_PARAMS:
            continue
        ticker_val = (row.get(ticker) or "").strip()
        standard_val = (row.get("standard") or "").strip()
        raw = ticker_val if ticker_val else standard_val
        if not raw:
            continue
        try:
            params[name] = _cast_param(name, raw)
        except ValueError:
            pass  # bad cell value — keep the default rather than crash the run
    return params


def price_col(mode: str) -> str:
    return {"close": "Close", "low": "Low", "high": "High"}.get(mode, "Close")


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


def compute_tp_trigger_flag(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Flags days where the take-profit "high" price (per tp_high_price_mode
    — close or intraday high) breaks tp_multiplier above the 1-year high of
    THAT SAME price series, computed over the window from 252 trading days
    ago to tp_exclude_days trading days ago.

    Implementation note: shift THEN take a (252-N)-day rolling max, not the
    other way around. Shifting a 252-day rolling max by N days would reach
    back 252+N days total, pulling in extra older history and overstating
    the reference high. Shifting first and then taking a (252-N)-day window
    on the shifted series gives exactly the intended 252-trading-day span
    ending N days ago."""
    col = price_col(params["tp_high_price_mode"])
    n = params["tp_exclude_days"]
    window = 252 - n
    df["prior_1y_max"] = df[col].shift(n).rolling(window=window, min_periods=1).max()
    df["tp_trigger_level"] = df["prior_1y_max"] * params["tp_multiplier"]
    df["hit_tp_high"] = df[col] >= df["tp_trigger_level"]
    return df


def run_state_machine(df: pd.DataFrame, params: dict) -> dict:
    """Replay the rules day by day. Returns current state + event log."""
    entry_col = price_col(params["entry_price_mode"])
    stop_col = price_col(params["stop_price_mode"])
    exit_col = price_col(params["tp_exit_price_mode"])
    stop_mult = params["stop_multiplier"]
    pct_180 = params["entry_ema180_pct"]
    pct_195 = params["entry_ema195_pct"]
    pct_225 = params["entry_ema225_pct"]

    position_pct = 0.0
    aligned = False          # bullish alignment (5>9>20>60) seen since entry
    tp_high_hit = False       # price broke tp_multiplier*1y-high since entry
    tp_half_done = False     # already took the half-reduce
    armed = True              # entry ladder is "live"; disarmed right after a
                               # stop-loss so a falling knife can't re-trigger
                               # a fresh max entry the very next day
    events = []               # list of {date, action, position_pct}

    def log(date, action, pct):
        events.append({"date": date, "action": action, "position_pct": pct})

    for _, row in df.iterrows():
        date = row["date_str"]
        close_price = row["Close"]
        entry_price = row[entry_col]
        stop_price = row[stop_col]
        exit_price = row[exit_col]
        e5, e9, e20, e60 = row["ema5"], row["ema9"], row["ema20"], row["ema60"]
        e180, e195, e225 = row["ema180"], row["ema195"], row["ema225"]

        # re-arm once price recovers back above EMA225 (fully clears the
        # distress zone) so a fresh decline can trigger the ladder again
        if not armed and entry_price > e225:
            armed = True

        # ---- 1) Stop loss (highest priority) ----
        if position_pct > 0 and stop_price <= stop_mult * e225:
            position_pct = 0.0
            aligned = False
            tp_high_hit = False
            tp_half_done = False
            armed = False
            log(date, "Stop-Loss Exit", position_pct)
            continue

        # ---- 2) Entry / scale-in ladder ----
        if armed:
            if entry_price <= e225:
                if position_pct < pct_225:
                    position_pct = pct_225
                    log(date, f"Add to {pct_225:.0f}%", position_pct)
            elif entry_price <= e195:
                if position_pct < pct_195:
                    position_pct = pct_195
                    log(date, f"Add to {pct_195:.0f}%", position_pct)
            elif entry_price <= e180:
                if position_pct < pct_180:
                    position_pct = pct_180
                    log(date, f"Enter {pct_180:.0f}%", position_pct)

        # ---- 3) Take profit (only relevant once in a position) ----
        if position_pct > 0:
            if not aligned and (e5 > e9 > e20 > e60):
                aligned = True
                log(date, "Bullish Stack Confirmed", position_pct)

            if not tp_high_hit and bool(row["hit_tp_high"]):
                tp_high_hit = True
                log(date, "TP High Trigger", position_pct)

            # take-profit ladder only arms once BOTH bullish alignment
            # AND the high-trigger have occurred since entry
            tp_ready = aligned and tp_high_hit

            if tp_ready and not tp_half_done and exit_price < e5:
                position_pct = position_pct / 2.0
                tp_half_done = True
                log(date, "TP Half Exit", position_pct)

            if tp_ready and tp_half_done and exit_price < e9:
                position_pct = 0.0
                aligned = False
                tp_high_hit = False
                tp_half_done = False
                log(date, "TP Full Exit", position_pct)

    last = df.iloc[-1]
    price = last["Close"]
    e225 = last["ema225"]

    # keep the trailing 2 years of event history. Unlimited was too much
    # (some tickers have 8+ years of cycles); the original 1-year window
    # was too little — it hid a real position's own entry dates when the
    # position had been held quietly for >1 year with no further action.
    two_years_ago = pd.to_datetime(last["date_str"]) - pd.Timedelta(days=730)
    recent_events = [e for e in events if pd.to_datetime(e["date"]) >= two_years_ago]

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
        "stop_level": round(float(stop_mult * e225), 2),
        "tp_level": round(float(last["tp_trigger_level"]), 2),
        "last_events": recent_events,  # trailing 2-year state-change history
        "chart": build_chart_series(df),
        "params": params,  # effective (resolved) params used for this ticker
    }


def fetch_one(ticker: str, params_rows: list) -> dict:
    params = load_ticker_params(ticker, params_rows)

    hist = yf.Ticker(ticker).history(period=LOOKBACK, interval="1d", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker}")
    hist = hist.reset_index()
    hist["date_str"] = hist["Date"].dt.strftime("%Y-%m-%d")
    hist = compute_emas(hist)
    hist = compute_tp_trigger_flag(hist, params)
    hist = hist.dropna(subset=[f"ema{p}" for p in EMA_PERIODS]).reset_index(drop=True)
    if hist.empty:
        raise RuntimeError(f"Not enough history for {ticker} to compute EMA225")
    # drop the warm-up window (see WARMUP_DAYS comment above) before replaying
    # the state machine — EMAs above were already computed on the FULL
    # history, so this only affects which rows count as "state-eligible",
    # not the accuracy of the EMA values themselves
    if len(hist) > WARMUP_DAYS:
        hist = hist.iloc[WARMUP_DAYS:].reset_index(drop=True)
    return run_state_machine(hist, params)


def build_params_table(params_rows: list) -> list:
    """Raw params.csv content (param/meaning/standard/per-ticker), trimmed
    to only the tickers currently tracked — for the frontend's collapsible
    Parameters panel. Not resolved/defaulted; blanks stay blank so the UI
    can show "using standard" for empty cells."""
    table = []
    for row in params_rows:
        name = (row.get("param") or "").strip()
        if name not in DEFAULT_PARAMS:
            continue
        table.append({
            "param": name,
            "meaning": (row.get("meaning") or "").strip(),
            "standard": (row.get("standard") or "").strip(),
            **{t: (row.get(t) or "").strip() for t in TICKERS},
        })
    return table


def main():
    params_rows = _read_params_csv()

    result = {
        "version": VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params_table": build_params_table(params_rows),
        "tickers": {},
    }
    for t in TICKERS:
        try:
            result["tickers"][t] = fetch_one(t, params_rows)
            print(f"[ok] {t}: {result['tickers'][t]['status']} @ {result['tickers'][t]['price']}")
        except Exception as e:
            print(f"[error] {t}: {e}", file=sys.stderr)
            sys.exit(1)

    with open("data/signals.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("Wrote data/signals.json")


if __name__ == "__main__":
    main()
