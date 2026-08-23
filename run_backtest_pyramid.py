"""
run_backtest_pyramid.py — pyramidage / scale-in vs entree unique.

Compare deux facons d'entrer en trend-following daily :
  - SINGLE : tout-en-un des le franchissement de la SMA (100% a l'entree).
  - PYRAMID : 1/3 a l'entree, +1/3 a +5% du 1er prix, +1/3 a +10% (scale-in sur la
    force). Le cash non deploye ne rapporte rien. Sortie totale au retournement SMA.

Interet theorique : le pyramidage deploie MOINS sur les trades qui echouent vite
(whipsaw) et PLUS sur ceux qui courent -> reduit le risque, mais avec une entree
moyenne plus haute il capte moins de rendement. Ce backtest tranche avec des chiffres.

Reutilise fetch_daily. Net de frais (BACKTEST_FEE_RT, ~moitie par cote).
Usage : python run_backtest_pyramid.py "BTC-USD,ETH-USD,..." 1825
"""
from __future__ import annotations

import asyncio
import sys

from run_backtest_trailing import FEE_RT, SMA, fetch_daily

HALF_FEE = FEE_RT / 2   # frais par cote


def simulate(closes: list[float], pyramid: bool) -> dict:
    equity = 10_000.0
    plan = [(0.0, 1.0)] if not pyramid else [(0.0, 1/3), (0.05, 1/3), (0.10, 1/3)]
    in_pos = False
    first_entry = units = deployed = 0.0
    ti = 0
    wins = losses = 0
    curve: list[float] = []
    for i in range(SMA, len(closes)):
        price = closes[i]
        sma   = sum(closes[i - SMA:i]) / SMA
        if not in_pos:
            if price > sma:
                in_pos, first_entry, units, deployed, ti = True, price, 0.0, 0.0, 0
                spend = equity * plan[0][1]
                units += spend / price * (1 - HALF_FEE)
                deployed += spend
                ti = 1
            curve.append(equity)
        else:
            while ti < len(plan) and price >= first_entry * (1 + plan[ti][0]):
                spend = equity * plan[ti][1]
                units += spend / price * (1 - HALF_FEE)
                deployed += spend
                ti += 1
            curve.append((equity - deployed) + units * price)
            if price < sma:
                proceeds = units * price * (1 - HALF_FEE)
                equity = (equity - deployed) + proceeds
                wins, losses = (wins + 1, losses) if proceeds >= deployed else (wins, losses + 1)
                in_pos, units, deployed = False, 0.0, 0.0
    if in_pos:
        proceeds = units * closes[-1] * (1 - HALF_FEE)
        equity = (equity - deployed) + proceeds
    peak_c, max_dd = (curve[0] if curve else equity), 0.0
    for v in curve:
        peak_c = max(peak_c, v)
        if peak_c > 0:
            max_dd = max(max_dd, (peak_c - v) / peak_c * 100)
    n = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "dd": max_dd, "n": n}


async def main() -> None:
    arg  = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD,ETH-USD,SOL-USD,NEAR-USD,XRP-USD,DOGE-USD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1825
    print("=" * 60)
    print(f"  PYRAMIDAGE vs ENTREE UNIQUE — SMA{SMA} daily — frais {FEE_RT:.1%}")
    print("=" * 60)
    a_s = a_p = d_s = d_p = 0.0
    cnt = 0
    for sym in arg.split(","):
        sym = sym.strip().upper()
        closes = await fetch_daily(sym, days)
        if len(closes) < SMA + 10:
            print(f"\n{sym}: pas assez de donnees")
            continue
        s = simulate(closes, pyramid=False)
        p = simulate(closes, pyramid=True)
        cnt += 1
        a_s += s["ret"]; a_p += p["ret"]; d_s += s["dd"]; d_p += p["dd"]
        print(f"\n{sym}")
        print(f"  single  : {s['ret']:>+8.1f}%   maxDD {s['dd']:>5.1f}%")
        print(f"  pyramid : {p['ret']:>+8.1f}%   maxDD {p['dd']:>5.1f}%   "
              f"[{p['ret']-s['ret']:+.1f} pts rendement, {p['dd']-s['dd']:+.1f} pts DD]")
    if cnt:
        print("\n" + "=" * 60)
        print(f"  MOYENNE : single {a_s/cnt:+.1f}% (DD {d_s/cnt:.0f}%)   "
              f"pyramid {a_p/cnt:+.1f}% (DD {d_p/cnt:.0f}%)")
        print("  Regle : pyramid vaut le coup s'il BAISSE nettement le DD sans trop")
        print("  amputer le rendement. Sinon l'entree unique (simple) gagne.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
