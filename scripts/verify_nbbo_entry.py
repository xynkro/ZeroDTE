"""Verify the NBBO entry plane (fix for Jul-1's near-ATM strikes + fictional credits).

Synthetic chain priced by our own BS engine at known tv → the correct 16Δ
strikes are computable in closed form; the picker must land on them, price
honestly off the quote mids, dodge excluded (open-position) strikes, refuse
near-ATM shorts, and the fetch layer must drop stale/one-sided quotes.

Run:  PYTHONPATH=. .venv/bin/python scripts/verify_nbbo_entry.py
"""
import asyncio
from datetime import datetime, timezone, timedelta

from backend.app import bs_pricing as bs
from backend.app.nbbo_chain import fetch_chain_nbbo, pick_iron_condor_nbbo
from backend.app.alpaca_trader import AlpacaTrader

SPOT = 747.0
TV = 0.010                       # 1.0% total vol to expiry — typical calm 0DTE
FRESH = datetime.now(timezone.utc).isoformat()
STALE = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
fails = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def synth_chain(spread=0.04):
    """Chain priced at flat TV with a fixed quote spread."""
    calls, puts = [], []
    for k in range(725, 771):
        cm = bs.call_price(SPOT, float(k), TV)
        pm = bs.put_price(SPOT, float(k), TV)
        if cm > 0.005:
            calls.append({"strike": float(k), "bid": max(0.01, cm - spread / 2),
                          "ask": cm + spread / 2, "mid": cm})
        if pm > 0.005:
            puts.append({"strike": float(k), "bid": max(0.01, pm - spread / 2),
                         "ask": pm + spread / 2, "mid": pm})
    return {"calls": calls, "puts": puts}


