"""
run_backtest_sma.py — cherche la PERIODE DE SMA optimale (trend-following daily pur).

Pour chaque symbole, teste plusieurs longueurs de SMA (20/30/50/100) : SMA courte =
reagit vite (capte les vagues tot, mais plus de faux departs), SMA longue = plus lente
mais plus propre. A 0 frais (Coinbase One), les SMA courtes redeviennent jouables.

Reutilise fetch_daily (1 fetch/symbole). Net de frais (BACKTEST_FEE_RT).

Usage :
    python run_backtest_sma.py                                  # 6 bots, 5 ans
    python run_backtest_sma.py "BTC-USD,ETH-USD" 1825
"""
from __future__ import annotations

import asyncio
import sys

from run_backtest_trailing import FEE_RT, fetch_daily

SMAS = [20, 30, 50, 100]


def simulate(closes: list[float], sma_p: int) -> dict:
    """Trend pur (long si prix > SMA_p, sort sinon). Compounding all-in, net frais."""
    equity = 10_000.0
    in_pos = False
    entry  = qty = 0.0
    wins   = losses = 0
    for i in range(sma_p, len(closes)):
        p   = closes[i]
        sma = sum(closes[i - sma_p:i]) / sma_p
        if not in_pos:
            if p > sma:
                in_pos, entry, qty = True, p, equity / p
        else:
            if p < sma:
                equity = qty * p * (1 - FEE_RT)
                wins, losses = (wins + 1, losses) if p >= entry else (wins, losses + 1)
                in_pos, qty = False, 0.0
    if in_pos:
        equity = qty * closes[-1] * (1 - FEE_RT)
        wins, losses = (wins + 1, losses) if closes[-1] >= entry else (wins, losses + 1)
    n = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "n": n,
            "win": (wins / n * 100) if n else 0.0}


async def main() -> None:
    arg  = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD,ETH-USD,SOL-USD,NEAR-USD,XRP-USD,DOGE-USD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1825

    print("=" * 60)
    print(f"  PERIODE DE SMA optimale — trend pur daily — frais {FEE_RT:.1%}")
    print("=" * 60)

    agg = {s: 0.0 for s in SMAS}
    cnt = 0
    for sym in arg.split(","):
        sym = sym.strip().upper()
        closes = await fetch_daily(sym, days)
        if len(closes) < max(SMAS) + 10:
            print(f"\n{sym}: pas assez de donnees ({len(closes)})")
            continue
        cnt += 1
        print(f"\n{sym} — {len(closes)} jours")
        best = None
        for s in SMAS:
            m = simulate(closes, s)
            agg[s] += m["ret"]
            print(f"  SMA{s:<3} : {m['ret']:>+8.1f}%   ({m['n']} trades, win {m['win']:.0f}%)")
            if best is None or m["ret"] > best[1]:
                best = (s, m["ret"])
        print(f"  -> meilleure : SMA{best[0]} ({best[1]:+.1f}%)")

    if cnt:
        print("\n" + "=" * 60)
        print(f"  MOYENNE par periode (sur {cnt} symboles) :")
        for s in SMAS:
            print(f"    SMA{s:<3} : {agg[s] / cnt:>+8.1f}%")
        best_s = max(SMAS, key=lambda s: agg[s])
        print(f"\n  >>> Meilleure periode moyenne : SMA{best_s}"
              f"  ->  TREND_SMA_PERIOD={best_s}")
        print("      (verifier par actif : une SMA peut gagner en moyenne mais perdre sur un bot)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
