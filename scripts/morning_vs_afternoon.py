import json, os, urllib.request, urllib.parse, statistics as st
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
env = {}
for l in open(os.path.expanduser("~/Trading/ZeroDTE-Wave/.env")):
    if "=" in l and not l.lstrip().startswith("#"):
        k, v = l.split("=", 1); env[k.strip()] = v.split("#")[0].strip().strip('"').strip("'")
H = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}
out, tok = [], None
while True:
    q = {"timeframe": "30Min", "start": "2020-07-01", "end": "2026-10-31", "limit": 10000, "adjustment": "raw", "feed": "iex"}
    if tok: q["page_token"] = tok
    r = urllib.request.Request("https://data.alpaca.markets/v2/stocks/SPY/bars?" + urllib.parse.urlencode(q), headers=H)
    d = json.load(urllib.request.urlopen(r, timeout=60)); out += d.get("bars", []); tok = d.get("next_page_token")
    if not tok: break
days = {}
for b in out:
    t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
    if t.hour < 9 or (t.hour == 9 and t.minute < 30) or t.hour >= 16: continue
    days.setdefault(t.date(), []).append((t, b))
rows = []
for d, bs in sorted(days.items()):
    bs.sort(key=lambda x: x[0])
    am = [b for t, b in bs if (t.hour, t.minute) < (13, 0)]; pm = [b for t, b in bs if (t.hour, t.minute) >= (13, 0)]
    if len(am) < 6 or len(pm) < 5: continue
    o = am[0]["o"]; p13 = pm[0]["o"]; c = pm[-1]["c"]
    rows.append(dict(d=d, am_rng=(max(b["h"] for b in am) - min(b["l"] for b in am)) / o,
                     am_move=p13 / o - 1, pm_move=c / p13 - 1,
                     pm_hi=max(b["h"] for b in pm) / p13 - 1, pm_lo=min(b["l"] for b in pm) / p13 - 1))
n = len(rows); print(f"SPY 30-min bars (Alpaca iex) {rows[0]['d']} to {rows[-1]['d']}: {n} sessions with a 09:30-13:00 morning and a 13:00-16:00 afternoon")
def corr(x, y):
    mx, my = st.mean(x), st.mean(y); sx = sum((a-mx)**2 for a in x)**.5; sy = sum((b-my)**2 for b in y)**.5
    return sum((a-mx)*(b-my) for a, b in zip(x, y)) / (sx*sy)
print(f"corr(morning move 09:30->13:00, afternoon move 13:00->close) = {corr([r['am_move'] for r in rows],[r['pm_move'] for r in rows]):+.3f}  <- direction carry-over")
print(f"corr(morning range, afternoon range)                          = {corr([r['am_rng'] for r in rows],[r['pm_hi']-r['pm_lo'] for r in rows]):+.3f}  <- size carry-over")
print()
qs = sorted(r["am_rng"] for r in rows); cuts = [qs[int(n*.25)], qs[int(n*.5)], qs[int(n*.75)], qs[int(n*.9)]]
print("13:00 mech arm = both sides 0.30% from spot. A side dies when the afternoon runs through it.")
print("morning range (09:30-13:00)        days   P(afternoon touches +0.30%)  P(touches -0.30%)  P(touches EITHER)  P(touches BOTH)")
labels = [("smallest 25%", 0, cuts[0]), ("25-50%", cuts[0], cuts[1]), ("50-75%", cuts[1], cuts[2]), ("75-90%", cuts[2], cuts[3]), ("top 10%", cuts[3], 9)]
for lab, lo, hi in labels:
    S = [r for r in rows if lo <= r["am_rng"] < hi]
    up = [r["pm_hi"] > .003 for r in S]; dn = [r["pm_lo"] < -.003 for r in S]
    either = [a or b for a, b in zip(up, dn)]; both = [a and b for a, b in zip(up, dn)]
    f = lambda xs: f"{100*sum(xs)/len(xs):5.1f}%"
    print(f"{lab:<14} ({100*lo:4.2f}%-{min(100*hi,9.99):4.2f}%)   {len(S):5d}        {f(up)}                   {f(dn)}             {f(either)}            {f(both)}")
print()
print("Direction: given the MORNING went down >0.30% by 13:00, afternoon ...")
S = [r for r in rows if r["am_move"] < -.003]
print(f"  n={len(S)}  continues down {100*sum(r['pm_move']<0 for r in S)/len(S):.1f}%  |  reverses up {100*sum(r['pm_move']>0 for r in S)/len(S):.1f}%  |  touches +0.30% anyway {100*sum(r['pm_hi']>.003 for r in S)/len(S):.1f}%")
S = [r for r in rows if r["am_move"] > .003]
print(f"given the morning went UP >0.30%: n={len(S)}  continues up {100*sum(r['pm_move']>0 for r in S)/len(S):.1f}%  |  reverses down {100*sum(r['pm_move']<0 for r in S)/len(S):.1f}%  |  touches -0.30% anyway {100*sum(r['pm_lo']<-.003 for r in S)/len(S):.1f}%")
