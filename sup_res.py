#!/usr/bin/env python3
import argparse
import sys
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import yfinance as yf
import pandas as pd

# ------------------- CONFIG -------------------
ALERT_PCT = 0.005         # 0.5% proximity (was 0.02)
POLL_SECS = 300           # 5 min
LOOKBACK_LOCAL = 5        # 1-week
LOOKBACK_52W = 252        # 52-week
LOOKBACK_30D = 30         # Monthly
MIN_VOLUME_PER_MIN = 50000
PRICE_LO = 2.0
PRICE_HI = 50.0
CSV_FILE = "nasdaq_2_to_50_stocks.csv"
# ---------------------------------------------

# ---------- Load CSV ----------
def load_universe() -> List[str]:
    try:
        df = pd.read_csv(CSV_FILE)
        symbols = df['symbol'].dropna().str.strip().str.upper().unique().tolist()
        print(f"Loaded {len(symbols)} symbols from {CSV_FILE}")
        return symbols
    except Exception as e:
        print(f"CSV error: {e}")
        sys.exit(1)

# ---------- Data ----------
def fetch_daily(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    data = {}
    for t in tickers:
        try:
            df = yf.download(t, period="2y", interval="1d", auto_adjust=True, progress=False)
            if not df.empty:
                data[t] = df
        except:
            pass
    return data

def fetch_intraday(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    data = {}
    for t in tickers:
        try:
            df = yf.download(t, period="5d", interval="5m", auto_adjust=True, progress=False)
            if not df.empty:
                data[t] = df
        except:
            pass
    return data

# ---------- Levels ----------
def last_close(df: pd.DataFrame) -> Optional[float]:
    try:
        return float(df["Close"].iloc[-1])
    except:
        return None

def prior_local_hi_lo(df: pd.DataFrame):
    if len(df) <= LOOKBACK_LOCAL:
        return None, None
    window = df.iloc[-(LOOKBACK_LOCAL+1):-1]
    return float(window["High"].max()), float(window["Low"].min())

def hi_lo_52w(df: pd.DataFrame):
    window = df.iloc[-LOOKBACK_52W:] if len(df) >= LOOKBACK_52W else df
    return float(window["High"].max()), float(window["Low"].min())

def hi_lo_30d(df: pd.DataFrame):
    cutoff = datetime.now() - timedelta(days=LOOKBACK_30D)
    recent = df[df.index >= cutoff]
    if recent.empty:
        return None, None
    return float(recent["High"].max()), float(recent["Low"].min())

def today_yesterday_hilo(df_intra: pd.DataFrame):
    if df_intra is None or df_intra.empty:
        return None, None, None, None
    df = df_intra.copy()
    df["date"] = df.index.date
    groups = df.groupby("date")
    dates = sorted(groups.groups.keys())
    if len(dates) < 1:
        return None, None, None, None
    today = groups.get_group(dates[-1])
    yday = groups.get_group(dates[-2]) if len(dates) >= 2 else None
    t_hi = float(today["High"].max()) if not today.empty else None
    t_lo = float(today["Low"].min()) if not today.empty else None
    y_hi = float(yday["High"].max()) if yday is not None and not yday.empty else None
    y_lo = float(yday["Low"].min()) if yday is not None and not yday.empty else None
    return t_hi, t_lo, y_hi, y_lo

def estimate_vol_per_min(df_intra: pd.DataFrame) -> Optional[float]:
    if df_intra is None or df_intra.empty or "Volume" not in df_intra.columns:
        return None
    df = df_intra.copy()
    df["date"] = df.index.date
    today = df[df["date"] == df["date"].max()]
    if today.empty:
        return None
    tail = today.tail(3)
    total = float(tail["Volume"].sum())
    mins = 5.0 * max(len(tail), 1)
    return total / mins if total > 0 else None

# ---------- Filter $2–$50 ----------
def screen_price_band(daily_data: Dict[str, pd.DataFrame]) -> List[str]:
    return [t for t, df in daily_data.items() if (p := last_close(df)) and PRICE_LO <= p <= PRICE_HI]

# ---------- Alerts (Only Real, 0.5% Proximity) ----------
def analyze_ticker(t: str, df_daily: pd.DataFrame, df_intra: Optional[pd.DataFrame]):
    p = last_close(df_daily)
    if not p or PRICE_LO > p or p > PRICE_HI:
        return []

    # Volume gate — silently skip
    vpm = estimate_vol_per_min(df_intra)
    if vpm is not None and vpm < MIN_VOLUME_PER_MIN:
        return []

    now = datetime.now().strftime("%H:%M")
    msgs = []

    # 1-Week
    loc_hi, loc_lo = prior_local_hi_lo(df_daily)
    if loc_hi and p > loc_hi:
        msgs.append(f"[{now}] {t}: Broke 1W HIGH → ${p:,.2f}")
    elif loc_hi and abs(p - loc_hi) / loc_hi <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near 1W HIGH → ${p:,.2f}")

    if loc_lo and p < loc_lo:
        msgs.append(f"[{now}] {t}: Broke 1W LOW → ${p:,.2f}")
    elif loc_lo and abs(p - loc_lo) / loc_lo <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near 1W LOW → ${p:,.2f}")

    # 52-Week
    yr_hi, yr_lo = hi_lo_52w(df_daily)
    if yr_hi and p > yr_hi:
        msgs.append(f"[{now}] {t}: New 52W HIGH → ${p:,.2f}")
    elif yr_hi and abs(p - yr_hi) / yr_hi <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near 52W HIGH → ${p:,.2f}")

    if yr_lo and p < yr_lo:
        msgs.append(f"[{now}] {t}: New 52W LOW → ${p:,.2f}")
    elif yr_lo and abs(p - yr_lo) / yr_lo <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near 52W LOW → ${p:,.2f}")

    # Monthly
    mon_hi, mon_lo = hi_lo_30d(df_daily)
    if mon_hi and p > mon_hi:
        msgs.append(f"[{now}] {t}: New MONTHLY HIGH → ${p:,.2f}")
    elif mon_hi and abs(p - mon_hi) / mon_hi <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near MONTHLY HIGH → ${p:,.2f}")

    if mon_lo and p < mon_lo:
        msgs.append(f"[{now}] {t}: New MONTHLY LOW → ${p:,.2f}")
    elif mon_lo and abs(p - mon_lo) / mon_lo <= ALERT_PCT:
        msgs.append(f"[{now}] {t}: Near MONTHLY LOW → ${p:,.2f}")

    # Today/Yesterday
    if df_intra is not None:
        t_hi, t_lo, y_hi, y_lo = today_yesterday_hilo(df_intra)
        if t_hi and p > t_hi:
            msgs.append(f"[{now}] {t}: Took out TODAY high")
        if t_lo and p < t_lo:
            msgs.append(f"[{now}] {t}: Broke TODAY low")
        if y_hi and p > y_hi:
            msgs.append(f"[{now}] {t}: Cleared YESTERDAY high")
        if y_lo and p < y_lo:
            msgs.append(f"[{now}] {t}: Lost YESTERDAY low")

    return msgs

# ---------- Main Loop ----------
def run_watch():
    seen = set()
    while True:
        tickers = load_universe()
        daily = fetch_daily(tickers)
        band = screen_price_band(daily)
        intra = fetch_intraday(band)

        header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{header}] Scanning {len(band)} stocks in ${PRICE_LO}–${PRICE_HI} (0.5% proximity)...")

        new_alerts = False
        for t in band:
            msgs = analyze_ticker(t, daily[t], intra.get(t))
            for m in msgs:
                if m not in seen:
                    print(m)
                    seen.add(m)
                    new_alerts = True

        if not new_alerts:
            print("  No new alerts.")

        print(f"Next scan in {POLL_SECS // 60} min...")
        time.sleep(POLL_SECS)

# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.watch:
        run_watch()
    else:
        print("Use --watch to run.")

if __name__ == "__main__":
    main()
