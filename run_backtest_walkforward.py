"""
run_backtest_walkforward.py — test de ROBUSTESSE d'un edge trend.
Un rendement 5 ans peut etre porte par UNE seule fenetre bull (ex SUI +670%).
On decoupe l'historique en K blocs consecutifs et on rejoue le trend SMA50 sur
chacun, independamment. Robuste = positif sur la majorite des blocs, pas juste 1.

Usage :
  python run_backtest_walkforward.py "SUI-USD,INJ-USD,NEAR-USD,AAVE-USD,ARB-USD,MANA-USD" 1825 5
A LANCER SUR LA MACHINE DE BRICE (truststore -> SSL Coinbase OK).
"""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import asyncio
import os
import sys

from run_backtest_trailing import SMA, fetch_daily

FEE_RT = float(os.getenv("BACKTEST_FEE_RT", "0.002"))
HALF   = FEE_RT / 2


def sim(closes: list[float]) -> float:
    """Rendement compose (%) du trend SMA50 long-only sur une serie de clotures."""
    equity = 10_000.0
    in_pos = False
    units = 0.0
    for i in range(SMA, len(closes)):
        price = closes[i]
        sma = sum(closes[i - SMA:i]) / SMA
        if not in_pos:
            if price > sma:
                in_pos = True
                units = equity / price * (1 - HALF)
        else:
            if price < sma:
                equity = units * price * (1 - HALF)
                in_pos = False
                units = 0.0
    if in_pos:
        equity = units * closes[-1] * (1 - HALF)
    return (equity / 10_000 - 1) * 100


async def main() -> None:
    syms = (sys.argv[1] if len(sys.argv) > 1 else "SUI-USD,INJ-USD,NEAR-USD,AAVE-USD,ARB-USD,MANA-USD").split(",")
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1825
    K    = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    print("=" * 72)
    print(f"  WALK-FORWARD trend SMA{SMA} - {K} blocs consecutifs - frais {FEE_RT:.2%}")
    print("=" * 72)
    print(f"  (chaque bloc ~= {days//K//30} mois, rejoue independamment)\n")
    for raw in syms:
        sym = raw.strip().upper()
        closes = await fetch_daily(sym, days)
        await asyncio.sleep(0.35)
        if len(closes) < (SMA + 20) * 2:
            print(f"  {sym:<10} histo trop court pour un walk-forward fiable ({len(closes)}j)")
            continue
        block = len(closes) // K
        rets = []
        for k in range(K):
            seg = closes[k * block:(k + 1) * block] if k < K - 1 else closes[k * block:]
            if len(seg) >= SMA + 15:
                rets.append(sim(seg))
        npos = sum(1 for r in rets if r > 0)
        total = sim(closes)
        cells = "  ".join(f"{r:>+5.0f}%" for r in rets)
        if npos >= (len(rets) + 1) // 2 and total > 0:
            verdict = "ROBUSTE"
        elif npos <= 1:
            verdict = "FRAGILE (porte par 1 bloc)"
        else:
            verdict = "MITIGE"
        print(f"  {sym:<10} [{cells}]   {npos}/{len(rets)} +   total {total:>+5.0f}%   -> {verdict}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
