"""G4: concurrency cap x per-trade risk stays under the pre-registered 15% halt gate."""
import sys
sys.path.insert(0, '.')
from backend.app.config import settings
from backend.app.directional_spread_manager import recommend_contracts

acct = settings.ACCOUNT_SIZE_USD
halt_pct = 15.0
max_loss_ct = 92.0   # SPY 1-wide spread, ~$8 credit -> ~$92 risk/contract
n, _note = recommend_contracts(max_loss_ct, 4, 4)
per_trade = n * max_loss_ct
conc = settings.MAX_CONCURRENT_POSITIONS
worst = per_trade * conc
pct = 100.0 * worst / acct
assert n >= 1, "sizing produced 0 contracts"
assert per_trade <= settings.SIZE_CAP_USD + max_loss_ct, f"per-trade ${per_trade} exceeds cap"
assert pct < halt_pct, f"worst concurrent exposure {pct:.1f}% >= {halt_pct}% halt gate"
assert settings.MAX_TRADES_PER_DAY >= 10, f"daily cap {settings.MAX_TRADES_PER_DAY} throttles the ladder"
assert settings.DAILY_LOSS_LIMIT_PCT > 0, "daily loss limit disabled"
print(f"  sizing {n}ct = ${per_trade:.0f}/trade x {conc} concurrent = ${worst:.0f} ({pct:.1f}% of ${acct:.0f})")
print("GATE_G4_PASS")
