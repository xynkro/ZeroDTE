#!/usr/bin/env python
"""Score the Config L live-read probe: every read in data/live_calls.jsonl against (a) the
session close (direction) and (b) broker dollars from data/trial_trades.jsonl. Prints the
scoreboard; --telegram sends it through the engine's Telegram routing. No trading."""
import json, os, sys, datetime as dt, statistics as st
from zoneinfo import ZoneInfo
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
ET = ZoneInfo("America/New_York")

def load(p):
    try:
        return [json.loads(l) for l in open(p) if l.strip()]
    except FileNotFoundError:
        return []

def spy_closes(dates, env):
    import requests
    h = {"APCA-API-KEY-ID": env.get("ALPACA_API_KEY", ""), "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", "")}
    out = {}
    if not dates: return out
    r = requests.get("https://data.alpaca.markets/v2/stocks/SPY/bars", headers=h, timeout=20,
                     params={"timeframe": "1Day", "start": f"{min(dates)}T00:00:00Z", "end": f"{max(dates)}T23:59:59Z", "feed": "iex", "limit": 1000}).json()
    for b in r.get("bars", []):
        out[b["t"][:10]] = b["c"]
    return out

def main():
    env = {}
    for l in open(os.path.join(REPO, ".env")):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, v = l.split("=", 1); env[k.strip()] = v.split("#")[0].strip().strip('"').strip("'")
    calls = load(os.path.join(REPO, "backend", "data", "live_calls.jsonl"))
    ledger = {r.get("id"): r for r in load(os.path.join(REPO, "backend", "data", "trial_trades.jsonl"))}
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    reads = [c for c in calls if c.get("lean") in ("up", "down") and c.get("decision") in ("submitted", "advisory_only", "rejected")]
    closes = spy_closes(sorted({c["date"] for c in reads if c["date"] < today or dt.datetime.now(ET).hour >= 16}), env)
    dir_hits = []; dollars = []; lines = []
    for c in reads:
        close = closes.get(c["date"]); spot = c.get("spot_spy")
        hit = None
        if close and spot:
            hit = (close > spot) if c["lean"] == "up" else (close < spot)
            dir_hits.append(hit)
        pnl = None
        t = ledger.get(c.get("trade_id")) if c.get("trade_id") else None
        if t and t.get("closed"):
            pnl = t.get("broker_realized_pnl"); 
            if pnl is not None: dollars.append(float(pnl))
        lines.append(f"{c['date']} {c.get('time_et')} {c.get('source','?')[:9]:9s} {c['lean']:4s} {c.get('conf',0):.2f} "
                     f"{'✓' if hit else ('✗' if hit is False else '·')} {('$%+.0f' % pnl) if pnl is not None else ('open' if t else c.get('decision','?')[:8])}")
    n_dir = len(dir_hits); n_hit = sum(1 for h in dir_hits if h)
    n_tr = len(dollars); tot = sum(dollars)
    green_days = {}
    for c in reads:
        t = ledger.get(c.get("trade_id")) if c.get("trade_id") else None
        if t and t.get("closed") and t.get("broker_realized_pnl") is not None:
            green_days[c["date"]] = green_days.get(c["date"], 0) + float(t["broker_realized_pnl"])
    gd = sum(1 for v in green_days.values() if v > 0)
    head = (f"📊 CONFIG L SCOREBOARD · {today}\n"
            f"reads scored {n_dir} · direction hit {n_hit}/{n_dir}" + (f" = {100*n_hit/max(1,n_dir):.0f}%" if n_dir else "") + "\n"
            f"trades closed {n_tr} · real ${tot:+.0f}" + (f" · mean ${tot/n_tr:+.0f}/trade · worst ${min(dollars):+.0f}" if n_tr else "") + "\n"
            f"sessions traded {len(green_days)} · green {gd}" + (f" ({100*gd/len(green_days):.0f}%)" if green_days else "") + "\n"
            f"gate at 25 calls: mean > 0, ≥70% green sessions, worst ≤ -$480")
    text = head + ("\n" + "\n".join(lines[-12:]) if lines else "\n(no reads yet)")
    print(text)
    if "--telegram" in sys.argv:
        from backend.app import telegram as tg
        cid, tid = tg._route_zero_dte()
        r = tg.send(text, chat_id=cid, message_thread_id=tid)
        print("telegram:", "sent" if r else "not sent")

if __name__ == "__main__":
    main()
