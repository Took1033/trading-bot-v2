"""
run_backtest_regime.py — valide le FILTRE DE REGIME macro.

Compare, pour chaque alt, le trend-following daily (long si prix > SMA50) AVEC et
SANS le gate "n'entrer que si BTC est lui-meme > sa SMA50". Le regime ne force jamais
la sortie (on sort toujours au retournement de la SMA de l'alt) — il ne fait que
BLOQUER les entrees pendant un bear global. Net de frais reels.

A lancer sur la machine de Brice (l'env Claude casse le SSL du SDK, mais l'API
publique exchange.coinbase.com passe via truststore).

Usage :
    python run_backtest_regime.py                         # SOL/ETH/NEAR, 5 ans
    python run_backtest_regime.py "SOL-USD,NEAR-USD" 1825
"""
from __future__ import annotations

import asyncio
import sys

from run_backtest_trailing import FEE_RT, SMA, fetch_daily


def simulate_regime(alt: list[float], btc: list[float], use_regime: bool) -> dict:
    """Trend SMA sur l'alt ; entree gatee par (BTC > SMA_BTC) si use_regime.
    Series alignees par la fin (memes jours recents). Compounding all-in, net frais."""
    n = min(len(alt), len(btc))
    alt, btc = alt[-n:], btc[-n:]
    equity = 10_000.0
    in_pos = False
    entry  = qty = 0.0
    wins   = losses = 0
    for i in range(SMA, n):
        p       = alt[i]
        alt_sma = sum(alt[i - SMA:i]) / SMA
        btc_sma = sum(btc[i - SMA:i]) / SMA
        btc_bull = btc[i] > btc_sma
        if not in_pos:
            if p > alt_sma and (btc_bull or not use_regime):
                in_pos, entry, qty = True, p, equity / p
        else:
            if p < alt_sma:                      # sortie = retournement SMA de l'alt
                equity = qty * p * (1 - FEE_RT)
                wins, losses = (wins + 1, losses) if p >= entry else (wins, losses + 1)
                in_pos, qty = False, 0.0
    if in_pos:
        equity = qty * alt[-1] * (1 - FEE_RT)
        wins, losses = (wins + 1, losses) if alt[-1] >= entry else (wins, losses + 1)
    n_tr = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "n": n_tr,
            "win": (wins / n_tr * 100) if n_tr else 0.0}


async def main() -> None:
    arg  = sys.argv[1] if len(sys.argv) > 1 else "SOL-USD,ETH-USD,NEAR-USD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1825

    print("=" * 64)
    print(f"  FILTRE DE REGIME (gate BTC>SMA) — SMA{SMA} daily — frais {FEE_RT:.1%}")
    print("=" * 64)

    btc = await fetch_daily("BTC-USD", days)
    if len(btc) < SMA + 10:
        print("  BTC : pas assez de donnees, abandon.")
        return

    agg_off = agg_on = 0.0
    cnt = 0
    for sym in arg.split(","):
        sym = sym.strip().upper()
        if sym == "BTC-USD":
            continue
        alt = await fetch_daily(sym, days)
        if len(alt) < SMA + 10:
            print(f"\n{sym}: pas assez de donnees ({len(alt)})")
            continue
        off = simulate_regime(alt, btc, use_regime=False)
        on  = simulate_regime(alt, btc, use_regime=True)
        cnt += 1
        agg_off += off["ret"]
        agg_on  += on["ret"]
        print(f"\n{sym} — {min(len(alt), len(btc))} jours communs")
        print(f"  sans filtre : {off['ret']:>+8.1f}%   ({off['n']} trades, win {off['win']:.0f}%)")
        print(f"  AVEC filtre : {on['ret']:>+8.1f}%   ({on['n']} trades, win {on['win']:.0f}%)"
              f"   [{on['ret'] - off['ret']:+.1f} pts]")

    if cnt:
        base_off = agg_off / cnt
        base_on  = agg_on / cnt
        print("\n" + "=" * 64)
        print(f"  MOYENNE : sans filtre {base_off:+.1f}%   avec filtre {base_on:+.1f}%"
              f"   ({base_on - base_off:+.1f} pts)")
        verdict = "BENEFIQUE -> activer REGIME_FILTER_ENABLED=true" if base_on > base_off \
                  else "INUTILE/NUISIBLE -> laisser OFF"
        print(f"  >>> Filtre de regime : {verdict}")
        print("      (verifier que le gain tient par actif, pas que la moyenne)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
