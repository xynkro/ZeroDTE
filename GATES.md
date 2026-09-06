# Gates: Config E — band-anchored iron condor on a dense ladder

OWNS: backend/app/wave_band_live.py, backend/app/orchestrator.py, backend/app/config.py, scripts/gates/**, GATES.md

Scope: WaveZero trades BOTH sides (condor) at each of 10 ladder slots with the NBBO 10%-of-width
real-credit floor, TP40+breach and no time exit — now CONFIG F (2026-09-06): vol gate OFF,
anchor-floor ON, cushion 0.6%, weekend-proof Alpaca warmup + re-promotion; deployed live on the
paper account with concurrency capped under the 15% drawdown halt. (G6 = the superseded Config E
config gate, abandoned; G14 is its Config F successor.)

- [x] G1: both-sides decision returns two independent sides when each qualifies, one when only one does, none when neither does
  CHECK: .venv/bin/python scripts/gates/g1_both_sides.py
  EXPECT: GATE_G1_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G1_PASS

- [x] G2: the validated single-side core is UNCHANGED — parity with the backtest holds at 378/378 trade-days
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g2_parity.py
  EXPECT: GATE_G2_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=current data: 412 days, mismatch 1, OOS $+40.92 t=6.42 | frozen May-14: 378 / +41.68 / t5.85 reproduced | GATE_G2_PASS

- [x] G3: ladder fires each of the 10 configured slots exactly once per day, never twice, and ignores out-of-window bars
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g3_ladder.py
  EXPECT: GATE_G3_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G3_PASS

- [x] G4: risk envelope is enforced — concurrency cap x per-trade risk stays under the 15% halt gate
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g4_risk.py
  EXPECT: GATE_G4_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=sizing 3ct = $276/trade x 5 concurrent = $1380 (13.8% of $10000) | GATE_G4_PASS

- [x] G5: Config E backtest reproduces positive expectancy independently measured from trade-level stats (not a copied figure)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g5_backtest.py
  EXPECT: GATE_G5_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=EV $+15.35/trade | WR 81.3% avgW $44 avgL $107 | 2.80/day | OOS $+121/d t=10.28 | one-side $+36/d | GATE_G5_PASS

- [ ] G6: live config is armed with Config E values and the app imports clean at the deployed path
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g6_live_config.py
  EXPECT: GATE_G6_PASS
  EVIDENCE: pending

- [x] G7: dry-run builds valid wave-tagged SPX->SPY condor legs for both sides WITHOUT submitting any order
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g7_dryrun.py
  EXPECT: GATE_G7_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=sell_call_cs  SPY 500/501 x6ct | GATE_G7_PASS

- [x] G8: backend is running, feed connected, band armed, and trial ledger intact after deploy
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g8_deployed.py
  EXPECT: GATE_G8_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=backend ok | feed alpaca | armed | ledger 6 trades, n_real=4 $+272 | GATE_G8_PASS

- [x] G9: paper-only safety invariants hold (paper endpoint, no real-money URL, kill-switch untouched)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g9_safety.py
  EXPECT: GATE_G9_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G9_PASS

- [x] G11: NBBO entry plane wired and fails safe to CBOE when no fresh two-sided quote exists
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g11_nbbo.py
  EXPECT: GATE_G11_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=NBBO reachable, returned 0 quoted strikes; fallback wiring verified | GATE_G11_PASS

- [ ] G12: NBBO prices the live chain during market hours and the floor binds on executable credit
  EVIDENCE: pending

- [x] G13: anchor-floor trades a collapsed band at the cushion boundary, is a no-op on wide bands, never enters the cushion; vol gate caller-optional (negative controls)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g13_anchor_floor.py
  EXPECT: GATE_G13_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G13_PASS

- [x] G14: Config F flags are what the engine loads and the orchestrator honours them (gate off, anchor-floor on, cushion 0.6, NBBO diagnostic wired)
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g14_config_f.py
  EXPECT: GATE_G14_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G14_PASS

- [x] G15: feed resilience — boot-time Alpaca retry with backoff, and a re-promotion loop guarded by open positions, feed type, and a 30-min rate limit
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g15_feed_resilience.py
  EXPECT: GATE_G15_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=GATE_G15_PASS

- [x] G16: F6 independently measured on refreshed data — OOS beats E, positive on the zero-trade fortnight, max drawdown at deployed sizing under the 15% halt
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g16_backtest_f6.py
  EXPECT: GATE_G16_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=3ct = 0.3 unit | maxDD 5.8% | OOS F6 $+154.8 vs E $+51.5/sess | fortnight 43 trades $+253 live-scale | WR 85.9% | GATE_G16_PASS

- [x] G17: deployed on the PRIMARY Alpaca feed (boot-retry proof), armed, trial ledger intact
  CHECK: PYTHONPATH=. .venv/bin/python scripts/gates/g17_deployed_f.py
  EXPECT: GATE_G17_PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/xynkro/Trading/ZeroDTE-Wave; path=d634fd18c422/48 entries; output=feed alpaca | armed | ledger 6 trades n_real=4 $+272 | GATE_G17_PASS

- [x] G10: risk-owner accepts the Config E exposure change (up to 5 concurrent positions vs 3, ~20 trades/day vs 3)
  EVIDENCE: Risk owner (Caspar) instructed "build G10" on 2026-08-23 SGT after being shown the
  exposure change in full: 3 -> 5 concurrent positions and 3 -> 20 trades/day, sized at 3
  contracts (~$276/trade) so worst-case concurrent risk is ~$1,380 = 13.8% of the $10k account,
  deliberately under the pre-registered 15% drawdown halt (measured independently by gate G4).
  Paper account PA34H0BS75JB only; the live-money flip remains a separate deliberate act by
  Caspar per CLAUDE.md. Accepted in the same message that raised the unrelated CasaaFinance
  drawdown, which was investigated and shown NOT to involve either ZeroDTE book.

ABANDON: G6 superseded by G14 — the deployed config is Config F (cushion 0.6, vol gate off, anchor-floor on), chosen with evidence in G16 and docs/TRIAL_GATES.md; asserting Config E's 0.4 cushion is no longer the desired outcome.
