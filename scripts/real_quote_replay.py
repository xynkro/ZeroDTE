#!/usr/bin/env python3
"""Real-quote replay of the mech rule at each entry slot: XSP $2 wide, both sides 0.30% from spot,
floor $12/ct, TP 60%, stop when buy-back >= 2x credit, 15:25 ET close, $2 round-trip fee. No model.
Strikes follow live_call.submit_call (SPY strike x live ratio, nearest XSP strike, step out to clear
the minimum). Checked against the live 2026-10-30 13:00 order: call 762/764, put 755/753.
Usage: python3 scripts/real_quote_replay.py [nbbo_surface.jsonl] [--confirm 2] [--dist 0.3]
Entries use the first 5-minute row after the slot. Live stops confirm on two reads ~60 s apart, so
--confirm 1 (default) is the closer model; --confirm 2 needs two rows 5 minutes apart."""
import argparse, collections, datetime as dt, json, math, statistics as st

def load(path):
    by = collections.defaultdict(list)
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("n_quotes", 0) >= 20 and r.get("legs_xsp") and r.get("xsp_implied_spot") and r["ts"][:10] >= "2026-10-12":
            t = dt.datetime.fromisoformat(r["ts"])
            r["_m"] = t.hour * 60 + t.minute + t.second / 60
            by[r["ts"][:10]].append(r)
    for d in by:
        by[d].sort(key=lambda r: r["_m"])
    return by

def spread(r, side, ks, kl):
    a, b = r["legs_xsp"][side].get(str(float(ks))), r["legs_xsp"][side].get(str(float(kl)))
    if not a or not b:
        return None
    return {"credit": (a[0] - b[1]) * 100, "cost": (a[1] - b[0]) * 100}

def live_strikes(r, put, dist):
    s, x, d = r["spot"], r["xsp_implied_spot"], dist / 100
    k = math.floor(s * (1 - d)) if put else math.ceil(s * (1 + d))
    if 100 * abs(s - k) / s < dist - 1e-9:
        k = k - 1 if put else k + 1
    ratio = x / s
    if not 1.0005 < ratio < 1.0070:
        return None
    ki = float(round(k * ratio))
    if (put and ki >= x - 0.5) or (not put and ki <= x + 0.5):
        return None
    for _ in range(3):
        if 100 * abs(x - ki) / x >= dist - 1e-9:
            break
        ki = ki - 1 if put else ki + 1
    if 100 * abs(x - ki) / x < dist - 1e-9:
        return None
    return int(ki), int(ki - 2 if put else ki + 2)

def run(by, slot, dist=0.3, floor=12.0, fee=2.0, tp=0.6, stop_mult=1.0, confirm=1):
    out, refused = [], 0
    for d, rs in sorted(by.items()):
        i = next((n for n, r in enumerate(rs) if r["_m"] >= slot), None)
        if i is None or rs[i]["_m"] > slot + 6:
            continue
        for side in ("put", "call"):
            ks = live_strikes(rs[i], side == "put", dist)
            e = spread(rs[i], side, *ks) if ks else None
            if not e:
                continue
            c = e["credit"]
            if c < floor:
                refused += 1
                continue
            pnl, how, hits, last = None, "close", 0, None
            for q in rs[i + 1:]:
                m = spread(q, side, *ks) if q["_m"] <= 925 else None
                if not m:
                    continue
                last = m["cost"]
                if last <= (1 - tp) * c:
                    pnl, how = c - last, "tp"; break
                hits = hits + 1 if last >= (1 + stop_mult) * c else 0
                if hits >= confirm:
                    pnl, how = c - last, "stop"; break
            if pnl is None:
                pnl = c - last if last is not None else 0.0
            out.append((d, side, c, pnl - fee, how))
    return out, refused

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="backend/data/nbbo_surface.jsonl")
    ap.add_argument("--confirm", type=int, default=1)
    ap.add_argument("--dist", type=float, default=0.3)
    a = ap.parse_args()
    by = load(a.path)
    print(f"{len(by)} sessions, {sum(map(len, by.values()))} rows, {min(by)} to {max(by)}")
    for s in ("09:35", "09:45", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00"):
        o, ref = run(by, int(s[:2]) * 60 + int(s[3:]), dist=a.dist, confirm=a.confirm)
        if not o:
            print(f"{s}  n=0 refused {ref}"); continue
        t, h = sum(x[3] for x in o), collections.Counter(x[4] for x in o)
        print(f"{s}  n={len(o):2d} refused {ref:2d} median credit ${st.median(x[2] for x in o):3.0f} ${t/len(o):+6.1f}/trade total ${t:+5.0f} "
              f"win {100*sum(x[3]>0 for x in o)/len(o):3.0f}% stop {h['stop']} tp {h['tp']} close {h['close']}")
