"""G12: the NBBO plane priced the LIVE chain during market hours on Config F, and the
10%-of-width floor bound on EXECUTABLE credit (short.bid - long.ask). Evidence = the band
decision journal 2026-09-08..2026-09-22 (Alpaca options feed). Details go to stderr;
stdout is the verdict only."""
import json, sys
rows = [json.loads(l) for l in open("backend/data/band_decisions.jsonl") if l.strip()]
win = [r for r in rows if "2026-09-08" <= r.get("date", "") <= "2026-09-22"]
nbbo = [r for r in win if r.get("basis") == "exec_bid_ask_nbbo" and r.get("quotable") is True
        and isinstance(r.get("best_real_ct"), (int, float))]
assert len(nbbo) >= 20, f"only {len(nbbo)} NBBO-priced rows"
assert {r.get("feed") for r in nbbo} == {"alpaca"}, "not on the Alpaca feed"
gated = [r for r in nbbo if r["decision"] == "gated"]
assert gated and all(r["best_real_ct"] < r["real_floor"] == 10.0 for r in gated), "floor did not bind"
opened = [r for r in win if r.get("decision") == "opened" and isinstance(r.get("real_mid_ct"), (int, float))]
assert opened and all(r["real_mid_ct"] >= 10.0 for r in opened), "an entry below the floor"
days = sorted({r["date"] for r in nbbo})
print(f"nbbo_rows={len(nbbo)} days={days[0]}..{days[-1]} ({len(days)}) gated_below_floor={len(gated)} "
      f"best_real_max=${max(r['best_real_ct'] for r in gated):.0f}/ct opened_at_or_above_floor={len(opened)} "
      f"(real_mid_ct {[r['real_mid_ct'] for r in opened]})", file=sys.stderr)
print("GATE_G12_PASS")
