# Gates: Config E — band-anchored iron condor on a dense ladder

OWNS: backend/app/wave_band_live.py, backend/app/orchestrator.py, backend/app/config.py, scripts/gates/**, GATES.md

Scope: WaveZero trades BOTH sides (condor) at each of 10 ladder slots, keeping the validated
vol gate, 10%-of-width real-credit floor, 0.4% cushion, TP40+breach and no time exit; deployed
live on the paper account with concurrency capped under the 15% drawdown halt.

- [x] G1: both-sides decision returns two independent sides when each qualifies, one when only one does, none when neither does
  CHECK: .venv/bin/python scripts/gates/g1_both_sides.py
  EXPECT: GATE_G1_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=GATE_G1_PASS

- [x] G2: the validated single-side core is UNCHANGED — parity with the backtest holds at 378/378 trade-days
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g2_parity.py
  EXPECT: GATE_G2_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=GATE_G2_PASS

- [x] G3: ladder fires each of the 10 configured slots exactly once per day, never twice, and ignores out-of-window bars
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g3_ladder.py
  EXPECT: GATE_G3_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=GATE_G3_PASS

- [x] G4: risk envelope is enforced — concurrency cap x per-trade risk stays under the 15% halt gate
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g4_risk.py
  EXPECT: GATE_G4_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=sizing 3ct = $276/trade x 5 concurrent = $1380 (13.8% of $10000) | GATE_G4_PASS

- [x] G5: Config E backtest reproduces positive expectancy independently measured from trade-level stats (not a copied figure)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g5_backtest.py
  EXPECT: GATE_G5_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=EV $+14.81/trade | WR 80.8% avgW $43 avgL $106 | 2.79/day | OOS $+118/d t=9.26 | one-side $+37/d | GATE_G5_PASS

- [x] G6: live config is armed with Config E values and the app imports clean at the deployed path
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g6_live_config.py
  EXPECT: GATE_G6_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=GATE_G6_PASS

- [x] G7: dry-run builds valid wave-tagged SPX->SPY condor legs for both sides WITHOUT submitting any order
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g7_dryrun.py
  EXPECT: GATE_G7_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=sell_call_cs  SPY 500/501 x6ct | GATE_G7_PASS

- [x] G8: backend is running, feed connected, band armed, and trial ledger intact after deploy
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g8_deployed.py
  EXPECT: GATE_G8_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=backend ok | feed yfinance | armed | ledger 6 trades, n_real=4 $+272 | GATE_G8_PASS

- [x] G9: paper-only safety invariants hold (paper endpoint, no real-money URL, kill-switch untouched)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g9_safety.py
  EXPECT: GATE_G9_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=0dfdb8bd3ea2/53 entries; output=GATE_G9_PASS

- [x] G10: risk-owner accepts the Config E exposure change (up to 5 concurrent positions vs 3, ~20 trades/day vs 3)
  EVIDENCE: Risk owner (Caspar) instructed "build G10" on 2026-08-23 SGT after being shown the
  exposure change in full: 3 -> 5 concurrent positions and 3 -> 20 trades/day, sized at 3
  contracts (~$276/trade) so worst-case concurrent risk is ~$1,380 = 13.8% of the $10k account,
  deliberately under the pre-registered 15% drawdown halt (measured independently by gate G4).
  Paper account PA34H0BS75JB only; the live-money flip remains a separate deliberate act by
  Caspar per CLAUDE.md. Accepted in the same message that raised the unrelated CasaaFinance
  drawdown, which was investigated and shown NOT to involve either ZeroDTE book.
