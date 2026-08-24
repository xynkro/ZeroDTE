"""G7: the full chain builds valid wave-tagged SPX->SPY legs for BOTH sides, no order sent."""
import sys, math
sys.path.insert(0, '.')
from backend.app.config import settings
from backend.app.wave_band_live import decide_band_trades, BandParams
from backend.app.directional_spread_manager import (open_directional_trade, spy_strike_params,
                                                    _periods_remaining)
from backend.app.models import SignalEvent, StrikeSuggestion

closes = [5000 + 2.0*math.sin(i/7) + 0.05*i for i in range(80)]
r5, pr = 0.0012, 60
cands = decide_band_trades(closes, r5, pr, None, None,
                           BandParams(credit_floor=1.0, cushion_pct=0.0), both_sides=True)
assert len(cands) == 2, f"expected both sides, got {len(cands)}"
built = []
for d in cands:
    S0 = closes[-1]; wing = abs(d.long_strike - d.short_strike)
    ev = SignalEvent(side=d.side, triggered_at="2026-08-24T10:00:00-04:00",
                     underlying_price=S0, confluence={}, confluence_score=4)
    sp = StrikeSuggestion(instrument="SPX", side=d.side, mode="directional_spread",
                          short_strike=d.short_strike, long_strike=d.long_strike,
                          wing_width=wing, multiplier=100,
                          estimated_credit_dollars=d.est_credit,
                          max_loss_dollars=wing*100 - d.est_credit,
                          notional_per_contract=S0*100)
    pt, _note = open_directional_trade(ev, sp, trade_no=len(built)+1, realized_std=r5)
    prm = spy_strike_params(side=pt.side, spx_short_strike=pt.short_strike,
                            spx_credit_dollars=pt.estimated_credit)
    assert pt.strategy == "directional_spread"
    assert pt.contracts >= 1, "sizing produced 0 contracts"
    assert pt.exec_scale == 0.1, f"exec_scale {pt.exec_scale} != 0.1 (SPY 1/10)"
    assert float(prm["short_strike"]).is_integer(), "SPY strike not on the $1 grid"
    assert float(prm["long_strike"]).is_integer(), "SPY long strike not on the $1 grid"
    assert abs(prm["short_strike"] - prm["long_strike"]) >= 1.0, "degenerate spread width"
    built.append((pt.side, prm["short_strike"], prm["long_strike"], pt.contracts))
assert {b[0] for b in built} == {"sell_put_cs", "sell_call_cs"}, f"sides built: {built}"
# no order path touched: TRADING is gated by the caller, and nothing here calls alpaca_trader
assert settings.ALPACA_BASE_URL.startswith("https://paper-api."), "not the paper endpoint"
for b in built:
    print(f"  {b[0]:13} SPY {b[1]:.0f}/{b[2]:.0f} x{b[3]}ct")
print("GATE_G7_PASS")
