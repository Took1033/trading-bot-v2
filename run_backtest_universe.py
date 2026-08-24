"""
run_backtest_universe.py — scanne un large univers de cryptos Coinbase pour le
trend-following daily (SMA50, long-only). But : elargir la flotte de bots en ne
gardant QUE les cryptos a edge trend COMPOSE positif (le critere de deploiement
qu'on a toujours utilise). Base a ~0 frais (Coinbase One) via BACKTEST_FEE_RT.

Regle testee (identique au bot live) : long si cloture > SMA50, sortie si cloture
< SMA50. Net de frais (moitie par cote). Rendement COMPOSE sur la periode.

Usage :
  python run_backtest_universe.py                       # ~5 ans, frais 0.2%
  BACKTEST_FEE_RT=0.002 python run_backtest_universe.py 1825
A LANCER SUR LA MACHINE DE BRICE (truststore -> SSL Coinbase Exchange OK).
"""
from __future__ import annotations

# truststore AVANT tout import reseau (SSL Coinbase via le magasin Windows)
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import asyncio
import os
import sys

from run_backtest_trailing import SMA, fetch_daily

FEE_RT = float(os.getenv("BACKTEST_FEE_RT", "0.002"))   # ~0 = Coinbase One
HALF   = FEE_RT / 2

# univers candidat : les 6 actuels (*) + les cryptos liquides a historique correct.
CANDIDATES = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "LINK-USD",   # actuels
    "ADA-USD", "AVAX-USD", "DOT-USD", "LTC-USD", "BCH-USD", "ATOM-USD",
    "UNI-USD", "AAVE-USD", "NEAR-USD", "FIL-USD", "ETC-USD", "XLM-USD",
    "ALGO-USD", "ICP-USD", "MKR-USD", "GRT-USD", "SAND-USD", "MANA-USD",
    "CRV-USD", "APT-USD", "ARB-USD", "OP-USD", "INJ-USD", "SUI-USD",
]
CURRENT = {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "LINK-USD"}


def simulate(closes: list[float]) -> dict:
    equity = 10_000.0
    in_pos = False
    units = entry = 0.0
    wins = losses = 0
    curve: list[float] = []
    for i in range(SMA, len(closes)):
        price = closes[i]
        sma = sum(closes[i - SMA:i]) / SMA
        if not in_pos:
            if price > sma:
                in_pos = True
                units = equity / price * (1 - HALF)
                entry = price
            curve.append(equity)
        else:
            curve.append(units * price)
            if price < sma:
                equity = units * price * (1 - HALF)
                wins, losses = (wins + 1, losses) if price >= entry else (wins, losses + 1)
                in_pos = False
                units = 0.0
    if in_pos:
        equity = units * closes[-1] * (1 - HALF)
    peak = curve[0] if curve else equity
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak * 100)
    n = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "dd": dd, "n": n,
            "win": (wins / n * 100) if n else 0.0}


async def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1825
    print("=" * 76)
    print(f"  UNIVERS trend-following SMA{SMA} daily - {days // 365} ans - frais {FEE_RT:.2%} (Coinbase One)")
    print("=" * 76)
    print(f"  {'crypto':<10}{'trend':>9}{'hold':>9}{'maxDD':>7}{'trades':>7}{'win':>6}{'histo':>7}  verdict")
    print("  " + "-" * 72)
    rows = []
    for sym in CANDIDATES:
        closes = await fetch_daily(sym, days)
        await asyncio.sleep(0.35)                        # anti rate-limit inter-symbole
        if len(closes) < SMA + 30:
            print(f"  {sym:<10}  (pas assez de donnees / paire indispo)")
            continue
        rows.append((sym, simulate(closes), (closes[-1] / closes[0] - 1) * 100, len(closes) / 365.0))
    rows.sort(key=lambda r: -r[1]["ret"])
    keepers, shorts = [], []
    for sym, m, bh, yrs in rows:
        keep = m["ret"] > 0
        short = yrs < 2.5
        tag = ("GARDER?" if short else "GARDER") if keep else "ecarter"
        star = " *" if sym in CURRENT else ""
        print(f"  {sym:<10}{m['ret']:>+8.0f}%{bh:>+8.0f}%{m['dd']:>6.0f}%"
              f"{m['n']:>7}{m['win']:>5.0f}%{yrs:>6.1f}a  {tag}{star}")
        if keep and not short:
            keepers.append(sym)
        elif keep:
            shorts.append(sym)
    print("  " + "-" * 72)
    print("  * = deja dans la flotte actuelle")
    print()
    print(f"  >> GARDER (edge trend compose positif, histo >=2.5 ans) - {len(keepers)} :")
    print("     " + ", ".join(keepers))
    if shorts:
        print(f"  >> A CONFIRMER (positif mais histo court <2.5 ans) : {', '.join(shorts)}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
