"""
run_backtest_trailing.py — A/B trend pur vs trend + trailing-stop (DAILY).

Rejoue la strategie trend-following (long si prix > SMA50 des clotures daily,
sortie au passage sous la SMA) AVEC et SANS trailing-stop, pour voir si
verrouiller le gain AVANT le retournement de SMA ameliore le P&L net.

Sortie trailing : on sort si le prix recule de TRAIL% depuis le plus haut atteint
depuis l'entree (echantillonne sur les clotures daily). C'est une APPROXIMATION
du trailing horaire du bot reel (TREND_TRAILING_STOP_PCT) — le vrai peut sortir
un peu plus tot / plus nerveusement sur une meche intra-jour.

Usage :
    python run_backtest_trailing.py                 # BTC/ETH, ~4 ans
    python run_backtest_trailing.py BTC-USD 1460
    python run_backtest_trailing.py "BTC-USD,ETH-USD,SOL-USD" 1095
"""
from __future__ import annotations

# Avast intercepte le HTTPS -> valider via le magasin Windows (cf. main.py).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import asyncio
import os
import sys
import time

import aiohttp

SMA    = 50
# Frais aller-retour estimes. Defaut 1.2% (taker 0.6%x2), mais le live paie ~2.4%
# (1.2%/cote) : surcharger via BACKTEST_FEE_RT=0.024 pour un chiffrage realiste.
FEE_RT = float(os.getenv("BACKTEST_FEE_RT", "0.012"))
TRAILS = [0.0, 0.10, 0.15, 0.20, 0.25]
DEFAULT_SYMBOLS = ["BTC-USD", "ETH-USD"]   # univers ou l'edge trend est valide


async def fetch_daily(symbol: str, days: int) -> list[float]:
    """Clotures journalieres via l'API publique Coinbase Exchange (paginee)."""
    end       = int(time.time())
    start_all = end - days * 86400
    url       = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    out: list = []
    cur_end   = end
    async with aiohttp.ClientSession(headers={"User-Agent": "bt-trailing"}) as s:
        while cur_end > start_all:
            cur_start = max(start_all, cur_end - 300 * 86400)
            params    = {"granularity": 86400, "start": cur_start, "end": cur_end}
            try:
                async with s.get(url, params=params,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        break
                    data = await r.json()
            except Exception as exc:
                print(f"  [fetch KO {symbol}] {exc}")
                break
            if not data:
                break
            out.extend(data)
            cur_end = cur_start
            await asyncio.sleep(0.3)
    out.sort(key=lambda c: c[0])                 # + ancien -> + recent
    return [float(c[4]) for c in out]            # close = index 4


def simulate(closes: list[float], trail: float,
             entry_buf: float = 0.0, exit_buf: float = 0.0) -> dict:
    """Rejoue trend SMA + trailing optionnel, avec bande d'hysteresis optionnelle.

    Compounding all-in, net de frais. entry_buf/exit_buf en fraction (0.01 = 1%) :
    entre au-dessus de SMA*(1+entry_buf), sort sous SMA*(1-exit_buf). A 0 = flip
    strict a la SMA (comportement historique).
    """
    equity = 10_000.0
    in_pos = False
    entry  = peak = qty = 0.0
    wins   = losses = 0
    curve: list[float] = []

    for i in range(SMA, len(closes)):
        price = closes[i]
        sma   = sum(closes[i - SMA:i]) / SMA
        curve.append(qty * price if in_pos else equity)   # mark-to-market

        if not in_pos:
            if price > sma * (1 + entry_buf):
                in_pos, entry, peak = True, price, price
                qty = equity / price
        else:
            peak = max(peak, price)
            hit_trail = trail > 0 and (peak - price) / peak >= trail
            hit_sma   = price < sma * (1 - exit_buf)
            if hit_trail or hit_sma:
                equity = qty * price * (1 - FEE_RT)
                wins, losses = (wins + 1, losses) if price >= entry else (wins, losses + 1)
                in_pos, qty = False, 0.0

    if in_pos:                                             # liquidation finale
        equity = qty * closes[-1] * (1 - FEE_RT)
        wins, losses = (wins + 1, losses) if closes[-1] >= entry else (wins, losses + 1)

    n = wins + losses
    peak_c, max_dd = (curve[0] if curve else equity), 0.0
    for v in curve:
        peak_c = max(peak_c, v)
        if peak_c > 0:
            max_dd = max(max_dd, (peak_c - v) / peak_c * 100)
    return {"ret": (equity / 10_000 - 1) * 100, "dd": max_dd,
            "n": n, "win": (wins / n * 100) if n else 0.0}


async def run_symbol(symbol: str, days: int):
    closes = await fetch_daily(symbol, days)
    if len(closes) < SMA + 10:
        print(f"  {symbol}: pas assez de donnees ({len(closes)})")
        return None
    bh = (closes[-1] / closes[0] - 1) * 100
    print(f"\n{symbol} — {len(closes)} jours, buy&hold {bh:+.0f}%")
    print(f"  {'trailing':>11} {'net %':>9} {'maxDD %':>9} {'trades':>7} {'win %':>6}")
    rows = {}
    for tr in TRAILS:
        m = simulate(closes, tr)
        rows[tr] = m
        label = "OFF (pur)" if tr == 0 else f"{tr*100:.0f}%"
        print(f"  {label:>11} {m['ret']:>+8.1f} {m['dd']:>8.1f} {m['n']:>7} {m['win']:>5.0f}")
    return rows


async def main() -> None:
    arg1    = sys.argv[1] if len(sys.argv) > 1 else None
    symbols = arg1.split(",") if arg1 else DEFAULT_SYMBOLS
    days    = int(sys.argv[2]) if len(sys.argv) > 2 else 1460

    print("=" * 64)
    print(f"  A/B TRAILING-STOP — trend SMA{SMA} daily — ~{days}j — frais {FEE_RT:.1%}")
    print("=" * 64)

    agg = {tr: 0.0 for tr in TRAILS}
    cnt = 0
    for sym in symbols:
        rows = await run_symbol(sym.strip().upper(), days)
        if rows:
            cnt += 1
            for tr in TRAILS:
                agg[tr] += rows[tr]["ret"]

    if cnt:
        base = agg[0.0] / cnt
        print("\n" + "=" * 64)
        print(f"  MOYENNE du rendement net par config (sur {cnt} symbole(s)) :")
        for tr in TRAILS:
            avg   = agg[tr] / cnt
            label = "trend pur" if tr == 0 else f"trailing {tr*100:.0f}%"
            print(f"    {label:>13} : {avg:>+7.1f}%   ({avg - base:+.1f} pts vs pur)")
        best = max(TRAILS, key=lambda t: agg[t])
        print(f"\n  >>> Meilleure config moyenne : "
              f"{'trend pur (ne rien ajouter)' if best == 0 else f'trailing {best*100:.0f}%'}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
