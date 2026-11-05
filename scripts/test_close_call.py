#!/usr/bin/env python
"""Pure-logic checks for scripts/close_call.py. No network. Run: python scripts/test_close_call.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from close_call import score_row, tally, validate, extract_json, first_sentence, call_text, score_text

CALL = {"direction": "down", "called_close": 757.40, "price_0935_iex": 759.45}


def raises(fn):
    try:
        fn()
    except (ValueError, TypeError):
        return True
    return False


# Night 1 must reproduce the hand-scored row.
s = score_row(CALL, 759.45, 752.10)
assert s["direction_correct"] and s["price_miss"] == 5.30 and s["price_miss_pct_open"] == 0.698
assert s["baseline_0935_miss_pct_open"] == 0.968

assert not score_row(CALL, 750.00, 752.10)["direction_correct"]
flat = score_row(CALL, 752.10, 752.10)  # close equal to open is a miss
assert flat["actual_direction"] == "flat" and not flat["direction_correct"]

none = score_row({"no_call": True}, 759.45, 752.10)
assert none["no_call"] and not none["direction_correct"] and none["price_miss_pct_open"] is None

assert validate({"direction": "down", "called_close": 757.4}, 759.45) == ("down", 757.4)
assert raises(lambda: validate({"direction": "down", "called_close": 760}, 759.45))  # wrong side
assert raises(lambda: validate({"direction": "up", "called_close": 759.45}, 759.45))  # on the open
assert raises(lambda: validate({"direction": "flat", "called_close": 760}, 759.45))
assert raises(lambda: validate({"direction": "up", "called_close": 900}, 759.45))  # absurd
assert raises(lambda: validate({"direction": "up", "called_close": None}, 759.45))

rows = [{"score": s}, {"score": none}, {"date": "unscored"}]
assert tally(rows) == (2, 1, 0.7)
assert tally([]) == (0, 0, None)

assert extract_json('text {"direction": "up"} more') == {"direction": "up"}
assert raises(lambda: extract_json("no json"))

assert first_sentence("Gap up 1.15% on news. Base rate 40%.") == "Gap up 1.15% on news."  # decimals don't split
assert first_sentence(None) == ""

day1 = {"date": "2026-11-02", "direction": "down", "called_close": 757.40, "score": s}
post_call = call_text({"direction": "down", "called_close": 761.0, "reason": "Big gap up. Low conviction."}, [day1])
assert post_call == "📍 SPY today: DOWN, close 761.00\nBig gap up.\n1 of 1 right so far. No trade."
assert "No scored days yet." in call_text({"direction": "up", "called_close": 1.0, "reason": "x"}, [])

post_score = score_text(day1, [day1])
assert post_score == ("📍 SPY result, Mon 2 Nov: called DOWN, 757.40\n"
                      "Opened 759.45, closed 752.10. Direction right, price off by 0.70%.\n"
                      "Score: 1 of 1 right, typical miss 0.7%. Pass: 11 of 14, under 0.38%.")
skipped = {"date": "2026-11-02", "no_call": True, "score": none}
assert "no call made, counts as a miss" in score_text(skipped, [skipped])
print("close_call: all checks passed")
