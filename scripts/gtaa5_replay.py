#!/usr/bin/env python3
"""
GTAA-5 (Faber, SSRN 962461) independent replay on ETF daily data.

Research only. No trading, no broker access, no keys, no .env.
Method and gate were pre-registered in docs/GTAA5_REPLAY_2026-10-30.md section 0
BEFORE any data was computed. This script implements exactly that method.

Usage:
    .venv/bin/python scripts/gtaa5_replay.py [--out RESULTS_SECTION.md]
Optional env GTAA5_CACHE_DIR: pickle the downloaded frames there (outside the repo)
so reruns do not hit the network.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

ASSETS = ["SPY", "EFA", "AGG", "VNQ", "DBC"]
NA = len(ASSETS)
BASE_DATE = pd.Timestamp("2006-12-29")   # equity = 1.0 at this close
W13_DATE = pd.Timestamp("2012-12-31")    # 2013-01 window starts from this close
SLEEVE = 0.20
COST_BP = 10.0
DL_START = "2005-01-01"
ET = "America/New_York"
LAST_MONTH_COMPLETE = True   # set in main(); controls whether the final month is treated as a full month


# --------------------------------------------------------------------------- data
def _dl(auto_adjust: bool) -> pd.DataFrame:
    last = None
    for k in range(4):
        try:
            d = yf.download(ASSETS + ["^IRX"], start=DL_START, auto_adjust=auto_adjust,
                            progress=False, threads=False)
            c = d["Close"].copy()
            if len(c) > 1000:
                return c
        except Exception as e:  # noqa
            last = e
        time.sleep(3 * (k + 1))
    raise RuntimeError(f"yfinance download failed: {last}")


def load_frames(notes: list[str]):
    cdir = os.environ.get("GTAA5_CACHE_DIR")
    if cdir:
        p = os.path.join(cdir, "frames.pkl")
        if os.path.exists(p):
            with open(p, "rb") as f:
                adj, raw, fetched = pickle.load(f)
            notes.append(f"Frames loaded from cache {p} (originally fetched {fetched.strftime('%Y-%m-%d %H:%M ET')}).")
            return adj, raw, fetched
    fetched = pd.Timestamp.now(tz=ET)
    adj = _dl(True)
    raw = _dl(False)
    if cdir:
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "frames.pkl"), "wb") as f:
            pickle.dump((adj, raw, fetched), f)
    return adj, raw, fetched


def drop_partial(df: pd.DataFrame, now: pd.Timestamp, notes: list[str]) -> pd.DataFrame:
    """now = the clock at FETCH time (ET). A bar dated today (ET) fetched before 16:05 ET is partial."""
    last = df.index[-1]
    if last.date() == now.date() and (now.hour, now.minute) < (16, 5):
        notes.append(f"Dropped partial in-session bar dated {last.date()} (US market still open at fetch time "
                     f"{now.strftime('%H:%M ET')}).")
        return df.iloc[:-1]
    return df


def fetch_dtb3(notes: list[str]):
    try:
        import requests
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3", timeout=30)
        r.raise_for_status()
        t = pd.read_csv(io.StringIO(r.text))
        dc = t.columns[0]
        t[dc] = pd.to_datetime(t[dc])
        s = pd.to_numeric(t["DTB3"], errors="coerce")
        s.index = t[dc]
        s = s.dropna()
        notes.append(f"FRED DTB3 fetched: {s.index[0].date()} .. {s.index[-1].date()}, {len(s)} obs.")
        return s
    except Exception as e:  # noqa
        notes.append(f"FRED DTB3 NOT available ({type(e).__name__}: {str(e)[:120]}); cross-check skipped.")
        return None


# ------------------------------------------------------------------ calendar helpers
def month_end_dates(idx: pd.DatetimeIndex, now: pd.Timestamp, notes: list[str]):
    """Last trading day of each COMPLETED calendar month in idx. Returns (DatetimeIndex, last_month_complete).
    The final month counts as complete if the last row is the last weekday of that month, or if the
    fetch-time clock (ET) is already in a later month."""
    s = pd.Series(idx, index=idx)
    me = s.groupby(idx.to_period("M")).last()
    last = idx[-1]
    last_period = last.to_period("M")
    eom = (last + pd.offsets.BMonthEnd(0)).normalize()
    complete = bool(last.normalize() >= eom) or (pd.Period(now.strftime("%Y-%m"), "M") > last_period)
    if not complete:
        notes.append(f"Final month {last_period} incomplete (last completed session {last.date()}); its month-end "
                     f"signal is not yet due and is excluded.")
        me = me.iloc[:-1]
    else:
        notes.append(f"Final month {last_period} is complete (last session {last.date()} is its last trading day), "
                     f"so its month-end signal IS included; trades for it execute at that close and are costed.")
    return pd.DatetimeIndex(me.values), complete


def cash_returns(irx: pd.Series, dates: pd.DatetimeIndex, mode="irx", dtb3=None, fixed=None) -> np.ndarray:
    """Daily cash return on interval (t-1 -> t): prior-day annual % /100 * calendar days / 365."""
    if mode == "fixed":
        ann = pd.Series(fixed, index=dates, dtype=float)
    elif mode == "dtb3":
        ann = dtb3.reindex(dates.union(dtb3.index)).ffill().reindex(dates)
    else:
        ann = irx.reindex(dates.union(irx.index)).ffill().reindex(dates)
    pct = ann.values / 100.0 if mode != "fixed" else ann.values
    cal = np.diff(dates.values).astype("timedelta64[D]").astype(float)
    r = np.zeros(len(dates))
    r[1:] = pct[:-1] * cal / 365.0
    return r


# ------------------------------------------------------------------------ decisions
def monthend_decisions(me_px: pd.DataFrame, flag_px: pd.DataFrame | None = None):
    """flag_px: price table used for the SIGNAL (defaults to me_px). Returns dict date->flags."""
    sp = me_px if flag_px is None else flag_px
    sma = sp.rolling(10).mean()
    valid = sma.notna().all(axis=1)
    f = (sp > sma)
    dec = {}
    for d in sp.index[valid.values]:
        dec[d] = f.loc[d, ASSETS].values.astype(bool)
    return dec


def dayd_decisions(adj: pd.DataFrame, me_px: pd.DataFrame, d: int, mode: str, last_date):
    """Decision on first trading day with calendar day >= d, each month from 2006-12."""
    idx = adj.index
    dec = {}
    for per in pd.period_range("2006-12", last_date.to_period("M"), freq="M"):
        days = idx[(idx.to_period("M") == per) & (idx.day >= d)]
        if len(days) == 0:
            continue
        dd = days[0]
        prior = me_px[me_px.index < per.start_time]
        n = 10 if mode == "primary" else 9
        ref = prior.tail(n)
        if len(ref) < n or ref.isna().any().any():
            continue
        flags = (adj.loc[dd, ASSETS] > ref.mean()).values.astype(bool)
        dec[dd] = flags
    return dec


# ------------------------------------------------------------------------- engine
def _rebal(hold, cash, fnew, fold, bp, mode):
    V = hold.sum() + cash
    tgt = SLEEVE * V * fnew.astype(float)
    trade = np.abs(tgt - hold)
    sw = fnew != fold
    traded_for_cost = trade.sum() if mode == "full" else trade[sw].sum()
    cost = bp / 1e4 * traded_for_cost
    tgt = tgt * ((V - cost) / V)
    return tgt, V - cost - tgt.sum(), cost, trade.sum(), int(sw.sum())


def simulate(adj, cash_r, dates, decisions, bp=COST_BP, mode="full"):
    """Strategy engine. decisions: dict date->flags (ON). Init flags = latest decision <= BASE_DATE."""
    ret = adj[ASSETS].reindex(dates).pct_change().values
    keys = sorted(decisions)
    pre = [k for k in keys if k <= dates[0]]
    if not pre:
        raise RuntimeError("no decision on/before base date")
    init = decisions[pre[-1]]
    later = {k: decisions[k] for k in keys if k > dates[0]}
    n = len(dates)
    eq = np.zeros(n); expo = np.zeros(n); flg = np.zeros((n, NA), bool)
    costs = np.zeros(n); traded = np.zeros(n); sws = np.zeros(n, int)
    hold = np.zeros(NA); cash = 1.0
    flags = np.zeros(NA, bool)
    for i, t in enumerate(dates):
        if i > 0:
            if np.isnan(ret[i]).any():
                raise RuntimeError(f"NaN return on {t}")
            hold = hold * (1.0 + ret[i]); cash = cash * (1.0 + cash_r[i])
        if i == 0:
            hold, cash, c, tr, _ = _rebal(hold, cash, init, flags, bp, mode)
            flags = init.copy(); costs[i] = c; traded[i] = tr
        elif t in later:
            fn = later[t]
            hold, cash, c, tr, s = _rebal(hold, cash, fn, flags, bp, mode)
            flags = fn.copy(); costs[i] = c; traded[i] = tr; sws[i] = s
        V = hold.sum() + cash
        eq[i] = V; expo[i] = hold.sum() / V; flg[i] = flags
    return pd.DataFrame({"eq": eq, "expo": expo, "cost": costs, "traded": traded, "sw": sws},
                        index=dates), pd.DataFrame(flg, index=dates, columns=ASSETS)


def sim_6040(adj, cash_r, dates, me_dates, bp=COST_BP):
    ret = adj[["SPY", "AGG"]].reindex(dates).pct_change().values
    tw = np.array([0.6, 0.4])
    hold = np.zeros(2); cash = 1.0
    eq = np.zeros(len(dates)); cost_tot = 0.0
    mset = set(me_dates)
    for i, t in enumerate(dates):
        if i > 0:
            hold = hold * (1 + ret[i])
        V = hold.sum() + (cash if i == 0 else 0.0)
        if i == 0 or t in mset:
            tgt = tw * V
            c = bp / 1e4 * np.abs(tgt - hold).sum()
            hold = tw * (V - c); cost_tot += c
        eq[i] = hold.sum()
    return pd.Series(eq, index=dates), cost_tot


def sim_spy_bh(adj, dates, bp=COST_BP):
    p = adj["SPY"].reindex(dates)
    return (p / p.iloc[0]) * (1 - bp / 1e4)


# ------------------------------------------------------------------------- stats
def cagr(eq: pd.Series, start: pd.Timestamp) -> float:
    s = eq.loc[start:]
    days = (s.index[-1] - s.index[0]).days
    return (s.iloc[-1] / s.iloc[0]) ** (365.25 / days) - 1.0


def maxdd(eq: pd.Series, start: pd.Timestamp):
    s = eq.loc[start:]
    peak = s.cummax()
    dd = s / peak - 1.0
    tr = dd.idxmin()
    pk = s.loc[:tr].idxmax()
    rec = s.loc[tr:][s.loc[tr:] >= s.loc[pk]]
    return dd.min(), pk, tr, (rec.index[0] if len(rec) else None), dd


def longest_underwater_years(dd: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp]:
    under = dd < -1e-12
    best = (0.0, None, None)
    start = None
    prev = None
    for t, u in under.items():
        if u and start is None:
            start = prev if prev is not None else t
        if (not u) and start is not None:
            yrs = (t - start).days / 365.25
            if yrs > best[0]:
                best = (yrs, start, t)
            start = None
        prev = t
    if start is not None:
        yrs = (dd.index[-1] - start).days / 365.25
        if yrs > best[0]:
            best = (yrs, start, dd.index[-1])
    return best


def full_stats(eq: pd.Series, start: pd.Timestamp):
    s = eq.loc[start:]
    r = s.pct_change().dropna()
    m, pk, tr, rec, dd = maxdd(eq, start)
    mret = s.resample("ME").last().pct_change().dropna()
    if not LAST_MONTH_COMPLETE:
        mret = mret.iloc[:-1]          # drop month-to-date stub from 'worst month' statistics
    return dict(cagr=cagr(eq, start), vol=r.std() * np.sqrt(252), maxdd=m, pk=pk, tr=tr, rec=rec, dd=dd,
                worst_day=r.min(), worst_day_date=r.idxmin(), mret=mret, end=s.iloc[-1],
                nyears=(s.index[-1] - s.index[0]).days / 365.25)


def yearly(eq: pd.Series) -> pd.Series:
    ye = eq.groupby(eq.index.year).last()
    return (ye / ye.shift(1) - 1.0).dropna()


def pct(x, d=2):
    return f"{100 * x:.{d}f}%"


def md_table(header, rows):
    o = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        o.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(o)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--asof", default=None, help="truncate data at this date (sensitivity only)")
    a = ap.parse_args()
    notes: list[str] = []
    out: list[str] = []
    P = out.append

    adj0, raw0, fetched_ts = load_frames(notes)
    fetched = fetched_ts.strftime("%Y-%m-%d %H:%M ET")
    if a.asof:
        adj0 = adj0.loc[:a.asof]
        notes.append(f"SENSITIVITY RUN: data truncated at --asof {a.asof}.")
        now_clock = pd.Timestamp(a.asof + " 20:00", tz=ET)
    else:
        adj0 = drop_partial(adj0, fetched_ts, notes)
        now_clock = fetched_ts
    raw0 = raw0.loc[adj0.index.intersection(raw0.index)]
    px = adj0[ASSETS]
    irx = adj0["^IRX"]
    last_date = px.index[-1]
    me_dates, last_month_complete = month_end_dates(px.index, now_clock, notes)
    global LAST_MONTH_COMPLETE
    LAST_MONTH_COMPLETE = last_month_complete

    # ---------- data audit
    P("## 1. Data audit\n")
    P(f"- Source: yfinance {yf.__version__}, `auto_adjust=True`, fetched {fetched}. Window requested from {DL_START}.")
    aud = []
    for t in ASSETS + ["^IRX"]:
        s = adj0[t]
        f = s.first_valid_index()
        n_int = int(s.loc[f:].isna().sum())
        aud.append([t, f.date(), s.last_valid_index().date(), int(s.notna().sum()), n_int])
    P("")
    P(md_table(["Series", "First", "Last", "Rows", "Internal NaN"], aud))
    P("")
    cal_all = px.dropna().index
    cal_all_post = cal_all[cal_all >= BASE_DATE]
    cal_set = set(cal_all)
    miss = [d for d in px.index[px.index >= BASE_DATE] if d not in cal_set]
    P(f"- Latest completed session used: **{last_date.date()}**. Common calendar from {BASE_DATE.date()}: "
      f"{len(cal_all_post)} sessions; sessions where some ETF is missing after the base date: {len(miss)}.")
    gaps = pd.Series(px.index[px.index >= BASE_DATE]).diff().dt.days.dropna()
    P(f"- Largest gap between consecutive sessions after base date: {int(gaps.max())} calendar days "
      f"(ending {pd.Series(px.index[px.index >= BASE_DATE]).iloc[gaps.idxmax()].date()}).")
    ii = irx.dropna()
    P(f"- ^IRX: min {ii.min():.3f}, max {ii.max():.3f}, mean {ii.mean():.3f} (annual %); days with exactly 0.000: "
      f"{int((ii == 0).sum())}; negative prints: {int((ii < 0).sum())}"
      + (f" (first {ii[ii < 0].index[0].date()}, last {ii[ii < 0].index[-1].date()}, used as-is)" if (ii < 0).any() else "")
      + f"; NaN (bond-market holidays, forward-filled): {int(irx.isna().sum())}.")
    dbc0 = px["DBC"].first_valid_index()
    P(f"- DBC first bar in yfinance: {dbc0.date()}. First month-end with a full 10-month DBC window: "
      f"{px['DBC'].dropna().groupby(px['DBC'].dropna().index.to_period('M')).last().index[9]}"
      f" (so the 2006-12-29 base date is feasible without any pre-inception data).")
    fp = hashlib.sha256(np.round(px.loc[BASE_DATE:].values, 5).tobytes()).hexdigest()[:16]
    P(f"- Data fingerprint (sha256 of adjusted closes {BASE_DATE.date()}..{last_date.date()}, rounded 5dp, first 16 hex): `{fp}`. "
      f"yfinance back-adjusts history at download time, so this fingerprint can drift on later runs.")
    for n_ in notes:
        P(f"- {n_}")
    P("")

    dates = cal_all_post
    # month-ends (complete months only), month-end closes per asset from each asset's own history
    me_px = px.loc[me_dates]
    me_raw = raw0.loc[me_dates, ASSETS]
    dec_me = monthend_decisions(me_px)
    # sanity: DBC cannot signal before Nov 2006
    first_sig = min(dec_me)
    P(f"- First month-end with a valid 10-month SMA for all five assets: {first_sig.date()}. "
      f"Month-end decisions available: {len(dec_me)}; those after the base date: {sum(1 for k in dec_me if k > BASE_DATE)}.\n")

    notes2: list[str] = []
    dtb3 = fetch_dtb3(notes2)
    cr_irx = cash_returns(irx, dates)
    sim, flg = simulate(px, cr_irx, dates, dec_me)
    eq = sim["eq"]

    # ---------- headline
    s07 = full_stats(eq, BASE_DATE)
    s13 = full_stats(eq, W13_DATE)
    spy_eq = sim_spy_bh(px, dates)
    eq6040, c6040 = sim_6040(px, cr_irx, dates, me_dates[me_dates > BASE_DATE])
    b = {"SPY buy-hold": spy_eq, "60/40 SPY/AGG": eq6040}
    bs07 = {k: full_stats(v, BASE_DATE) for k, v in b.items()}
    bs13 = {k: full_stats(v, W13_DATE) for k, v in b.items()}
    yr = yearly(eq)
    yr_b = {k: yearly(v) for k, v in b.items()}

    tb_eq = pd.Series(np.cumprod(1.0 + cr_irx), index=dates)
    tb07 = full_stats(tb_eq, BASE_DATE); tb13 = full_stats(tb_eq, W13_DATE)
    P("## 2. Primary result: GTAA-5, month-end signal, 10 bp on all ETF trades, cash = ^IRX\n")
    rows = []
    for name, s0, s1 in [("GTAA-5 (primary)", s07, s13)] + [(k, bs07[k], bs13[k]) for k in b] + [("T-bills only (^IRX accrual)", tb07, tb13)]:
        rows.append([name, pct(s0["cagr"]), pct(s1["cagr"]), pct(s0["vol"]), pct(s0["maxdd"]),
                     f"{s0['pk'].date()} to {s0['tr'].date()}", pct(s0["worst_day"]) + f" ({s0['worst_day_date'].date()})",
                     pct(s0["mret"].min()) + f" ({s0['mret'].idxmin().strftime('%Y-%m')})"])
    P(md_table(["Strategy", "CAGR 2007-01..latest", "CAGR 2013-01..latest", "Ann. vol", "Max DD (daily close)",
                "DD peak to trough", "Worst day", "Worst month"], rows))
    P("")
    exc = (eq.pct_change().iloc[1:] - cr_irx[1:])
    P(f"- Excess over T-bills: GTAA-5 CAGR minus T-bill CAGR = {100 * (s07['cagr'] - tb07['cagr']):.2f} points (2007+), "
      f"{100 * (s13['cagr'] - tb13['cagr']):.2f} points (2013+). Sharpe vs T-bills (daily excess, annualised): GTAA-5 {exc.mean() / exc.std() * np.sqrt(252):.2f}; "
      f"SPY {((spy_eq.pct_change().iloc[1:] - cr_irx[1:]).mean() / (spy_eq.pct_change().iloc[1:] - cr_irx[1:]).std() * np.sqrt(252)):.2f}; "
      f"60/40 {((eq6040.pct_change().iloc[1:] - cr_irx[1:]).mean() / (eq6040.pct_change().iloc[1:] - cr_irx[1:]).std() * np.sqrt(252)):.2f}. "
      f"Last ^IRX print ({irx.dropna().index[-1].date()}): {irx.dropna().iloc[-1]:.3f}%.")
    P(f"- Window 2007: {BASE_DATE.date()} to {last_date.date()} = {s07['nyears']:.2f} years. Window 2013: {W13_DATE.date()} to "
      f"{last_date.date()} = {s13['nyears']:.2f} years. Terminal value of 1.00: GTAA-5 {s07['end']:.3f}, "
      f"SPY {bs07['SPY buy-hold']['end']:.3f}, 60/40 {bs07['60/40 SPY/AGG']['end']:.3f}.")
    P(f"- Max-DD recovery date (GTAA-5): {s07['rec'].date() if s07['rec'] is not None else 'not yet recovered'}. "
      f"Max DD of 2013-onward window: {pct(s13['maxdd'])} ({s13['pk'].date()} to {s13['tr'].date()}).")
    uw = longest_underwater_years(s07["dd"])
    P(f"- Longest time below a prior equity high: {uw[0]:.2f} years ({uw[1].date()} to {uw[2].date()}).")
    se = s07["vol"] / np.sqrt(s07["nyears"])
    P(f"- Sampling noise: with {s07['nyears']:.1f} years and {pct(s07['vol'])} vol, the standard error of the mean annual "
      f"return is about {100 * se:.2f} points, so a CAGR gap of under ~{100 * se:.1f} points vs any threshold is statistically indistinguishable.")
    tfrac = (sim["traded"] / sim["eq"]).iloc[1:]      # one-way traded notional as a fraction of equity at the time
    P(f"- Turnover/cost (primary, 10 bp on all ETF trades): one-way traded notional averages {pct(tfrac.sum() / s07['nyears'], 0)} "
      f"of equity per year (includes monthly drift rebalancing of unchanged sleeves), a cost drag of about "
      f"{100 * tfrac.sum() / s07['nyears'] * COST_BP / 1e4:.2f} points/yr (check: 0 bp vs 10 bp CAGR gap is in section 4).\n")

    # worst-case intra-claim checks
    dd = s07["dd"]
    oct08 = dd.loc["2008-10-01":"2008-10-31"]
    y08 = dd.loc["2008-01-01":"2008-12-31"]
    P("### 2.1 Checks against the researcher's specific numbers\n")
    P(f"- Drawdown at end Oct 2008: {pct(dd.loc[:'2008-10-31'].iloc[-1])}; min drawdown inside Oct 2008: {pct(oct08.min())} "
      f"({oct08.idxmin().date()}); min drawdown inside 2008: {pct(y08.min())} ({y08.idxmin().date()}). "
      f"Researcher claimed -16.1% (Oct 2008).")
    dd22 = dd.loc["2022-01-01":"2022-12-31"]
    e22 = eq.loc["2022-01-01":"2022-12-31"]
    dd22_within = (e22 / e22.cummax() - 1)
    P(f"- 2022: calendar return {pct(yr.loc[2022])}; max drawdown vs all-time high during 2022 {pct(dd22.min())} ({dd22.idxmin().date()}); "
      f"max drawdown measured only inside 2022 {pct(dd22_within.min())} ({dd22_within.idxmin().date()}).")
    P(f"- Decisions per year: 12 (one per month-end); number of month-end decisions after base: "
      f"{sum(1 for k in dec_me if k > BASE_DATE)}.\n")

    # ---------- calendar-year table
    P("## 3. Calendar-year returns, switches, exposure\n")
    sw_year = sim["sw"].groupby(sim.index.year).sum()
    held = sim["expo"].shift(1).dropna()
    expo_year = held.groupby(held.index.year).mean()
    rows = []
    for y in yr.index:
        rows.append([y + 0, pct(yr.loc[y]), pct(yr_b["SPY buy-hold"].loc[y]), pct(yr_b["60/40 SPY/AGG"].loc[y]),
                     int(sw_year.get(y, 0)), pct(expo_year.get(y, np.nan), 1)])
    P(md_table(["Year", "GTAA-5", "SPY", "60/40", "ON/OFF switches (sleeve changes)", "Avg ETF exposure"], rows))
    P(f"\n(2026 is year-to-date through {last_date.date()}. 2007 is measured from the {BASE_DATE.date()} close.)\n")
    full_years = [y for y in yr.index if y < 2026]
    P(f"- Switches per year (sleeve ON/OFF changes), complete years 2007-2025: mean {sw_year.loc[full_years].mean():.2f}, "
      f"median {sw_year.loc[full_years].median():.1f}, max {int(sw_year.loc[full_years].max())} "
      f"({int(sw_year.loc[full_years].idxmax())}), min {int(sw_year.loc[full_years].min())}. Total over run: {int(sim['sw'].sum())}.")
    P(f"- Exposure fraction (time-weighted share of equity held in the five ETFs, positions carried into each day): "
      f"{pct(held.mean(), 1)}. Share of days each sleeve is ON: "
      + ", ".join(f"{a} {pct(flg[a].shift(1).dropna().mean(), 1)}" for a in ASSETS) + ".")
    P(f"- Worst calendar year: {int(yr.idxmin())} at {pct(yr.min())}. Best: {int(yr.idxmax())} at {pct(yr.max())}. "
      f"Negative years: {int((yr < 0).sum())} of {len(yr)}.\n")

    # ---------- sensitivities
    P("## 4. Sensitivities on the month-end strategy (NOT used for the verdict)\n")
    def run_variant(label, cr=None, bp=COST_BP, mode="full", decisions=None):
        cr = cr_irx if cr is None else cr
        d = dec_me if decisions is None else decisions
        s_, f_ = simulate(px, cr, dates, d, bp=bp, mode=mode)
        e_ = s_["eq"]
        a0 = full_stats(e_, BASE_DATE); a1 = full_stats(e_, W13_DATE)
        y_ = yearly(e_)
        return [label, pct(a0["cagr"]), pct(a1["cagr"]), pct(a0["maxdd"]), pct(y_.loc[2022]), pct(a0["vol"])], e_

    sens = []
    for lab, kw in [("PRIMARY: 10 bp all ETF trades, ^IRX", {}),
                    ("10 bp on ON/OFF switches only, ^IRX", dict(mode="switch")),
                    ("0 bp costs, ^IRX", dict(bp=0.0)),
                    ("20 bp all ETF trades, ^IRX", dict(bp=20.0))]:
        sens.append(run_variant(lab, **kw)[0])
    cash_rows = []
    cash_rows.append(run_variant("cash 0%", cr=cash_returns(irx, dates, "fixed", fixed=0.0))[0])
    cash_rows.append(run_variant("cash 2% flat", cr=cash_returns(irx, dates, "fixed", fixed=0.02))[0])
    if dtb3 is not None:
        cash_rows.append(run_variant("cash = FRED DTB3 (cross-check)", cr=cash_returns(irx, dates, "dtb3", dtb3=dtb3))[0])
    hdr = ["Variant", "CAGR 2007+", "CAGR 2013+", "Max DD", "2022", "Vol"]
    P(md_table(hdr, sens + cash_rows))
    P("")
    for n_ in notes2:
        P(f"- {n_}")
    P("")

    # ---------- raw-close signal check
    P("## 5. Adjusted vs unadjusted prices for the signal (live-implementation risk)\n")
    me_raw_px = me_raw.copy()
    dec_raw = monthend_decisions(me_px, flag_px=me_raw_px)
    keys = [k for k in dec_me if k >= BASE_DATE and k in dec_raw]
    diff = np.array([dec_me[k] != dec_raw[k] for k in keys])
    P(f"- Signal built on raw `Close` (split-adjusted, NOT dividend-adjusted) vs adjusted close: "
      f"{int(diff.sum())} differing asset-month flags out of {diff.size} ({pct(diff.mean(), 1)}). Per asset: "
      + ", ".join(f"{a} {int(diff[:, i].sum())}" for i, a in enumerate(ASSETS)) + ".")
    # run strategy with raw-signal flags, adjusted returns
    dec_raw2 = {k: dec_raw[k] for k in dec_raw}
    s_raw, _ = simulate(px, cr_irx, dates, dec_raw2)
    r0 = full_stats(s_raw["eq"], BASE_DATE); r1 = full_stats(s_raw["eq"], W13_DATE)
    P(f"- Strategy with raw-close signals (returns still adjusted): CAGR 2007+ {pct(r0['cagr'])}, 2013+ {pct(r1['cagr'])}, "
      f"max DD {pct(r0['maxdd'])}, 2022 {pct(yearly(s_raw['eq']).loc[2022])}. "
      f"A live bot must feed total-return (dividend-adjusted) prices to the SMA or it will deviate, mostly on AGG/VNQ.\n")

    # ---------- rebalance-day sweep
    P("## 6. Rebalance-day sweep\n")
    P("Definitions (pre-registered): decision on the first trading day on or after calendar day d, decided and traded at that day's close. "
      "PRIMARY: ON if that day's close > mean of the 10 most recent completed month-end adjusted closes (prior month-end and the 9 before). "
      "SECONDARY: ON if close > mean of the 9 most recent completed month-ends (algebraically identical to 'mean of {today's close + 9 prior month-ends} < today's close'). "
      "All variants start at the 2006-12-29 close in the state set by their latest decision on or before that date, 10 bp on all ETF trades, cash = ^IRX.\n")
    sweep = {}
    for mode in ["primary", "secondary"]:
        rows = []
        for d in [1, 10, 20]:
            dec = dayd_decisions(px, me_px, d, mode, last_date)
            s_, f_ = simulate(px, cr_irx, dates, dec)
            e_ = s_["eq"]
            a0 = full_stats(e_, BASE_DATE); a1 = full_stats(e_, W13_DATE)
            sweep[(mode, d)] = (a0, a1, e_, s_)
            rows.append([f"day {d}", pct(a0["cagr"]), pct(a1["cagr"]), pct(a0["maxdd"]),
                         f"{a0['pk'].date()} to {a0['tr'].date()}", pct(yearly(e_).loc[2022]),
                         f"{s_['sw'].sum() / a0['nyears']:.2f}", f"{sum(1 for k in dec if k > BASE_DATE)}"])
        rows.append(["month-end (ref, not gated)", pct(s07["cagr"]), pct(s13["cagr"]), pct(s07["maxdd"]),
                     f"{s07['pk'].date()} to {s07['tr'].date()}", pct(yr.loc[2022]),
                     f"{sim['sw'].sum() / s07['nyears']:.2f}", f"{sum(1 for k in dec_me if k > BASE_DATE)}"])
        P(f"**{mode.upper()} definition**\n")
        P(md_table(["Rebalance day", "CAGR 2007+", "CAGR 2013+", "Max DD", "DD peak to trough", "2022", "Switches/yr", "# decisions"], rows))
        c07 = [sweep[(mode, d)][0]["cagr"] for d in [1, 10, 20]]
        c13 = [sweep[(mode, d)][1]["cagr"] for d in [1, 10, 20]]
        c07_4 = c07 + [s07["cagr"]]
        P(f"\n- Spread across d = 1, 10, 20 (max - min): 2007+ **{100 * (max(c07) - min(c07)):.2f} points**; 2013+ {100 * (max(c13) - min(c13)):.2f} points. "
          f"Including month-end as a fourth point: {100 * (max(c07_4) - min(c07_4)):.2f} points (2007+).\n")
    sp_primary = 100 * (max(sweep[("primary", d)][0]["cagr"] for d in [1, 10, 20]) -
                        min(sweep[("primary", d)][0]["cagr"] for d in [1, 10, 20]))
    sp_secondary = 100 * (max(sweep[("secondary", d)][0]["cagr"] for d in [1, 10, 20]) -
                          min(sweep[("secondary", d)][0]["cagr"] for d in [1, 10, 20]))

    dd_all = [sweep[(m_, d)][0]["maxdd"] for m_ in ["primary", "secondary"] for d in [1, 10, 20]] + [s07["maxdd"]]
    n_dd_fail = sum(1 for x in dd_all if x < -0.18)
    P(f"- Drawdown-timing sensitivity: across the 7 timing variants above (month-end, plus days 1/10/20 under both definitions) "
      f"the 2007+ max daily-close drawdown ranges from {pct(max(dd_all))} to {pct(min(dd_all))}; "
      f"{n_dd_fail} of 7 breach the -18% gate level. The gate line is evaluated on the month-end strategy as specified, "
      f"but this shows the -18% margin is inside the rebalance-timing luck range.\n")

    # ---------- gate
    P("## 7. Gate evaluation (exactly as pre-registered)\n")
    g = []
    def line(name, thr, val, ok):
        g.append([name, thr, val, "PASS" if ok else "FAIL"])
    line("CAGR 2007-01 to latest", ">= 4.5%", pct(s07["cagr"]), s07["cagr"] >= 0.045)
    line("CAGR 2013-01 to latest", ">= 4.0%", pct(s13["cagr"]), s13["cagr"] >= 0.040)
    line("Max daily-close drawdown (full run)", ">= -18%", pct(s07["maxdd"]), s07["maxdd"] >= -0.18)
    line("Calendar-2022 return", ">= -12%", pct(yr.loc[2022]), yr.loc[2022] >= -0.12)
    line("CAGR spread over rebalance days 1/10/20 (primary def., 2007+)", "<= 2.0 points", f"{sp_primary:.2f} points", sp_primary <= 2.0)
    P(md_table(["Gate line", "Threshold", "Measured", "Result"], g))
    allok = all(r[3] == "PASS" for r in g)
    P(f"\n**VERDICT: {'PASS' if allok else 'FAIL'}** "
      f"({sum(r[3] == 'PASS' for r in g)} of 5 lines met). Secondary-definition spread, for information: {sp_secondary:.2f} points "
      f"({'within' if sp_secondary <= 2.0 else 'outside'} the 2.0 limit).\n")

    # ---------- execution-lag sensitivity
    P("## 8. Execution-lag sensitivity (signal at the close, trade at the NEXT session's close)\n")
    def lag_dec(dec, k=1):
        o = {}
        for d_, f_ in dec.items():
            pos = px.index.get_loc(d_)
            if pos + k < len(px.index):
                o[px.index[pos + k]] = f_
        return o
    rows = []
    for lab, dec in [("month-end", dec_me)] + [(f"day {d} (primary def.)", dayd_decisions(px, me_px, d, "primary", last_date)) for d in [1, 10, 20]]:
        for kk in [0, 1]:
            dd_ = dec if kk == 0 else lag_dec(dec, kk)
            s_, f_ = simulate(px, cr_irx, dates, dd_)
            a0 = full_stats(s_["eq"], BASE_DATE); a1 = full_stats(s_["eq"], W13_DATE)
            rows.append([lab, "same close" if kk == 0 else "next close", pct(a0["cagr"]), pct(a1["cagr"]), pct(a0["maxdd"]),
                         f"{a0['pk'].date()} to {a0['tr'].date()}", pct(yearly(s_["eq"]).loc[2022])])
    cr0 = cash_returns(irx, dates, "fixed", fixed=0.0)
    dl1 = lag_dec(dec_me, 1)
    for lab, cr_, bp_ in [("month-end, next close, 20 bp", cr_irx, 20.0),
                          ("month-end, next close, cash 0%", cr0, COST_BP),
                          ("month-end, same close, cash 0% (closest to the researcher's likely setup)", cr0, COST_BP)]:
        dd_ = dec_me if "same close" in lab else dl1
        s_, f_ = simulate(px, cr_, dates, dd_, bp=bp_)
        a0 = full_stats(s_["eq"], BASE_DATE); a1 = full_stats(s_["eq"], W13_DATE)
        rows.append([lab, "-", pct(a0["cagr"]), pct(a1["cagr"]), pct(a0["maxdd"]),
                     f"{a0['pk'].date()} to {a0['tr'].date()}", pct(yearly(s_["eq"]).loc[2022])])
    P(md_table(["Variant", "Execution", "CAGR 2007+", "CAGR 2013+", "Max DD", "DD peak to trough", "2022"], rows))
    P("")

    # ---------- integrity checks
    P("## 9. Integrity checks on this script and the data\n")
    # (i) independent engine re-implementation (0 bp, IRX cash): segment compounding from price ratios
    s0_, _ = simulate(px, cr_irx, dates, dec_me, bp=0.0)
    keys = sorted(dec_me)
    pre = [k for k in keys if k <= dates[0]]
    seg_dates = [dates[0]] + [k for k in keys if k > dates[0]]
    cum_cash = np.cumprod(1.0 + cr_irx)
    pxv = px.reindex(dates)
    ind = pd.Series(np.nan, index=dates)
    E = 1.0
    cur_flags = dec_me[pre[-1]]
    for j, sd in enumerate(seg_dates):
        ed = seg_dates[j + 1] if j + 1 < len(seg_dates) else dates[-1]
        i0, i1 = dates.get_loc(sd), dates.get_loc(ed)
        f = cur_flags.astype(float)
        for i in range(i0, i1 + 1):
            ratio = pxv.iloc[i].values / pxv.iloc[i0].values
            val = E * ((SLEEVE * f * ratio).sum() + (1 - SLEEVE * f.sum()) * cum_cash[i] / cum_cash[i0])
            ind.iloc[i] = val
        E = ind.iloc[i1]
        if j + 1 < len(seg_dates):
            cur_flags = dec_me[seg_dates[j + 1]]
    diff_e = (ind - s0_["eq"]).abs().max()
    P(f"- Engine vs independent segment-compounding re-implementation (0 bp, ^IRX cash, month-end signal): max |equity difference| = {diff_e:.2e} "
      f"over {len(dates)} days (end values {ind.iloc[-1]:.6f} vs {s0_['eq'].iloc[-1]:.6f}).")
    # (ii) independent SMA/flag re-computation with plain python dicts
    last_by_month = {}
    for d_ in px.index:
        last_by_month[(d_.year, d_.month)] = d_
    me_list = sorted(last_by_month.values())
    if not last_month_complete:
        me_list = me_list[:-1]
    bad = 0; chk = 0
    for n_i in range(len(me_list)):
        d_ = me_list[n_i]
        if d_ not in dec_me:
            continue
        for ai, a_ in enumerate(ASSETS):
            window = [float(px.loc[me_list[q], a_]) for q in range(n_i - 9, n_i + 1)]
            flag = window[-1] > sum(window) / 10.0
            chk += 1
            if flag != bool(dec_me[d_][ai]):
                bad += 1
    P(f"- Month-end flags re-computed with plain-Python loops: {chk} asset-month flags checked, {bad} mismatches.")
    # (iii) total-return reconstruction from raw close + dividends vs yfinance adjusted close
    try:
        rows = []
        divy = {}
        for a_ in ASSETS:
            h = yf.Ticker(a_).history(start=DL_START, auto_adjust=False, actions=True)
            h.index = h.index.tz_localize(None) if h.index.tz is not None else h.index
            h.index = h.index.normalize()
            h = h.loc[:last_date]
            tr = ((h["Close"] + h["Dividends"]) / h["Close"].shift(1)).fillna(1.0).cumprod()
            ad = px[a_].reindex(h.index).dropna()
            tr = tr.reindex(ad.index)
            rel = (tr / tr.loc[BASE_DATE]) / (ad / ad.loc[BASE_DATE]) - 1.0
            rel = rel.loc[BASE_DATE:]
            rows.append([a_, pct(rel.abs().max(), 3), pct(rel.iloc[-1], 3), int(h["Dividends"].loc[BASE_DATE:].gt(0).sum())])
            divy[a_] = (h["Dividends"] / h["Close"].shift(1)).reindex(dates).fillna(0.0)
        P("- Total-return reconstruction (raw Close + cash dividends, reinvested at the close) vs yfinance adjusted close, 2006-12-29..latest "
          "(same vendor, so this tests the adjustment arithmetic, NOT the price source):\n")
        P(md_table(["Asset", "Max abs deviation", "Deviation at end", "# dividend events"], rows))
        drag = {a_: float((SLEEVE * flg[a_].shift(1).fillna(False).astype(float) * divy[a_]).sum() * 0.30 / s07["nyears"]) for a_ in ASSETS}
        P(f"\n- Conditional tax caveat (account-holder status NOT known to this task): if 30% US withholding were applied to ALL dividends "
          f"(an upper bound; interest-type AGG dividends are largely exempt for non-US persons), the simple annual drag on this exposure pattern "
          f"would be about {100 * sum(drag.values()):.2f} points/yr (" + ", ".join(f"{a_} {100 * v:.2f}" for a_, v in drag.items()) + "). "
          "Not included in any CAGR above. US taxpayers would instead face short-term-gain treatment on most switches.")
    except Exception as e:  # noqa
        P(f"- Total-return reconstruction check could not run ({type(e).__name__}: {str(e)[:100]}).")
    P("- Second independent price vendor: NOT obtained. Stooq's CSV endpoint returns a JavaScript bot-challenge page, which I did not try to bypass; "
      "FRED carries no ETF prices. All ETF prices therefore rest on one vendor (Yahoo via yfinance). FRED DTB3 was used only for the cash cross-check.\n")

    # ---------- current state
    P("## 10. Current state of the signal (informational, no action)\n")
    rows = []
    for d_ in me_dates[-4:]:
        sma_ = me_px.rolling(10).mean().loc[d_]
        rows.append([d_.date()] + [("ON " if me_px.loc[d_, a_] > sma_[a_] else "off") + f" ({100 * (me_px.loc[d_, a_] / sma_[a_] - 1):+.1f}%)" for a_ in ASSETS])
    P(md_table(["Month-end"] + ASSETS, rows))
    sma_now = me_px.tail(9).mean()
    P("\nLast close vs mean of the last 9 completed month-ends (preview of the next month-end test, which also includes that day's own close): "
      + ", ".join(f"{a_} {100 * (px[a_].iloc[-1] / sma_now[a_] - 1):+.1f}%" for a_ in ASSETS) + ".\n")

    # ---------- $10K translation
    P("## 11. What this means in dollars on $10,000 (computed, not estimated)\n")
    mm = {}
    for nm, e_ in [("GTAA-5", eq), ("60/40", eq6040), ("SPY", spy_eq)]:
        mr = e_.resample("ME").last().pct_change().dropna()
        mr = mr if last_month_complete else mr.iloc[:-1]
        sdd = full_stats(e_, BASE_DATE)
        yy = yearly(e_)
        yy_full = yy[yy.index < 2026]
        r12 = e_.resample("ME").last().pct_change(12).dropna()
        r12 = r12 if last_month_complete else r12.iloc[:-1]
        mm[nm] = dict(avg=10000 * mr.mean(), med=10000 * mr.median(), neg=(mr < 0).mean(), worst_m=10000 * mr.min(),
                      dd_usd=10000 * sdd["maxdd"], worst_y=10000 * yy_full.min(), worst_y_yr=int(yy_full.idxmin()),
                      end_usd=10000 * sdd["end"], cagr_usd_mo=10000 * (sdd["end"] - 1) / (sdd["nyears"] * 12),
                      worst12=10000 * r12.min(), worst12_end=r12.idxmin(), n=len(mr))
    rows = []
    for nm, v in mm.items():
        rows.append([nm, f"${v['avg']:.0f}", f"${v['med']:.0f}", pct(v["neg"], 0), f"${v['worst_m']:.0f}",
                     f"${v['worst_y']:.0f} ({v['worst_y_yr']})", f"${v['worst12']:.0f} (to {v['worst12_end'].strftime('%Y-%m')})",
                     f"${v['dd_usd']:.0f}", f"${v['end_usd']:,.0f}"])
    P(md_table(["Strategy ($10K at 2006-12-29)", "Mean $/month (compounding path)", "Median $/month", "Months down",
                "Worst month $", "Worst calendar year $", "Worst rolling 12m $", "Max peak-to-trough $ (on $10K at the peak)",
                "Value today of $10K"], rows))
    e13 = eq.loc[W13_DATE:]
    P(f"\n- Starting a fresh $10,000 at the {W13_DATE.date()} close would be worth ${10000 * e13.iloc[-1] / e13.iloc[0]:,.0f} at {last_date.date()}, "
      f"an average of ${10000 * (e13.iloc[-1] / e13.iloc[0] - 1) / (s13['nyears'] * 12):.0f} per month of simple gain.")
    P(f"- Practical: SPY adjusted close on {last_date.date()} is ${px['SPY'].iloc[-1]:.2f}; one 20% sleeve of a $10,000 account is $2,000, "
      f"i.e. {2000 / px['SPY'].iloc[-1]:.2f} SPY shares. Whole-share rounding on SPY alone can misstate that sleeve by up to "
      f"{pct(0.5 * px['SPY'].iloc[-1] / 2000, 0)} unless the broker supports fractional shares.\n")

    txt = "\n".join(out)
    if a.out:
        with open(a.out, "w") as f:
            f.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
