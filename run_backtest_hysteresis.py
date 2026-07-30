"""
run_backtest_hysteresis.py — calibrage de la BANDE D'HYSTERESIS anti-whipsaw.

Probleme mesure en live : le signal trend flippe buy<->sell a la moindre traversee
de la SMA. 17/19 sorties se sont declenchees a <=0.5% sous la SMA, et chaque A/R
coute ~2.4% de frais round-trip -> les frais sont la 1re source de perte.

Ce script balaie une bande de SORTIE (on ne sort qu'en-dessous de SMA*(1-exit%),
on garde la position dans la bande) et affiche le rendement NET par largeur de
bande, pour choisir la valeur a mettre dans TREND_EXIT_BUFFER_PCT (.env) AVANT de
l'activer en live. entry_buf reste a 0 (la bande de sortie est le levier prouve).

Reutilise le fetch + le simulateur de run_backtest_trailing.py (aucune duplication).
A LANCER SUR LA MACHINE DE BRICE (l'env Claude casse la verif SSL Coinbase).

Usage :
    python run_backtest_hysteresis.py                      # BTC/ETH, ~4 ans
    python run_backtest_hysteresis.py "BTC-USD,ETH-USD,SOL-USD" 1460
"""
from __future__ import annotations

import asyncio
import sys

from run_backtest_trailing import FEE_RT, SMA, fetch_daily, simulate

# Largeurs de bande de sortie testees (fraction : 0.01 = 1% sous la SMA).
EXIT_BUFFERS = [0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03]
DEFAULT_SYMBOLS = ["BTC-USD", "ETH-USD"]


async def run_symbol(symbol: str, days: int) -> dict | None:
    closes = await fetch_daily(symbol, days)
    if len(closes) < SMA + 10:
        print(f"  {symbol}: pas assez de donnees ({len(closes)})")
        return None
    bh = (closes[-1] / closes[0] - 1) * 100
    print(f"\n{symbol} — {len(closes)} jours, buy&hold {bh:+.0f}%")
    print(f"  {'exit buf':>10} {'net %':>9} {'maxDD %':>9} {'trades':>7} {'win %':>6}")
    out = {}
    for eb in EXIT_BUFFERS:
        m = simulate(closes, trail=0.0, entry_buf=0.0, exit_buf=eb)
        out[eb] = m
        label = "OFF (0%)" if eb == 0 else f"{eb * 100:.1f}%"
        print(f"  {label:>10} {m['ret']:>+8.1f} {m['dd']:>8.1f} {m['n']:>7} {m['win']:>5.0f}")
    return out


async def main() -> None:
    arg1    = sys.argv[1] if len(sys.argv) > 1 else None
    symbols = arg1.split(",") if arg1 else DEFAULT_SYMBOLS
    days    = int(sys.argv[2]) if len(sys.argv) > 2 else 1460

    print("=" * 66)
    print(f"  CALIBRAGE HYSTERESIS — trend SMA{SMA} daily — ~{days}j — frais {FEE_RT:.1%}")
    print("  (bande de sortie sous la SMA ; entree stricte ; trailing OFF)")
    print("=" * 66)

    agg = {eb: 0.0 for eb in EXIT_BUFFERS}
    cnt = 0
    for sym in symbols:
        rows = await run_symbol(sym.strip().upper(), days)
        if rows:
            cnt += 1
            for eb in EXIT_BUFFERS:
                agg[eb] += rows[eb]["ret"]

    if cnt:
        base = agg[0.0] / cnt
        print("\n" + "=" * 66)
        print(f"  MOYENNE du rendement net par bande (sur {cnt} symbole(s)) :")
        for eb in EXIT_BUFFERS:
            avg   = agg[eb] / cnt
            label = "sans bande" if eb == 0 else f"exit {eb * 100:.1f}%"
            print(f"    {label:>12} : {avg:>+7.1f}%   ({avg - base:+.1f} pts vs sans)")
        best = max(EXIT_BUFFERS, key=lambda e: agg[e])
        rec  = "ne rien changer" if best == 0 else f"TREND_EXIT_BUFFER_PCT={best * 100:.1f}"
        print(f"\n  >>> Meilleure bande moyenne : "
              f"{'sans bande' if best == 0 else f'{best * 100:.1f}% sous SMA'}  ->  {rec}")
        print("      (verifier que le gain net tient sur CHAQUE symbole, pas que la moyenne)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
