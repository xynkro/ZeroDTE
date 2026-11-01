import json, os, sys, urllib.request, urllib.parse, statistics as st
env = {}
for l in open(os.path.expanduser("~/Trading/ZeroDTE-Wave/.env")):
    if "=" in l and not l.lstrip().startswith("#"):
        k, v = l.split("=", 1); env[k.strip()] = v.split("#")[0].strip().strip('"').strip("'")
H = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}
def bars(feed):
    out, tok = [], None
    while True:
        q = {"timeframe": "1Day", "start": "2015-01-01", "end": "2026-10-31", "limit": 10000, "adjustment": "raw", "feed": feed}
        if tok: q["page_token"] = tok
        r = urllib.request.Request("https://data.alpaca.markets/v2/stocks/SPY/bars?" + urllib.parse.urlencode(q), headers=H)
        d = json.load(urllib.request.urlopen(r, timeout=30))
        out += d.get("bars", []); tok = d.get("next_page_token")
        if not tok: return out
try:
    B = bars("sip"); src = "sip"
except Exception as e:
    B = bars("iex"); src = "iex"
rows = []
for a, b in zip(B, B[1:]):
    gap = b["o"] / a["c"] - 1; intra = b["c"] / b["o"] - 1
    rows.append(dict(d=b["t"][:10], gap=gap, intra=intra, net=b["c"]/a["c"]-1,
                     hi=b["h"]/b["o"]-1, lo=b["l"]/b["o"]-1, rng=(b["h"]-b["l"])/b["o"]))
n = len(rows)
print(f"SPY daily bars via Alpaca ({src}): {rows[0]['d']} to {rows[-1]['d']}, {n} days")
def corr(x, y):
    mx, my = st.mean(x), st.mean(y)
    sx = sum((a-mx)**2 for a in x) ** .5; sy = sum((b-my)**2 for b in y) ** .5
    return sum((a-mx)*(b-my) for a, b in zip(x, y)) / (sx*sy)
print(f"corr(gap, open-to-close move)  = {corr([r['gap'] for r in rows],[r['intra'] for r in rows]):+.3f}   <- direction")
print(f"corr(|gap|, day range)         = {corr([abs(r['gap']) for r in rows],[r['rng'] for r in rows]):+.3f}   <- size")
print()
print("gap bucket (pre-market move)      days  P(day continues  P(day reverses   P(gap-down day closes   high ran >0.30% above open   low ran >0.30% below open")
print("                                        in gap direction) past open)       below prev close)       (kills a call spread)        (kills a put spread)")
def pct(xs): return f"{100*sum(xs)/len(xs):5.1f}%" if xs else "   - "
for lab, lo, hi, sign in [("down > 1.0%", -9, -.01, -1), ("down 0.5-1.0%", -.01, -.005, -1), ("down 0.2-0.5%", -.005, -.002, -1),
                          ("flat +-0.2%", -.002, .002, 0), ("up 0.2-0.5%", .002, .005, 1), ("up 0.5-1.0%", .005, .01, 1), ("up > 1.0%", .01, 9, 1)]:
    S = [r for r in rows if lo <= r["gap"] < hi]
    if sign == 0:
        cont = rev = holds = None
    else:
        cont = [ (r["intra"]*sign) > 0 for r in S ]; rev = [ (r["intra"]*sign) < 0 for r in S ]
        holds = [ (r["net"]*sign) > 0 for r in S ]
    callkill = [ r["hi"] > .003 for r in S ]; putkill = [ r["lo"] < -.003 for r in S ]
    print(f"{lab:<32}{len(S):5d}   {pct(cont) if cont else '  n/a '}            {pct(rev) if rev else '  n/a '}            {pct(holds) if holds else '  n/a '}                 {pct(callkill)}                       {pct(putkill)}")
print()
big = [r for r in rows if abs(r["gap"]) >= .01]
print(f"median day range: all days {100*st.median([r['rng'] for r in rows]):.2f}% | after |gap|>=1%: {100*st.median([r['rng'] for r in big]):.2f}%  <- size is forecastable, direction is not")
