"""
run_backtest_universe_walkforward.py — peut-on CHOISIR de bons alts d'avance ? (sans biais in-sample)

`run_backtest_universe_ranked.py` classe sur les 5 ans COMPLETS (in-sample) : il dit
qui a gagne, pas qui GAGNERA. Ce harnais simule la SELECTION elle-meme, hors echantillon :
a chaque fenetre, on classe l'univers sur le seul PASSE, on selectionne le top-3, et on
mesure sa tenue dans la fenetre SUIVANTE. Puis on compare a :
  - BTC seul (concentrer sur l'edge prouve),
  - equi-ponderE de tout l'univers (ne pas choisir du tout).

Question tranchee : "selectionner les past-winners" bat-il "juste tenir BTC" hors
echantillon ? Si le classement ne PERSISTE pas (past-winners ne restent pas gagnants),
alors aucun process de selection d'alt ne marche, et BTC-only est la reponse honnete.

NB : lancer sur la machine de Brice (truststore). Aucune donnee live.
Usage : python run_backtest_universe_walkforward.py [--k 5]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from edge_report import REFERENCE_FEE, SMA_PERIOD, fetch_daily, simulate

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UNIVERSE = [
    "BTC-USDC", "ETH-USDC", "SOL-USDC", "XRP-USDC", "DOGE-USDC",
    "ADA-USDC", "AVAX-USDC", "LINK-USDC", "DOT-USDC", "LTC-USDC",
    "BCH-USDC", "ATOM-USDC", "XLM-USDC", "UNI-USDC", "AAVE-USDC", "NEAR-USDC",
]
TOP_N = 3


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fold_returns(ts: list[int], equity: list[float], bounds: list[int]) -> list:
    """Rendement de l'equite continue sur chaque fenetre calendaire (None si pas de data)."""
    out = []
    for k in range(len(bounds) - 1):
        t0, t1 = bounds[k], bounds[k + 1]
        idx = [i for i in range(len(ts)) if t0 <= ts[i] < t1]
        if len(idx) < 2 or equity[idx[0]] <= 0:
            out.append(None)
        else:
            out.append(equity[idx[-1]] / equity[idx[0]] - 1)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-forward de la SELECTION d'univers")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    K = args.k

    # fetch + equite continue (flip strict) par actif
    data = {}
    all_ts = set()
    for sym in UNIVERSE:
        candles = await fetch_daily(sym.replace("USDC", "USD"), 1825)
        if len(candles) < SMA_PERIOD + 15:
            continue
        ts = [c[0] for c in candles]; closes = [c[1] for c in candles]
        eq = simulate(closes, SMA_PERIOD, REFERENCE_FEE, 0.0, 0.0)["equity"]
        data[sym] = (ts, eq)
        all_ts.update(ts)
        await asyncio.sleep(0.35)

    lo, hi = min(all_ts), max(all_ts)
    bounds = [lo + round(j * (hi - lo) / K) for j in range(K + 1)]
    labels = [datetime.fromtimestamp(b, timezone.utc).strftime("%Y-%m") for b in bounds]
    folds = {sym: _fold_returns(ts, eq, bounds) for sym, (ts, eq) in data.items()}

    print("=" * 90)
    print(f"  WALK-FORWARD DE LA SELECTION — classer sur le PASSE, mesurer le FUTUR · {K} fenetres · flip strict")
    print(f"  peut-on choisir de bons alts d'avance, ou BTC-only est-il la reponse honnete ?")
    print("=" * 90)

    # OOS : a chaque fenetre k>=1, top-N par cumul passe -> rendement fenetre k
    eq_sel = eq_btc = eq_ew = 1.0
    n_win_sel = 0
    overlap = []
    prev_top = None
    print(f"  {'FENETRE OOS':<20}{'TOP-3 (choisis sur le passe)':<34}{'sel':>7}{'BTC':>7}{'equi':>7}")
    print("-" * 90)
    for k in range(1, K):
        scored = []
        for sym, fr in folds.items():
            past = fr[:k]
            if any(x is None for x in past):
                continue
            cum = 1.0
            for x in past:
                cum *= (1 + x)
            scored.append((cum, sym))
        scored.sort(reverse=True)
        top = [s for _, s in scored[:TOP_N]]
        r_sel = _mean([folds[s][k] for s in top])
        r_btc = folds.get("BTC-USDC", [None] * K)[k]
        r_ew = _mean([fr[k] for fr in folds.values()])
        if r_sel is not None:
            eq_sel *= (1 + r_sel); n_win_sel += (1 if r_btc is not None and r_sel > r_btc else 0)
        if r_btc is not None:
            eq_btc *= (1 + r_btc)
        if r_ew is not None:
            eq_ew *= (1 + r_ew)
        if prev_top is not None:
            overlap.append(len(set(top) & set(prev_top)) / TOP_N)
        prev_top = top
        win = f"{labels[k]}→{labels[k+1]}"
        tops = ", ".join(s.replace("-USDC", "") for s in top)
        f = lambda v: f"{v*100:>+6.0f}%" if v is not None else "    — "
        print(f"  {win:<20}{tops:<34}{f(r_sel)}{f(r_btc)}{f(r_ew)}")

    print("-" * 90)
    pers = _mean(overlap)
    def pct(e): return f"{(e-1)*100:+.0f}%"
    print(f"  OOS COMPOSE :  selection top-3 {pct(eq_sel)}   |   BTC seul {pct(eq_btc)}   |   equi-pondere {pct(eq_ew)}")
    print(f"  Selection bat BTC : {n_win_sel}/{K-1} fenetres   |   persistance du top-3 : "
          f"{pers*100:.0f}% de recouvrement d'une fenetre a l'autre" if pers is not None else "")
    verdict = ("SELECTIONNER DES ALTS AJOUTE DE LA VALEUR OOS" if eq_sel > eq_btc and n_win_sel * 2 >= (K - 1)
               else "BTC-ONLY EST LA REPONSE HONNETE — la selection d'alt ne bat pas BTC hors echantillon")
    print(f"  VERDICT : {verdict}")
    print("  (aide a la decision — survivorship non corrige ; la decision reste a Brice)")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
