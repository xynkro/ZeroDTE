#!/usr/bin/env python
"""NBBO SURFACE SAMPLER — logs the EXECUTABLE 0DTE SPY credit surface every 5 minutes during RTH.

WHY: Config F's journal recorded real credit at ONE strike per slot (the cushion boundary) and
proved the backtest's model×0.5 haircut was fantasy. Any premium-targeted structure (sell where
the premium IS, manage risk with a stop — the Math Makes Money / TradeSteward family) can only be
evaluated on REAL executable quotes by strike and time-of-day. This builds that dataset.
Zero trading. Read-only quotes. Appends one JSON row per sample to backend/data/nbbo_surface.jsonl.
Run by launchd every 300 s; exits immediately outside RTH unless --force.
"""
import os, re, sys, json, datetime as dt, requests
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "backend", "data", "nbbo_surface.jsonl")
env = {}
for l in open(os.path.join(REPO, ".env")):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
H = {"APCA-API-KEY-ID": env.get("ALPACA_API_KEY") or env.get("ALPACA_KEY_ID", ""),
     "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY") or env.get("ALPACA_API_SECRET", "")}
FEED = env.get("ALPACA_OPTIONS_FEED", "indicative")
ET = ZoneInfo("America/New_York")
now = dt.datetime.now(ET)
force = "--force" in sys.argv
if not force and (now.weekday() >= 5 or not (dt.time(9, 35) <= now.time() <= dt.time(16, 0))):
    sys.exit(0)

spot = requests.get("https://data.alpaca.markets/v2/stocks/SPY/trades/latest", headers=H,
                    params={"feed": "iex"}, timeout=10).json()["trade"]["p"]
exp = now.strftime("%Y-%m-%d")
snaps = {}
for typ in ("put", "call"):
    r = requests.get("https://data.alpaca.markets/v1beta1/options/snapshots/SPY", headers=H, timeout=15,
                     params={"feed": FEED, "type": typ, "expiration_date": exp,
                             "strike_price_gte": round(spot * 0.975), "strike_price_lte": round(spot * 1.025),
                             "limit": 1000})
    if r.status_code != 200:
        print("HTTP", r.status_code, r.text[:160]); continue
    for sym, s in (r.json().get("snapshots") or {}).items():
        m = re.match(r"SPY(\d{6})([CP])(\d{8})", sym)
        if not m: continue
        q = s.get("latestQuote") or {}
        if q.get("bp") is None or q.get("ap") is None: continue
        snaps[(typ, int(m.group(3)) / 1000.0)] = {"bp": q["bp"], "ap": q["ap"], "t": q.get("t")}

def q(typ, k): return snaps.get((typ, float(k)))
def ex(typ, ks, kl):
    s, l = q(typ, ks), q(typ, kl)
    return None if not s or not l else round((s["bp"] - l["ap"]) * 100, 2)
def mid(typ, k):
    s = q(typ, k); return None if not s else round((s["bp"] + s["ap"]) / 2, 3)

atm = round(spot)
row = {"ts": now.isoformat(timespec="seconds"), "spot": spot, "exp": exp, "n_quotes": len(snaps),
       "min_left": max(0, int((16 * 60) - (now.hour * 60 + now.minute))),
       "atm_straddle_mid": (mid("put", atm) or 0) + (mid("call", atm) or 0) if snaps else None,
       "surface": {}, "targets": {}}
for otm in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
    kp, kc = round(spot * (1 - otm / 100)), round(spot * (1 + otm / 100))
    row["surface"][f"{otm:.1f}"] = {
        "put": {"k": kp, "bid": (q("put", kp) or {}).get("bp"), "w1": ex("put", kp, kp - 1), "w2": ex("put", kp, kp - 2), "w5": ex("put", kp, kp - 5)},
        "call": {"k": kc, "bid": (q("call", kc) or {}).get("bp"), "w1": ex("call", kc, kc + 1), "w2": ex("call", kc, kc + 2), "w5": ex("call", kc, kc + 5)}}
# premium-targeted candidates: farthest strike whose BID still pays the target; wing = first strike asking <= $0.05
for tgt in (0.20, 0.30, 0.50, 1.00):
    for typ, sign in (("put", -1), ("call", +1)):
        ks = None
        for i in range(0, 60):
            k = atm + sign * i; s = q(typ, k)
            if s and s["bp"] >= tgt: ks = k
            elif s and ks is not None: break
        nickel = None
        for i in range(0, 120):
            k = atm + sign * i; s = q(typ, k)
            if s and 0 < s["ap"] <= 0.05: nickel = k; break
        row["targets"][f"{typ}_{tgt:.2f}"] = {
            "short": ks, "pct_otm": round(100 * abs(ks - spot) / spot, 3) if ks else None,
            "wing": nickel, "width": abs(nickel - ks) if (ks and nickel) else None,
            "net_credit": round((q(typ, ks)["bp"] - q(typ, nickel)["ap"]) * 100, 2) if (ks and nickel) else None}
with open(OUT, "a") as f:
    f.write(json.dumps(row) + "\n")
print(f"{row['ts']} spot {spot} quotes {len(snaps)} atm_straddle {row['atm_straddle_mid']} "
      f"put0.5%w1={row['surface']['0.5']['put']['w1']} call0.5%w1={row['surface']['0.5']['call']['w1']} "
      f"put$0.30→{row['targets']['put_0.30']['short']} ({row['targets']['put_0.30']['pct_otm']}% OTM, wing {row['targets']['put_0.30']['wing']}, net {row['targets']['put_0.30']['net_credit']})")