async def main():
    print("1. implied_tv round-trip")
    for k in (740.0, 747.0, 754.0):
        px = bs.call_price(SPOT, k, TV)
        tv = bs.implied_tv(px, SPOT, k, True)
        check(f"call K={k:.0f}", tv is not None and abs(tv - TV) < 2e-4,
              f"tv={tv}")
    px = bs.put_price(SPOT, 741.0, TV)
    tv = bs.implied_tv(px, SPOT, 741.0, False)
    check("put K=741", tv is not None and abs(tv - TV) < 2e-4, f"tv={tv}")
    check("intrinsic-only → None", bs.implied_tv(7.0, SPOT, 740.0, True) is None)

    print("2. delta targeting (closed-form expected strikes)")
    exp_c = bs.strike_for_call_delta(SPOT, TV, 0.16)
    exp_p = bs.strike_for_put_delta(SPOT, TV, 0.16)
    chain = synth_chain()
    ic = pick_iron_condor_nbbo(chain, SPOT, 0.16, 25.0, "260702")
    check("picker ok", ic.get("ok"), str(ic.get("error")))
    if ic.get("ok"):
        sc, sp = ic["short_call"] / 10, ic["short_put"] / 10
        check(f"short call ≈ {exp_c:.1f}", abs(sc - exp_c) <= 1.0, f"picked {sc}")
        check(f"short put ≈ {exp_p:.1f}", abs(sp - exp_p) <= 1.0, f"picked {sp}")
        check("call delta ≈ 0.16", abs(ic["short_call_delta"] - 0.16) < 0.04,
              str(ic["short_call_delta"]))
        check("SPX scale ×10", ic["spot"] == SPOT * 10)

        print("3. credit honesty (mid-based, conservative below mid)")
        bys = {r["strike"]: r for r in chain["calls"]}
        byp = {r["strike"]: r for r in chain["puts"]}
        exp_call_usd = (bys[sc]["mid"] - bys[ic["long_call"] / 10]["mid"]) * 1000
        exp_put_usd = (byp[sp]["mid"] - byp[ic["long_put"] / 10]["mid"]) * 1000
        check("call credit == chain mids", abs(ic["call_credit_usd"] - exp_call_usd) < 1)
        check("put credit == chain mids", abs(ic["put_credit_usd"] - exp_put_usd) < 1)
        check("conservative < mid",
              ic["call_credit_cons_usd"] < ic["call_credit_usd"]
              and ic["put_credit_cons_usd"] < ic["put_credit_usd"])

        print("4. exclusion dodge (open MEIC strikes)")
        ic2 = pick_iron_condor_nbbo(chain, SPOT, 0.16, 25.0, "260702",
                                    exclude_spy_strikes={sc, sc + 1.0, sp})
        check("still ok with exclusions", ic2.get("ok"))
        if ic2.get("ok"):
            check("short call moved off excluded",
                  ic2["short_call"] / 10 not in {sc, sc + 1.0},
                  f"{sc} → {ic2['short_call'] / 10}")
            check("short put moved off excluded", ic2["short_put"] / 10 != sp,
                  f"{sp} → {ic2['short_put'] / 10}")

    print("5. near-ATM refusal (Jul-1 13:00 disease)")
    atm_only = {"calls": [r for r in chain["calls"] if abs(r["strike"] - SPOT) <= 1],
                "puts": [r for r in chain["puts"] if abs(r["strike"] - SPOT) <= 1]}
    ic3 = pick_iron_condor_nbbo(atm_only, SPOT, 0.16, 25.0, "260702")
    check("ATM-only chain → ok=False", not ic3.get("ok"), str(ic3.get("error")))

    print("6. fetch layer drops stale / one-sided / missing")
    class T:
        _occ_symbol = staticmethod(AlpacaTrader._occ_symbol)
        async def get_option_quotes(self, symbols):
            out = {}
            for s in symbols:
                k = int(s[-8:]) / 1000
                if k == 745:      # stale
                    out[s] = {"bid": 1.0, "ask": 1.1, "ts": STALE}
                elif k == 746:    # one-sided
                    out[s] = {"bid": 1.0, "ask": 0.0, "ts": FRESH}
                elif k == 748:    # missing → not in dict
                    continue
                else:
                    out[s] = {"bid": 1.0, "ask": 1.1, "ts": FRESH}
            return out
    ch = await fetch_chain_nbbo(T(), SPOT, "260702", span_pct=0.005)
    got = {r["strike"] for r in ch["calls"]}
    check("stale dropped", 745.0 not in got)
    check("one-sided dropped", 746.0 not in got)
    check("missing dropped", 748.0 not in got)
    check("fresh kept", 747.0 in got and 744.0 in got, str(sorted(got)))

    print("7. per-side credit floor → one-sided (Jun-30 free-call-risk ban)")
    # Calls: barely-positive mid credit but conservative ≤ 0 → floored out.
    dead_calls = []
    for k in range(748, 762):
        mid = max(0.02, 0.10 - 0.008 * (k - 748))
        dead_calls.append({"strike": float(k), "bid": max(0.01, mid - 0.02),
                           "ask": mid + 0.02, "mid": mid})
    mixed = {"calls": dead_calls, "puts": chain["puts"]}
    ic4 = pick_iron_condor_nbbo(mixed, SPOT, 0.16, 25.0, "260702",
                                min_side_credit_usd=100.0, allow_one_sided=True)
    check("one-sided ok", ic4.get("ok"), str(ic4.get("error")))
    if ic4.get("ok"):
        check("sides=put_only", ic4.get("sides") == "put_only", str(ic4.get("sides")))
        check("call dropped loudly", any("call" in d for d in ic4.get("dropped", [])),
              str(ic4.get("dropped")))
        check("no call keys in one-sided output", "short_call" not in ic4)
        check("put credit sane", ic4["total_credit_usd"] > 100)
    ic5 = pick_iron_condor_nbbo(mixed, SPOT, 0.16, 25.0, "260702",
                                min_side_credit_usd=100.0, allow_one_sided=False)
    check("condor-only mode → skip when a side fails", not ic5.get("ok"))
    ic6 = pick_iron_condor_nbbo(chain, SPOT, 0.16, 25.0, "260702",
                                min_side_credit_usd=100.0, allow_one_sided=True)
    check("healthy chain stays both-sided", ic6.get("ok") and ic6.get("sides") == "both",
          str(ic6.get("sides")))

    print()
    if fails:
        print(f"❌ FAILED: {fails}")
        raise SystemExit(1)
    print("✅ ALL PASS — NBBO entry plane picks true-delta strikes, prices honestly, "
          "dodges collisions, refuses ATM, fail-closes on bad quotes, and bans free-risk sides.")


if __name__ == "__main__":
    asyncio.run(main())
