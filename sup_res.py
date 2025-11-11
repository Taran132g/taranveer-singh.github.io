#!/usr/bin/env python3
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

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
MIN_VOLUME_PER_MIN = 100000
PRICE_LO = 5.0
PRICE_HI = 30.0
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

def write_alert_file(path: str, generated_at: datetime, symbols: Sequence[str], history: Sequence[str]) -> None:
    """Persist the current alert snapshot to ``path``."""

    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    unique_symbols = sorted({s.strip().upper() for s in symbols if s})

    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(f"# Alerts generated at {timestamp}\n")
        fh.write("TICKERS: ")
        fh.write(",".join(unique_symbols))
        fh.write("\n\n")

        if history:
            for line in history:
                fh.write(f"{line}\n")
        else:
            fh.write("No alerts yet.\n")


def scan_once(seen: Optional[Set[str]] = None) -> Tuple[str, int, List[str], Set[str]]:
    """Run a single scan and return the header, coverage size, new messages, and symbols."""

    tickers = load_universe()
    daily = fetch_daily(tickers)
    band = screen_price_band(daily)
    intra = fetch_intraday(band)

    header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen_cache = seen if seen is not None else set()
    new_msgs: List[str] = []
    triggered: Set[str] = set()

    for t in band:
        df_daily = daily.get(t)
        if df_daily is None:
            continue
        msgs = analyze_ticker(t, df_daily, intra.get(t))
        for msg in msgs:
            if msg in seen_cache:
                continue
            new_msgs.append(msg)
            triggered.add(t)
            seen_cache.add(msg)

    return header, len(band), new_msgs, triggered


# ---------- Main Loop ----------
def run_once(output_path: Optional[str] = None) -> None:
    header, coverage, messages, symbols = scan_once()
    print(f"\n[{header}] Scanning {coverage} stocks in ${PRICE_LO}–${PRICE_HI} (0.5% proximity)...")

    if messages:
        for msg in messages:
            print(msg)
    else:
        print("  No alerts.")

    if output_path:
        write_alert_file(output_path, datetime.now(), list(symbols), list(messages))


def run_watch(output_path: Optional[str] = None) -> None:
    seen: Set[str] = set()
    history: List[str] = []
    active_symbols: Set[str] = set()

    while True:
        header, coverage, messages, new_symbols = scan_once(seen)
        print(f"\n[{header}] Scanning {coverage} stocks in ${PRICE_LO}–${PRICE_HI} (0.5% proximity)...")

        if messages:
            for msg in messages:
                print(msg)
            history.extend(messages)
            active_symbols.update(new_symbols)
        else:
            print("  No new alerts.")

        if output_path:
            write_alert_file(output_path, datetime.now(), list(active_symbols), list(history))

        print(f"Next scan in {POLL_SECS // 60} min...")
        time.sleep(POLL_SECS)


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Continuously poll for support/resistance alerts.")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    parser.add_argument("--output", type=str, help="Write alerts and watchlist symbols to the provided file path.")
    args = parser.parse_args()

    if args.once:
        run_once(args.output)
    elif args.watch:
        run_watch(args.output)
    else:
        print("Use --watch for continuous monitoring or --once for a single scan.")

if __name__ == "__main__":
    main()
