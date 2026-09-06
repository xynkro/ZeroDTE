"""G15: boot-time Alpaca retry + safe re-promotion exist and are guarded."""
import sys, inspect; sys.path.insert(0, '.')
from backend.app import orchestrator as om
from backend.app.config import settings as s
from backend.app.alpaca_feed import AlpacaFeed
start=inspect.getsource(om.Orchestrator.start)
assert "ALPACA_FEED_RETRIES" in start and "for _try in range" in start, "no boot retry"
assert s.ALPACA_FEED_RETRIES>=3 and s.ALPACA_FEED_RETRY_SEC>=5
assert "_feed_repromote_loop()" in start, "re-promote loop not registered"
rp=inspect.getsource(om.Orchestrator._feed_repromote_loop)
for must in ('any(not t.closed for t in self.paper_trades)', 'feed_type", "") == "alpaca"', "SIGTERM", "1800", "_persist_state()"):
    assert must in rp, f"re-promote missing guard: {must}"
assert hasattr(AlpacaFeed, "_fetch_bars") and hasattr(AlpacaFeed, "stop")
print("GATE_G15_PASS")
