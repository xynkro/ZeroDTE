# DESK CHARTER — two books, one account, two agents

**Read this at the start of every session, right after HANDOFF.md.**

Two trading books share ONE Alpaca paper account and ONE backend process, each
worked by its own agent. This file is the boundary that keeps them from
conflating P&L, contaminating numbers, or editing the same code at the same time.

## The two books
| Book | Agent | Trades | Contract size (today) |
|------|-------|--------|-----------------------|
| **MEIC** | 🦅 MEICZero | SPY 4-leg iron condors, 11/12/13/14 ET ladder | 1 |
| **WAVE** | 🌊 WaveZero | SPY 2-leg directional credit spreads | 6 |

CasaaFinance (a SEPARATE project) also trades this paper account — equities AND
options (e.g. LMT). **Neither book ever counts or touches CasaaFinance positions.**

## Rule 0 — broker truth is the only scoreboard
Real Alpaca fills decide everything: the headline number, the kill-switch, the
gates. Model / estimated P&L may be SHOWN but must be labelled "model" and never
drives a decision. Every fake number this desk ever produced was a model number.

## Rule 1 — one ledger, one filter (nobody hand-sums fills)
All P&L comes from `backend/app/broker_ledger.py`:
- `is_our_option(symbol)` — root filter, **SPY/XSP/SPX only** (this is what keeps
  CasaaFinance's LMT and equities out; "it's an option → it's ours" is FALSE).
- `realized_by_book(fills, orders)` — splits by book via the order tag.
- `reconcile_ledger.py` — the canonical per-day, per-book table.

Do NOT write a second fill-summer or a second option filter. Import these.

## Rule 2 — tag every order at submission
Every Alpaca order sets `client_order_id` = `meic-{id}` or `wave-{id}`, on
**entries AND closes**. New order path? It tags — no exceptions. The tag is the
only reliable book separator (leg-count fails: MEIC's stop-close is a 2-leg order,
same shape as a Wave spread). Untagged → surfaced as "untagged", never silently bucketed.

## Rule 3 — contamination is loud, never silent
If the ledger sees a fill that isn't SPY/XSP/SPX, or isn't tagged meic/wave, it
surfaces it (`⚠️ untagged/foreign — not counted`). A stray LMT shows as a flag,
not a mystery $195 found three sessions later.

## Rule 4 — one EOD message
A single Telegram per session: `✅/❌ total (real Alpaca) · 🦅 MEIC · 🌊 Wave`,
from real fills. No second contradictory summary.

## Code ownership — who edits what
| Scope | Owner | Files |
|-------|-------|-------|
| MEIC | 🦅 MEICZero | IC build/submit/stop paths in `orchestrator.py`; `meic_*.py`; `account_sim.py` |
| WAVE | 🌊 WaveZero | `directional_spread_manager.py`; wave submit/exit paths; `wave_*.py` |
| **SHARED — coordinate before touching** | — | `broker_ledger.py`, `alpaca_trader.py`, `config.py`, the EOD send block in `orchestrator._fire_eod_summary`, `OWNERS.md`, `HANDOFF.md`, `CLAUDE.md` |

**Shared-file protocol:** ping the other agent / the user before editing shared
code. If you must, leave a one-line note in HANDOFF.md so the other session sees it.
Better: each session works in its own `git worktree` / branch and merges (zero
live-edit collisions).

## Conflict referee
- **Numbers disagree?** Run `reconcile_ledger.py`. That's the tiebreaker — not the
  debrief, not a hand-count, not the dashboard.
- **Code conflict?** The file's owner decides. Shared file → the user decides.

## If you scale to real money
The clean separation is **two accounts**: MEIC on Alpaca account A, Wave on B (or
IBKR), two backends. Then the books can NEVER touch each other's money or code.
Do this when paper validates, not before.
