"""
run_backtest_band_walkforward.py — la bande d'hysteresis survit-elle HORS echantillon ?

Le +22 pts de l'exit-1% a ete choisi par argmax sur la grille complete, IN-SAMPLE
(run_backtest_hysteresis balaie 7 valeurs sur les memes 5 ans). C'est l'ironie exacte
que le walk-forward combat. Le seul test honnete : walk-forwarder LA BANDE elle-meme.

A chaque fenetre de test i : on SELECTIONNE la meilleure bande sur TOUT le passe
(train, in-sample), on l'APPLIQUE a la fenetre i (out-of-sample), et on compare a
"toujours flip strict (0%)". Question tranchee :
  - Si la selection adaptative BAT le flip strict OOS -> choisir une bande a de la valeur.
  - Sinon -> le +22 pts etait un artefact d'optimisation in-sample (curve-fitting).

NB : lancer sur la machine de Brice (truststore). Aucune donnee live.
Usage : python run_backtest_band_walkforward.py [--k 6]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from edge_report import INITIAL, LIVE_FLEET, REFERENCE_FEE, SMA_PERIOD, fetch_daily, simulate

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GRID = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]   # la vraie grille du harnais de prod


def _ret(closes: list[float], buf: float) -> float:
    return simulate(closes, SMA_PERIOD, REFERENCE_FEE, 0.0, buf)["total_return_pct"]


def select_band(train_closes: list[float]) -> float:
    """Argmax du rendement in-sample sur la grille (mirroir de la procedure de prod)."""
    return max(GRID, key=lambda b: _ret(train_closes, b))


def walk_forward_band(closes: list[float], ts: list[int], k: int) -> dict:
    """Anchored/expanding : train = tout le passe avant la fenetre de test."""
    n = len(closes)
    bound = [round(j * n / k) for j in range(k + 1)]
    folds = []
    eq_adapt = INITIAL
    eq_strict = INITIAL
    for i in range(1, k):                      # fenetre 0 = train initial seul
        tr = closes[: bound[i]]
        te = closes[bound[i]: bound[i + 1]]
        if len(tr) < SMA_PERIOD + 15 or len(te) < SMA_PERIOD + 15:
            continue
        best = select_band(tr)                 # choisi SUR LE PASSE uniquement
        r_adapt = _ret(te, best)               # applique au FUTUR (OOS)
        r_strict = _ret(te, 0.0)
        eq_adapt *= (1 + r_adapt / 100)
        eq_strict *= (1 + r_strict / 100)
        d0 = datetime.fromtimestamp(ts[bound[i]], timezone.utc).strftime("%Y-%m")
        d1 = datetime.fromtimestamp(ts[min(bound[i + 1], n - 1)], timezone.utc).strftime("%Y-%m")
        folds.append({"win": f"{d0}->{d1}", "band": best, "adapt": r_adapt,
                      "strict": r_strict, "edge": round(r_adapt - r_strict, 1)})
    n_win = sum(1 for f in folds if f["adapt"] > f["strict"])
    return {"folds": folds, "eq_adapt": eq_adapt, "eq_strict": eq_strict,
            "oos_adapt_pct": round((eq_adapt / INITIAL - 1) * 100, 1),
            "oos_strict_pct": round((eq_strict / INITIAL - 1) * 100, 1),
            "n_win": n_win, "n_folds": len(folds)}


async def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-forward de la bande d'hysteresis")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--symbols", default=",".join(LIVE_FLEET))
    args = ap.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 78)
    print(f"  WALK-FORWARD DE LA BANDE — selection in-sample -> application OOS, {args.k} fenetres")
    print(f"  grille {GRID}  |  frais {REFERENCE_FEE*100:.1f}%  |  question : tuner la bande paie-t-il OOS ?")
    print("=" * 78)

    tot_adapt = tot_strict = 0.0
    tot_win = tot_folds = 0
    for sym in syms:
        candles = await fetch_daily(sym.replace("USDC", "USD"), 1825)
        ts = [c[0] for c in candles]; closes = [c[1] for c in candles]
        r = walk_forward_band(closes, ts, args.k)
        print(f"\n  {sym}  (OOS compose : bande adaptative {r['oos_adapt_pct']:+.0f}%  "
              f"vs flip strict {r['oos_strict_pct']:+.0f}%  |  gagne {r['n_win']}/{r['n_folds']} fenetres)")
        for f in r["folds"]:
            flag = "  <= bande gagne" if f["edge"] > 0 else ("  (strict gagne)" if f["edge"] < 0 else "")
            print(f"     {f['win']:<16} bande choisie {f['band']:.1f}%   "
                  f"OOS adapt {f['adapt']:>+6.1f}%  strict {f['strict']:>+6.1f}%  edge {f['edge']:>+5.1f}{flag}")
        tot_adapt += r["oos_adapt_pct"]; tot_strict += r["oos_strict_pct"]
        tot_win += r["n_win"]; tot_folds += r["n_folds"]
        await asyncio.sleep(0.35)

    print("\n" + "=" * 78)
    verdict = ("LA BANDE PAIE OOS" if tot_adapt > tot_strict and tot_win * 2 >= tot_folds
               else "ARTEFACT IN-SAMPLE (la bande ne bat pas le flip strict OOS)")
    print(f"  AGREGAT {len(syms)} actifs : adaptatif {tot_adapt:+.0f}% vs strict {tot_strict:+.0f}% "
          f"| bande gagne {tot_win}/{tot_folds} fenetres OOS")
    print(f"  VERDICT : {verdict}")
    print(f"  (aide a la decision — la decision live et tout acte restent a Brice)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
