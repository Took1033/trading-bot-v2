"""
test_backtester.py — métriques standard du Backtester (Axe 3).

Vérifie les fonctions pures (Sharpe, profit factor, exposition), un run() complet
sur données synthétiques, et la rétrocompatibilité du BacktestResult enrichi.
Aucun réseau : données et stratégie sont synthétiques.

Lancer : python test_backtester.py
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["COINBASE_MODE"] = "paper"

from strategies.backtester import (  # noqa: E402
    BacktestResult,
    Backtester,
    compute_exposure,
    compute_profit_factor,
    compute_sharpe,
)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def test_profit_factor() -> None:
    print("\n[1] profit factor")
    check("gains 30 / pertes 5 -> 6.0", compute_profit_factor([10, 20, -5]) == 6.0)
    check("aucune perte -> None (indéfini)", compute_profit_factor([10, 20]) is None)
    check("aucun gain -> 0.0", compute_profit_factor([-5, -3]) == 0.0)
    check("vide -> 0.0", compute_profit_factor([]) == 0.0)


def test_exposure() -> None:
    print("\n[2] exposition")
    check("5/10 -> 50%", compute_exposure(5, 10) == 50.0)
    check("0/10 -> 0%", compute_exposure(0, 10) == 0.0)
    check("garde-fou division par 0", compute_exposure(3, 0) == 0.0)


def test_sharpe() -> None:
    print("\n[3] Sharpe")
    check("moins de 2 points -> 0.0", compute_sharpe([100.0]) == 0.0)
    check("équité plate (variance nulle) -> 0.0", compute_sharpe([100, 100, 100, 100]) == 0.0)
    eq = [100.0, 110.0, 105.0, 115.0, 120.0]
    s = compute_sharpe(eq)
    check("courbe globalement montante -> Sharpe > 0", s > 0)
    s_down = compute_sharpe([120.0, 115.0, 118.0, 108.0, 100.0])
    check("courbe globalement descendante -> Sharpe < 0", s_down < 0)
    # Annualisation : *sqrt(periods_per_year), à l'arrondi près.
    s_ann = compute_sharpe(eq, periods_per_year=4)   # sqrt(4) = 2
    check("annualisation ~ x2 pour ppy=4", abs(s_ann - s * 2) < 0.01)


def test_backtest_run_metrics() -> None:
    print("\n[4] run() complet sur données synthétiques (buy-and-hold, prix montants)")

    class Sig:
        def __init__(self, action, confidence=1.0):
            self.action = action
            self.confidence = confidence

    async def buy_and_hold(symbol, prices):
        return Sig("buy", 1.0)   # le BT n'achète qu'une fois (qty_held == 0 requis)

    prices = [100.0 + i for i in range(60)]   # strictement croissants, > warmup
    bt = Backtester(strategy=buy_and_hold, initial_usdc=10_000.0)
    res = asyncio.run(bt.run("SYN-USDC", prices, periods_per_year=365))

    check("rendement positif (prix montants)", res.total_return > 0)
    check("1 trade clôturé (liquidation finale)", res.n_trades == 1)
    check("100% gagnants", res.win_rate == 100.0)
    check("profit factor None (aucune perte)", res.profit_factor is None)
    check("exposition élevée (>90%)", res.exposure > 90.0)
    check("Sharpe renseigné (>0)", res.sharpe > 0)
    check("summary() ne casse pas avec profit_factor None", "∞" in res.summary())


def test_backward_compat() -> None:
    print("\n[5] rétrocompatibilité : BacktestResult sans les nouveaux champs")
    raised = False
    try:
        r = BacktestResult(
            symbol="X-USDC", initial_usdc=100.0, final_usdc=110.0,
            total_return=10.0, n_trades=1, n_wins=1, win_rate=100.0, max_drawdown=0.0,
        )
        _ = r.summary()
    except Exception as exc:
        raised = True
        print(f"       a levé : {exc!r}")
    check("construction sans nouveaux champs -> OK", raised is False)
    check("défaut sharpe = 0.0", r.sharpe == 0.0)
    check("défaut profit_factor = None", r.profit_factor is None)


def main() -> int:
    print("=== test_backtester : métriques standard (Axe 3) ===")
    test_profit_factor()
    test_exposure()
    test_sharpe()
    test_backtest_run_metrics()
    test_backward_compat()

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
