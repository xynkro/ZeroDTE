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
    reads = [c for c in calls if c.get("lean") in ("up", "down") and c.get("decision") in ("submitted", "advisory_only", "rejected")
             and not str(c.get("source", "")).startswith("dryrun")]
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
    # per-source scoreboard (mech = control arm, caspar = human arm, scan = model commentary)
    src_lines = []
    for src_key, label in (("mech", "MECH 13:00"), ("caspar", "CASPAR"), ("scan", "model read")):
        rs = [c for c in reads if str(c.get("source", "")).startswith(src_key)]
        if not rs:
            continue
        tr = [ledger.get(c.get("trade_id")) for c in rs if c.get("trade_id")]
        tr = [t for t in tr if t and t.get("closed") and t.get("broker_realized_pnl") is not None]
        pnl = [float(t["broker_realized_pnl"]) for t in tr]
        hits = []
        for c in rs:
            cl = closes.get(c["date"]); sp = c.get("spot_spy")
            if cl and sp:
                hits.append((cl > sp) if c["lean"] == "up" else (cl < sp))
        src_lines.append(f"{label}: reads {len(rs)} · dir {sum(hits)}/{len(hits)} · trades {len(pnl)} · real ${sum(pnl):+.0f}"
                         + (f" · mean ${sum(pnl)/len(pnl):+.0f} · worst ${min(pnl):+.0f}" if pnl else ""))
    n_dir = len(dir_hits); n_hit = sum(1 for h in dir_hits if h)
    n_tr = len(dollars); tot = sum(dollars)
    green_days = {}
    for c in reads:
        t = ledger.get(c.get("trade_id")) if c.get("trade_id") else None
        if t and t.get("closed") and t.get("broker_realized_pnl") is not None:
            green_days[c["date"]] = green_days.get(c["date"], 0) + float(t["broker_realized_pnl"])
    gd = sum(1 for v in green_days.values() if v > 0)
    # broker truth: orders this week — the number that was silently 0 for 3 weeks
    broker_line = ""
    try:
        import requests
        h = {"APCA-API-KEY-ID": env.get("ALPACA_API_KEY", ""), "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", "")}
        base = env.get("ALPACA_BASE_URL", "")
        since = (dt.datetime.now(ET) - dt.timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
        od = requests.get(base + "/v2/orders", headers=h, params={"status": "all", "after": since, "limit": 200}, timeout=15).json()
        acct = requests.get(base + "/v2/account", headers=h, timeout=10).json()
        n_ord = len(od) if isinstance(od, list) else -1
        broker_line = f"BROKER: {n_ord} orders in 7 days · equity ${float(acct.get('equity', 0)):,.0f}" + ("  ⚠️ NO ORDERS" if n_ord == 0 else "")
    except Exception as e:  # noqa: BLE001
        broker_line = f"BROKER: unavailable ({e})"
    head = (f"📊 CONFIG L SCOREBOARD · {today}\n{broker_line}\n" + ("\n".join(src_lines) + "\n" if src_lines else "")
            + f"reads scored {n_dir} · direction hit {n_hit}/{n_dir}" + (f" = {100*n_hit/max(1,n_dir):.0f}%" if n_dir else "") + "\n"
            f"trades closed {n_tr} · real ${tot:+.0f}" + (f" · mean ${tot/n_tr:+.0f}/trade · worst ${min(dollars):+.0f}" if n_tr else "") + "\n"
            f"sessions traded {len(green_days)} · green {gd}" + (f" ({100*gd/len(green_days):.0f}%)" if green_days else "") + "\n"
            f"gate at 25 calls: mean > 0, ≥70% green sessions, worst ≤ -$480")
    text = head + ("\n" + "\n".join(lines[-12:]) if lines else "\n(no reads yet)")
    print(text)
    if "--telegram" in sys.argv:
        from backend.app import config as _cfg  # noqa: F401 — loads .env (TELEGRAM_*) for the standalone script
        from backend.app import telegram as tg
        cid, tid = tg._route_zero_dte()
        r = tg.send(text, chat_id=cid, message_thread_id=tid)
        print("telegram:", "sent" if r else "not sent")

if __name__ == "__main__":
    main()
