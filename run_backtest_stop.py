"""
run_backtest_stop.py — valide le STOP CATASTROPHE.

Rejoue le trend-following SMA daily avec un stop-loss dur (coupe si la position perd
plus de X% depuis l'entree, en plus de la sortie SMA). Compare net % et surtout
MAX DRAWDOWN par niveau de stop : un bon stop catastrophe reduit le pire drawdown
sans trop amputer le rendement.

⚠️ Approximation : le stop est teste a la CLOTURE journaliere. Un vrai stop intraday
declencherait plus tot / plus haut -> ce backtest SOUS-estime sa protection.

Usage :
    python run_backtest_stop.py                 # 6 bots, 5 ans
    python run_backtest_stop.py "BTC-USD" 1825
"""
from __future__ import annotations

import asyncio
import sys

from run_backtest_trailing import FEE_RT, SMA, fetch_daily

STOPS = [0.0, 0.10, 0.15, 0.20, 0.25]   # 0 = pas de stop


def simulate(closes: list[float], stop: float) -> dict:
    equity = 10_000.0
    in_pos = False
    entry  = qty = 0.0
    wins   = losses = 0
    curve: list[float] = []
    for i in range(SMA, len(closes)):
        price = closes[i]
        sma   = sum(closes[i - SMA:i]) / SMA
        curve.append(qty * price if in_pos else equity)
        if not in_pos:
            if price > sma:
                in_pos, entry, qty = True, price, equity / price
        else:
            hit_stop = stop > 0 and price <= entry * (1 - stop)
            hit_sma  = price < sma
            if hit_stop or hit_sma:
                equity = qty * price * (1 - FEE_RT)
                wins, losses = (wins + 1, losses) if price >= entry else (wins, losses + 1)
                in_pos, qty = False, 0.0
    if in_pos:
        equity = qty * closes[-1] * (1 - FEE_RT)
        wins, losses = (wins + 1, losses) if closes[-1] >= entry else (wins, losses + 1)
    peak_c, max_dd = (curve[0] if curve else equity), 0.0
    for v in curve:
        peak_c = max(peak_c, v)
        if peak_c > 0:
            max_dd = max(max_dd, (peak_c - v) / peak_c * 100)
    n = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "dd": max_dd, "n": n,
            "win": (wins / n * 100) if n else 0.0}


async def main() -> None:
    arg  = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD,ETH-USD,SOL-USD,NEAR-USD,XRP-USD,DOGE-USD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1825

    print("=" * 62)
    print(f"  STOP CATASTROPHE — trend SMA{SMA} daily — frais {FEE_RT:.1%}")
    print("=" * 62)

    agg_ret = {s: 0.0 for s in STOPS}
    agg_dd  = {s: 0.0 for s in STOPS}
    cnt = 0
    for sym in arg.split(","):
        sym = sym.strip().upper()
        closes = await fetch_daily(sym, days)
        if len(closes) < SMA + 10:
            print(f"\n{sym}: pas assez de donnees")
            continue
        cnt += 1
        print(f"\n{sym}")
        print(f"  {'stop':>8} {'net %':>9} {'maxDD %':>9} {'trades':>7}")
        for s in STOPS:
            m = simulate(closes, s)
            agg_ret[s] += m["ret"]
            agg_dd[s]  += m["dd"]
            label = "OFF" if s == 0 else f"-{s*100:.0f}%"
            print(f"  {label:>8} {m['ret']:>+8.1f} {m['dd']:>8.1f} {m['n']:>7}")

    if cnt:
        print("\n" + "=" * 62)
        print(f"  MOYENNE (sur {cnt} symboles) :")
        print(f"  {'stop':>8} {'net %':>9} {'maxDD %':>9}")
        for s in STOPS:
            label = "OFF" if s == 0 else f"-{s*100:.0f}%"
            print(f"  {label:>8} {agg_ret[s]/cnt:>+8.1f} {agg_dd[s]/cnt:>8.1f}")
        print("\n  Lecture : cherche un stop qui BAISSE le maxDD en gardant un net proche")
        print("  de OFF. S'il coupe le net autant que le DD -> il whipsaw, laisser OFF.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
