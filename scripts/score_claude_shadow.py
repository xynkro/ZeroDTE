"""Score the Claude shadow analyst against broker-truth outcomes.

Joins backend/data/claude_shadow.jsonl (per-slot reads) to debrief_log.jsonl
(per-day real net) and the condor history (per-slot outcomes). Answers ONE
question: does 'hostile' predict stops/red days better than chance?
Run after N≥20 clean nights; before that this prints the honest 'not enough
data' and refuses conclusions (small-N discipline, same as improve_loop).

Run:  PYTHONPATH=. .venv/bin/python scripts/score_claude_shadow.py
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "backend" / "data"
MIN_N_DAYS = 20


def main():
    reads = []
    p = DATA / "claude_shadow.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("read"):
                    reads.append(r)
            except json.JSONDecodeError:
                continue
    day_net = {}
    dl = DATA / "debrief_log.jsonl"
    if dl.exists():
        for line in dl.read_text().splitlines():
            try:
                d = json.loads(line)
                if d.get("ic_real_net") is not None:
                    day_net[d["date"]] = d["ic_real_net"]
            except json.JSONDecodeError:
                continue

    days = sorted({r["date"] for r in reads})
    print(f"shadow reads: {len(reads)} across {len(days)} day(s); "
          f"scored days available: {len([d for d in days if d in day_net])}")
    if len(days) < MIN_N_DAYS:
        print(f"⏳ N={len(days)} < {MIN_N_DAYS} — NOT enough for a verdict. "
              f"Keep logging; no conclusions at small N.")
    # Descriptive table regardless (visible drift check, no verdicts)
    by_env = defaultdict(list)
    for r in reads:
        net = day_net.get(r["date"])
        if net is not None:
            by_env[r["read"]["condor_env"]].append(net)
    for env in ("friendly", "neutral", "hostile"):
        vals = by_env.get(env, [])
        if vals:
            print(f"  {env:9} n={len(vals):3}  mean day-net ${sum(vals)/len(vals):+7.0f}  "
                  f"green {100*sum(1 for v in vals if v>0)/len(vals):.0f}%")
    if len(days) >= MIN_N_DAYS:
        f, h = by_env.get("friendly", []), by_env.get("hostile", [])
        if f and h:
            spread = (sum(f)/len(f)) - (sum(h)/len(h))
            print(f"\nfriendly-minus-hostile day-net spread: ${spread:+.0f} "
                  f"(predictive if robustly > 0; take to improve_loop for the gate decision)")


if __name__ == "__main__":
    main()
