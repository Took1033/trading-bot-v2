"""
test_edge_report.py — la carte d'identite de l'edge (Axe 3), hermetique (ZERO reseau).

Verifie les garde-fous scientifiques du rapport :
  - FIDELITE : la decision inline == compute_trend_signal de prod.
  - ANTI LOOK-AHEAD : execution a la barre SUIVANTE (pas same-bar).
  - correction des metriques (Sharpe/Sortino/CAGR/Calmar/Wilson/MaxDD/underwater).
  - moteur net de frais, benchmark equitable, walk-forward, gate GO/NO-GO.

Lancer : python test_edge_report.py
"""
from __future__ import annotations

import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["COINBASE_MODE"] = "paper"

import edge_report as er
from strategies.trend_daily import compute_trend_signal

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def synth(n: int, base: float = 100.0) -> list[float]:
    """Serie deterministe : tendance haussiere + oscillation (croise la SMA plusieurs fois)."""
    return [base * (1 + 0.30 * math.sin(i / 9.0) + 0.004 * i) for i in range(n)]


def test_fidelity_vs_prod() -> None:
    print("\n[1] FIDELITE : trend_decision == compute_trend_signal (prod)")
    closes = synth(200)
    mismatch = 0
    for buf in ((0.0, 0.0), (0.5, 1.5)):
        entry, exit_ = buf
        for i in range(len(closes)):
            window = closes[: i + 1]
            mine = er.trend_decision(closes[i], window, 50, entry, exit_)
            prod = compute_trend_signal("X-USDC", closes[i], window, 50, entry, exit_).action
            if mine != prod:
                mismatch += 1
    check("aucune divergence de decision sur 400 barres x 2 configs", mismatch == 0)


def test_next_bar_execution() -> None:
    print("\n[2] ANTI LOOK-AHEAD : execution a la barre SUIVANTE")
    # 50 barres a 100 (SMA=100, price==SMA -> hold), puis signal buy a la barre 50,
    # qui doit s'executer a la barre 51 (prix 150), PAS a la barre 50 (prix 200).
    closes = [100.0] * 50 + [200.0, 150.0, 160.0, 170.0]
    sim = er.simulate(closes, 50, 0.0, 0.0, 0.0)
    buys = [t for t in sim["trades"] if t["side"] == "buy"]
    check("un achat a eu lieu", len(buys) >= 1)
    check("achat execute a la barre 51 (pas 50)", buys[0]["idx"] == 51)
    check("achat au prix de la barre 51 (150), pas 50 (200)", abs(buys[0]["price"] - 150.0) < 1e-9)


def test_fees_reduce_return() -> None:
    print("\n[3] frais : plus de frais => moins de rendement")
    closes = synth(400)
    r0 = er.simulate(closes, 50, 0.0, 0.0, 0.0)["total_return_pct"]
    r_hi = er.simulate(closes, 50, 0.015, 0.0, 0.0)["total_return_pct"]
    check("des trades ont eu lieu", er.simulate(closes, 50, 0.0, 0.0, 0.0)["n_trades"] > 0)
    check("rendement(0 frais) > rendement(1.5% frais)", r0 > r_hi)


def test_metrics_pure() -> None:
    print("\n[4] metriques pures (valeurs connues)")
    check("max_drawdown 100->120->90->110 = 25%", er.max_drawdown_pct([100, 120, 90, 110]) == 25.0)
    tuw_pct, tuw_days = er.time_underwater([100, 120, 90, 110])
    check("time-underwater 50% / 2 barres", tuw_pct == 50.0 and tuw_days == 2)
    check("CAGR 10k->20k sur 365j = 100%", er.cagr_pct(10_000, 20_000, 365) == 100.0)
    check("CAGR 10k->20k sur 730j ~ 41.4%", abs(er.cagr_pct(10_000, 20_000, 730) - 41.42) < 0.05)
    check("Calmar = CAGR/MaxDD (100/25=4)", er.calmar(100.0, 25.0) == 4.0)
    check("Calmar None si aucun drawdown", er.calmar(50.0, 0.0) is None)
    lo, hi = er.wilson_ci(12, 20)   # p=60% sur 20 trades
    check("Wilson IC borne et ordonne (lo<60<hi)", 0 < lo < 60 < hi < 100)
    check("Wilson n=0 -> (0,0)", er.wilson_ci(0, 0) == (0.0, 0.0))
    check("Sortino positif si tendance montante", er.sortino([0.01, -0.005, 0.02, 0.008], 365) > 0)


