"""
test_portfolio.py — invariants du backtest de book (run_backtest_portfolio), hermetique.

Un backtest de portefeuille qui viole le cap ou cree de la monnaie mentirait pire
qu'un single-asset. On verifie les invariants durs sur donnees synthetiques.

Lancer : python test_portfolio.py
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["COINBASE_MODE"] = "paper"

from run_backtest_portfolio import INITIAL, hold_at_cap, simulate_book  # noqa: E402

_failures: list[str] = []
BASE = 1_600_000_000


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def mk(prices, signals):
    return {BASE + i * 86400: (prices[i], signals[i]) for i in range(len(prices))}


def test_cap_respected() -> None:
    print("\n[1] le cap d'exposition n'est JAMAIS depasse (rationnement)")
    n = 20
    ts = [BASE + i * 86400 for i in range(n)]
    # 3 actifs qui veulent TOUS entrer (buy a chaque barre), prix plats
    assets = {s: mk([100.0] * n, ["buy"] * n) for s in ("A", "B", "C")}
    res = simulate_book(assets, ts, ["A", "B", "C"], position_pct=0.03, cap=0.06, fee_rt=0.0)
    check("exposition max <= cap 6% (+eps)", res["max_expo_ratio"] <= 0.0601)
    check("cap effectivement atteint (~6%)", res["max_expo_ratio"] >= 0.05)


def test_cash_conservation() -> None:
    print("\n[2] aucune creation de monnaie : 100% hold -> book intact")
    n = 15
    ts = [BASE + i * 86400 for i in range(n)]
    assets = {"A": mk([100.0] * n, ["hold"] * n)}
    res = simulate_book(assets, ts, ["A"], position_pct=0.03, cap=0.06, fee_rt=0.0)
    check("jamais d'entree", res["n_buys"] == 0)
    check("book = capital initial", abs(res["final"] - INITIAL) < 1e-6)


def test_fee_drag() -> None:
    print("\n[3] les frais coutent : A/R sur prix plat -> book < initial")
    # buy, tenu, puis sell, sur prix plat -> perd 2 cotes de frais sur la portion investie
    sig = ["buy", "hold", "hold", "sell", "hold", "hold"]
    px = [100.0] * len(sig)
    ts = [BASE + i * 86400 for i in range(len(sig))]
    res = simulate_book({"A": mk(px, sig)}, ts, ["A"], position_pct=0.03, cap=0.06, fee_rt=0.01)
    check("un round-trip a eu lieu", res["n_buys"] == 1)
    check("book < initial (frais payes)", res["final"] < INITIAL)
    check("perte bornee (< 0.1% du NAV, car ~3% investi)", res["final"] > INITIAL * 0.999)


def test_hold_benchmark() -> None:
    print("\n[4] benchmark hold-a-cap : gagne si le prix monte, borne par le cap")
    closes = [100.0] * 5 + [100.0 + i for i in range(20)]   # warmup puis hausse
    curve = hold_at_cap(closes, warmup=5, cap=0.06, fee_rt=0.0)
    check("courbe de la bonne longueur", len(curve) == len(closes) - 5)
    check("gain positif sur hausse", curve[-1] > INITIAL)
    # 6% investi, prix x1.19 -> gain ~ 6% * 19% ~ +1.1%
    check("gain borne par le cap (~+1%)", curve[-1] < INITIAL * 1.02)


def main() -> int:
    print("=== test_portfolio : invariants du book (Axe 3) ===")
    test_cap_respected()
    test_cash_conservation()
    test_fee_drag()
    test_hold_benchmark()
    print("\n" + "=" * 50)
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"    - {f}")
        return 1
    print("  Tous les checks passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
