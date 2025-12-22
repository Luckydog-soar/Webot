# strict_backtest_price_volume.py
# Strict backtest of your "Final rules" using ONLY Binance Klines (price+volume).
# OI/Funding conditions are disabled (needs extra data source).
#
# Usage:
#   pip install pandas requests python-dateutil tqdm
#   python strict_backtest_price_volume.py --trades your_trades.csv --out ./out

import argparse, os, json, time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
from dateutil import parser as dtparser
from tqdm import tqdm

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session():
    s = requests.Session()
    retry = Retry(
        total=8,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

SESSION = make_session()

BINANCE_FAPI_PRIMARY = "https://fapi.binance.com"
BINANCE_FAPI_BACKUP  = "https://fstream.binance.com"


# BINANCE_FAPI = "https://fapi.binance.com"

# ---------- helpers ----------
def to_utc_ms(x):
    # Accept: ms int, s int, ISO string
    if pd.isna(x):
        return None
    if isinstance(x, (int, float)) and x > 1e12:
        return int(x)
    if isinstance(x, (int, float)) and x > 1e10:
        return int(x)  # already ms-ish
    if isinstance(x, (int, float)) and x > 1e9:
        return int(x * 1000)  # seconds -> ms
    if isinstance(x, str):
        try:
            # If it's numeric string
            if x.isdigit():
                v = int(x)
                return v if v > 1e12 else v * 1000
        except:
            pass
        dt = dtparser.parse(x)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    # fallback
    try:
        return int(pd.to_datetime(x, utc=True).timestamp() * 1000)
    except:
        return None

def fapi_klines(symbol, interval, start_ms, end_ms, limit=1500, sleep=0.6):
    """
    Download futures klines [open_time, open, high, low, close, volume, close_time, ...]
    """
    out = []
    cur = start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": limit}
        # r = requests.get(BINANCE_FAPI + "/fapi/v1/klines", params=params, timeout=20)
        url = BINANCE_FAPI_PRIMARY + "/fapi/v1/klines"
        try:
            r = SESSION.get(url, params=params, timeout=25)
            r.raise_for_status()
        except Exception:
            # fallback to backup domain
            time.sleep(1.5)
            url2 = BINANCE_FAPI_BACKUP + "/fapi/v1/klines"
            r = SESSION.get(url2, params=params, timeout=25)
            r.raise_for_status()
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        out.extend(data)
        last_open = data[-1][0]
        # next page
        nxt = last_open + 1
        if nxt >= end_ms or len(data) < limit:
            break
        cur = nxt
        time.sleep(sleep)
    df = pd.DataFrame(out, columns=[
        "open_time","open","high","low","close","volume","close_time",
        "qav","num_trades","tbbav","tbqav","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def rolling_mean(s, n):
    return s.rolling(n, min_periods=n).mean()

@dataclass
class RuleParams:
    # thresholds
    main_vol_usdt: float = 30_000_000
    new_vol_usdt: float  = 80_000_000
    new_days: int = 7

    # triggers (price/volume only version)
    a_main_up: float = 0.055
    a_new_up: float  = 0.08
    a_main_dn: float = -0.055
    a_new_dn: float  = -0.08

    vol_mult_main: float = 3.0
    vol_mult_new: float  = 4.0

    # confirmation
    confirm_5m_bars: int = 1  # "at least 1x 5m close holds level"

    # risk management
    breakeven_r: float = 1.5
    trail_start_r: float = 3.0

# ---------- rule logic ----------
def is_breakout_4h_15m(df15, idx, direction):
    """
    Check breakout of last 4h range.
    idx is the bar index representing the signal bar (15m).
    """
    if idx < 16:  # 4h = 16 bars of 15m
        return False, None
    window = df15.iloc[idx-16:idx]  # previous 4h
    prev_high = window["high"].max()
    prev_low  = window["low"].min()
    close = df15.iloc[idx]["close"]
    if direction == "up":
        return close > prev_high, prev_high
    else:
        return close < prev_low, prev_low

def vol_spike(df15, idx, mult):
    if idx < 20:
        return False
    ma20 = df15["volume"].iloc[idx-20:idx].mean()
    return df15.iloc[idx]["volume"] >= ma20 * mult

def pct_change_15m(df15, idx):
    # Using open->close of the same bar as "15m move"
    o = df15.iloc[idx]["open"]
    c = df15.iloc[idx]["close"]
    return (c - o) / o if o else 0.0

def confirm_hold_5m(df5, level, t_ms, direction, bars=1):
    """
    After signal time t_ms, require next 'bars' 5m closes hold above level (up) or below level (down).
    """
    # find first 5m bar whose open_time >= t_ms
    sub = df5[df5["open_time"] >= t_ms].copy()
    if len(sub) < bars:
        return False
    sub = sub.iloc[:bars]
    if direction == "up":
        return (sub["close"] >= level).all()
    else:
        return (sub["close"] <= level).all()

def simulate_trade(df15, df5, entry_ms, side, level, params: RuleParams):
    """
    Simple simulation:
    - enter at next 5m open after entry_ms
    - stop at level (breakout level) +/- small buffer using 5m structure proxy:
      long stop = level (breakout) ; short stop = level
    - breakeven at +1.5R
    - trailing at +3R using 15m swing structure (proxy: last 2 15m lows/highs)
    Returns dict with pnl in R and timestamps.
    """
    direction = "up" if side == "LONG" else "down"
    # entry price: next 5m open
    nxt = df5[df5["open_time"] >= entry_ms]
    if nxt.empty:
        return {"taken": False, "reason": "no_5m_data"}
    entry_row = nxt.iloc[0]
    entry_px = entry_row["open"]

    # define stop at level (conservative)
    stop_px = level

    # avoid invalid risk
    risk = (entry_px - stop_px) if side == "LONG" else (stop_px - entry_px)
    if risk <= 0:
        return {"taken": False, "reason": "non_positive_risk"}

    be_px = entry_px + params.breakeven_r * risk if side == "LONG" else entry_px - params.breakeven_r * risk
    trail_start_px = entry_px + params.trail_start_r * risk if side == "LONG" else entry_px - params.trail_start_r * risk

    cur_stop = stop_px
    moved_be = False
    trail_on = False

    # iterate 5m bars forward for max 10 days as safety
    max_end = entry_ms + 10*24*60*60*1000
    fwd = df5[(df5["open_time"] >= entry_row["open_time"]) & (df5["open_time"] <= max_end)].copy()
    if fwd.empty:
        return {"taken": True, "entry_px": entry_px, "exit_px": entry_px, "exit_reason":"no_forward", "pnl_r":0.0}

    exit_px = None
    exit_t = None
    exit_reason = None

    # for trailing: use 15m structure proxy
    def structure_stop(t_ms):
        # Use last 4h 15m lows/highs as proxy for structure
        bar = df15[df15["open_time"] <= t_ms]
        if len(bar) < 16:
            return cur_stop
        last = bar.iloc[-16:]  # last 4h
        if side == "LONG":
            return last["low"].min()
        else:
            return last["high"].max()

    for _, r in fwd.iterrows():
        o,h,l,c = r["open"], r["high"], r["low"], r["close"]
        t = int(r["open_time"])

        # move to breakeven
        if not moved_be:
            if (side == "LONG" and h >= be_px) or (side == "SHORT" and l <= be_px):
                cur_stop = entry_px  # breakeven
                moved_be = True

        # enable trailing
        if not trail_on:
            if (side == "LONG" and h >= trail_start_px) or (side == "SHORT" and l <= trail_start_px):
                trail_on = True

        if trail_on:
            new_stop = structure_stop(t)
            # only tighten
            if side == "LONG":
                cur_stop = max(cur_stop, new_stop)
            else:
                cur_stop = min(cur_stop, new_stop)

        # stop check within bar
        if side == "LONG":
            if l <= cur_stop:
                exit_px = cur_stop
                exit_t = t
                exit_reason = "stop"
                break
        else:
            if h >= cur_stop:
                exit_px = cur_stop
                exit_t = t
                exit_reason = "stop"
                break

    if exit_px is None:
        # exit at last close
        exit_px = float(fwd.iloc[-1]["close"])
        exit_t = int(fwd.iloc[-1]["open_time"])
        exit_reason = "timeout"

    pnl = (exit_px - entry_px) if side == "LONG" else (entry_px - exit_px)
    pnl_r = pnl / risk
    return {
        "taken": True,
        "entry_px": float(entry_px),
        "exit_px": float(exit_px),
        "entry_time": int(entry_row["open_time"]),
        "exit_time": int(exit_t),
        "exit_reason": exit_reason,
        "pnl_r": float(pnl_r),
        "risk": float(risk),
        "stop_px_initial": float(stop_px),
    }

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True, help="Your trades csv from Binance (fills/positions).")
    ap.add_argument("--out", default="./out", help="Output folder")
    ap.add_argument("--assume_newdays", type=int, default=7)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    params = RuleParams(new_days=args.assume_newdays)

    df = pd.read_csv(args.trades)

    # try detect fields
    col_candidates = {
    "symbol": ["symbol"],
    "side": ["Position Side"],
    "time": ["Opened"],
    }

    def pick(col_list):
        for c in col_list:
            if c in df.columns:
                return c
        return None

    c_symbol = pick(col_candidates["symbol"])
    c_side = pick(col_candidates["side"])
    c_time = pick(col_candidates["time"])

    if not (c_symbol and c_side and c_time):
        raise SystemExit(f"Cannot detect columns. Found: {df.columns.tolist()}.\n"
                         f"Need symbol/side/time. Edit col_candidates in script.")

    # normalize
    t_ms = df[c_time].apply(to_utc_ms)
    df = df.assign(_tms=t_ms)
    df = df.dropna(subset=["_tms"])
    df["_tms"] = df["_tms"].astype("int64")
    df["_symbol"] = df[c_symbol].astype(str).str.upper()
    df["_side"] = df[c_side].astype(str).str.upper()
    # normalize side values
    df["_side"] = df["_side"].replace({"BUY":"LONG","SELL":"SHORT","LONG":"LONG","SHORT":"SHORT"})
    df = df[df["_side"].isin(["LONG","SHORT"])].copy()

    # We'll backtest each trade as if the rule decides to trade at that time.
    results = []
    cache15, cache5 = {}, {}

    # pre-estimate "new coin": we can't know listing time from klines alone reliably; we approximate:
    # if earliest available 15m kline is within new_days from trade time => treat as new
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Backtesting"):
        sym = row["_symbol"]
        side = row["_side"]
        tm = int(row["_tms"])

        # get klines window: need 4h before + 2 days after for trailing
        start = tm - 8*60*60*1000   # 8h before
        end   = tm + 3*24*60*60*1000  # 3 days after

        key15 = (sym, start//(60*60*1000), end//(60*60*1000))
        # cache per symbol with broader range to reduce calls
        if sym not in cache15:
            cache15[sym] = fapi_klines(sym, "15m", tm - 30*24*60*60*1000, tm + 5*24*60*60*1000)  # 30d back, 5d fwd
        if sym not in cache5:
            cache5[sym] = fapi_klines(sym, "5m", tm - 7*24*60*60*1000, tm + 5*24*60*60*1000)     # 7d back, 5d fwd

        df15 = cache15[sym]
        df5  = cache5[sym]
        if df15.empty or df5.empty:
            results.append({"symbol":sym,"side":side,"tms":tm,"rule_take":False,"reason":"no_klines"})
            continue

        # determine "new coin" proxy
        first15 = int(df15.iloc[0]["open_time"])
        is_new = (tm - first15) <= params.new_days * 24*60*60*1000

        # locate 15m bar containing tm
        idx_candidates = df15.index[(df15["open_time"] <= tm) & (df15["close_time"] > tm)]
        if len(idx_candidates)==0:
            results.append({"symbol":sym,"side":side,"tms":tm,"rule_take":False,"reason":"no_15m_bar"})
            continue
        idx = int(idx_candidates[0])

        # trigger check
        mult = params.vol_mult_new if is_new else params.vol_mult_main
        move = pct_change_15m(df15, idx)

        if side == "LONG":
            th = params.a_new_up if is_new else params.a_main_up
            ok_move = move >= th
            ok_break, level = is_breakout_4h_15m(df15, idx, "up")
        else:
            th = params.a_new_dn if is_new else params.a_main_dn
            ok_move = move <= th
            ok_break, level = is_breakout_4h_15m(df15, idx, "down")

        ok_vol = vol_spike(df15, idx, mult)

        if not (ok_move and ok_break and ok_vol and level is not None):
            results.append({
                "symbol":sym,"side":side,"tms":tm,"is_new":is_new,
                "rule_take":False,"reason":"no_trigger",
                "move15m":move,"ok_move":ok_move,"ok_break":ok_break,"ok_vol":ok_vol
            })
            continue

        # confirmation: next N 5m closes hold above/below breakout level
        confirm = confirm_hold_5m(df5, level, int(df15.iloc[idx]["close_time"]), "up" if side=="LONG" else "down",
                                  bars=params.confirm_5m_bars)
        if not confirm:
            results.append({
                "symbol":sym,"side":side,"tms":tm,"is_new":is_new,
                "rule_take":False,"reason":"no_confirm",
                "move15m":move,"break_level":level
            })
            continue

        # simulate trade
        sim = simulate_trade(df15, df5, int(df15.iloc[idx]["close_time"]), side, level, params)
        results.append({
            "symbol":sym,"side":side,"tms":tm,"is_new":is_new,
            "rule_take":sim.get("taken", False),
            "reason":sim.get("reason","ok"),
            "move15m":move,
            "break_level":level,
            **{k:v for k,v in sim.items() if k!="reason"}
        })

    out_csv = os.path.join(args.out, "trade_level_backtest.csv")
    pd.DataFrame(results).to_csv(out_csv, index=False, encoding="utf-8-sig")

    # summary
    res = pd.DataFrame(results)
    taken = res[res["rule_take"]==True].copy()
    summary = {
        "total_trades_in_file": int(len(res)),
        "trades_taken_by_rule": int(len(taken)),
        "take_rate": float(len(taken) / max(1,len(res))),
    }
    if len(taken)>0 and "pnl_r" in taken.columns:
        wins = taken[taken["pnl_r"]>0]
        losses = taken[taken["pnl_r"]<=0]
        summary.update({
            "win_rate": float(len(wins) / len(taken)),
            "avg_r": float(taken["pnl_r"].mean()),
            "median_r": float(taken["pnl_r"].median()),
            "profit_factor_r": float(wins["pnl_r"].sum() / max(1e-9, abs(losses["pnl_r"].sum()))),
            "max_r": float(taken["pnl_r"].max()),
            "min_r": float(taken["pnl_r"].min()),
        })
    out_json = os.path.join(args.out, "summary.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("DONE")
    print("trade-level:", out_csv)
    print("summary:", out_json)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
