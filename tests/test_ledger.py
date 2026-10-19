"""Synthetic checks for the ledger statistics: python tests/test_ledger.py"""
import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger  # noqa: E402


def days(n):
    start = dt.date(2026, 1, 1)
    return [(start + dt.timedelta(i)).isoformat() for i in range(n)]


def market_from_moves(ds, moves):
    p, closes = 0.5, {}
    for d, m in zip(ds, moves):
        closes[d] = round(p, 4)
        p = min(0.99, max(0.01, p + m))
    return closes


def test_real_lead_is_found():
    rng = random.Random(1)
    ds = days(120)
    sig = {d: rng.random() * 10 for d in ds}
    # Tomorrow's move is large exactly when today's signal is high.
    moves = [(sig[d] / 100) * rng.choice([-1, 1]) for d in ds]
    t = ledger.run_test(sig, market_from_moves(ds, moves), ds[0])
    assert t["rho"] > 0.5 and t["p"] < 0.05, t


def test_noise_is_not_found():
    rng = random.Random(2)
    ds = days(120)
    sig = {d: rng.random() for d in ds}
    moves = [rng.gauss(0, 0.02) for _ in ds]
    t = ledger.run_test(sig, market_from_moves(ds, moves), ds[0])
    assert t["p"] > 0.05, t


def test_weekday_rhythm_is_not_a_lead():
    rng = random.Random(3)
    ds = days(140)
    wk = {d: dt.date.fromisoformat(d).weekday() < 5 for d in ds}
    sig = {d: (40 if wk[d] else 12) + rng.random() * 3 for d in ds}
    moves = [(0.02 if wk[d] else 0.005) * rng.choice([-1, 1]) * rng.random() for d in ds]
    t = ledger.run_test(sig, market_from_moves(ds, moves), ds[0])
    assert abs(t["rho"]) < 0.3, t


def test_bh_and_verdicts():
    q = ledger.bh([0.01, 0.04, 0.5])
    assert q[0] <= q[1] <= q[2] and abs(q[0] - 0.03) < 1e-9
    assert ledger.verdict({"n": 10}) == "too_early"
    assert ledger.verdict({"n": 40, "p": 0.5, "q": 0.6, "rho": 0.1}) == "no_effect"
    assert ledger.verdict({"n": 40, "p": 0.01, "q": 0.05, "rho": 0.4}) == "holds_up"


def test_keyword_regions():
    regions = ledger.load_regions()
    assert "ukraine_blacksea" in ledger.match_regions("Russia x Ukraine ceasefire", regions)
    assert ledger.match_regions("Will the Fed cut rates?", regions) == ["global"]
    assert "europe" not in ledger.match_regions("Will Trump meet Putin?", regions)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