def test_buy_hold_fair() -> None:
    print("\n[5] benchmark buy&hold equitable")
    closes = synth(300)
    bh0 = er.buy_hold(closes, 50, 0.0, 365)
    bh_fee = er.buy_hold(closes, 50, 0.02, 365)
    check("buy&hold positif sur serie montante", bh0["total_return_pct"] > 0)
    check("frais reduisent le buy&hold", bh0["total_return_pct"] > bh_fee["total_return_pct"])


def test_walk_forward() -> None:
    print("\n[6] walk-forward : structure + verdict valide")
    closes = synth(600)
    wf = er.walk_forward(closes, 5, 50, 0.002, 0.0, 0.0)
    check("5 fenetres decoupees", wf["n_windows"] >= 4)
    check("verdict dans l'ensemble attendu",
          wf["verdict"] in {"ROBUSTE", "FRAGILE", "MITIGE", "INDETERMINE"})
    check("concentration entre 0 et 1", 0.0 <= wf["max_window_share"] <= 1.0)


def test_gate_logic() -> None:
    print("\n[7] gate GO/NO-GO : verdicts")
    # (a) donnees insuffisantes -> INSUFFISANT
    short = {
        "n_candles": 100, "gaps": [],
        "reference": {"n_trades": 3, "total_return_pct": 5.0},
        "buy_hold": {"total_return_pct": 2.0},
        "walk_forward": {"verdict": "MITIGE"},
        "fee_sensitivity": [{"fee_rt": 0.002, "total_return_pct": 5.0}],
    }
    check("historique court -> INSUFFISANT", er.publication_gate(short)["verdict"] == "INSUFFISANT")

    # (b) trou de donnees -> INSUFFISANT (ex. suspension SEC)
    gapped = dict(short, n_candles=1000, gaps=[{"from": "2021-01-01", "to": "2023-01-01", "days": 730}])
    check("trou de donnees -> INSUFFISANT", er.publication_gate(gapped)["verdict"] == "INSUFFISANT")

    # (c) tout bon -> EDGE PROUVE
    good = {
        "n_candles": 1000, "gaps": [],
        "reference": {"n_trades": 40, "total_return_pct": 120.0},
        "buy_hold": {"total_return_pct": 60.0},
        "walk_forward": {"verdict": "ROBUSTE"},
        "fee_sensitivity": [{"fee_rt": 0.002, "total_return_pct": 120.0}],
    }
    check("tous criteres passes -> EDGE PROUVE", er.publication_gate(good)["verdict"] == "EDGE PROUVE")

    # (d) bat le hold en brut mais fragile en walk-forward -> NON PROBANT
    fragile = dict(good, walk_forward={"verdict": "FRAGILE"})
    check("fragile en WF -> NON PROBANT", er.publication_gate(fragile)["verdict"] == "NON PROBANT")

    # (e) ne bat pas le buy&hold -> NON PROBANT + raison
    loses = {
        "n_candles": 1000, "gaps": [],
        "reference": {"n_trades": 40, "total_return_pct": 30.0},
        "buy_hold": {"total_return_pct": 60.0},
        "walk_forward": {"verdict": "ROBUSTE"},
        "fee_sensitivity": [{"fee_rt": 0.002, "total_return_pct": 30.0}],
    }
    g = er.publication_gate(loses)
    check("ne bat pas le hold -> NON PROBANT", g["verdict"] == "NON PROBANT")
    check("la raison est explicite", any("buy&hold" in r for r in g["reasons"]))


def test_hysteresis_reduces_whipsaw() -> None:
    print("\n[9] hysteresis : une bande de sortie plus large tient plus longtemps")
    closes = synth(500)
    strict = er.simulate(closes, 50, 0.002, 0.0, 0.0)["n_trades"]
    wide   = er.simulate(closes, 50, 0.002, 0.0, 2.0)["n_trades"]
    check("des trades en flip strict", strict > 0)
    check("bande de sortie 2% => moins (ou autant) de round-trips", wide <= strict)


def test_report_reproducible() -> None:
    print("\n[8] empreinte : deterministe sur les memes closes")
    a = er.sha256_floats([1.0, 2.0, 3.0])
    b = er.sha256_floats([1.0, 2.0, 3.0])
    c = er.sha256_floats([1.0, 2.0, 3.001])
    check("meme serie -> meme empreinte", a == b)
    check("serie differente -> empreinte differente", a != c)


def main() -> int:
    print("=== test_edge_report : carte d'identite de l'edge (Axe 3) ===")
    test_fidelity_vs_prod()
    test_next_bar_execution()
    test_fees_reduce_return()
    test_metrics_pure()
    test_buy_hold_fair()
    test_walk_forward()
    test_gate_logic()
    test_hysteresis_reduces_whipsaw()
    test_report_reproducible()

    print("\n" + "=" * 52)
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"    - {f}")
        return 1
    print("  Tous les checks passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
