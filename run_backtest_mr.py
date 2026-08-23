"""
run_backtest_mr.py — le "2e moteur" : mean-reversion HORAIRE a 0 frais.

Teste si la mean-reversion (acheter survendu, revendre au retour a la moyenne)
a un edge NET une fois les frais a ~0 (Coinbase One). C'est la strategie qui avait
ete abandonnee car les frais la tuaient -> on re-tranche avec 0 frais.

Regle (proche de strategies/mean_reversion.py) :
  - ENTREE long : RSI(14) <= 32 ET prix <= Bollinger bas (survendu)
  - SORTIE : prix >= Bollinger milieu (retour a la moyenne)  OU  RSI >= 68  OU  stop -8%
Donnees HORAIRES (la MR churne trop pour du daily). Net de BACKTEST_FEE_RT.

A LANCER SUR LA MACHINE DE BRICE (truststore -> SSL Coinbase Exchange OK).
Usage : python run_backtest_mr.py "BTC-USD,ETH-USD,SOL-USD" 180
"""
from __future__ import annotations

# Avast intercepte le HTTPS -> valider via le magasin Windows. DOIT etre injecte
# AVANT l'import d'aiohttp (sinon aiohttp capte le contexte SSL non patche).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import asyncio
import sys
import time

import aiohttp

from run_backtest_trailing import FEE_RT

RSI_P, BB_P, BB_STD = 14, 20, 2.0
RSI_BUY, RSI_SELL, STOP = 32.0, 68.0, 0.08
HALF_FEE = FEE_RT / 2


async def fetch_hourly(symbol: str, days: int) -> list[float]:
    end, start_all = int(time.time()), int(time.time()) - days * 86400
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    out, cur_end = [], end
    async with aiohttp.ClientSession(headers={"User-Agent": "bt-mr"}) as s:
        while cur_end > start_all:
            cur_start = max(start_all, cur_end - 300 * 3600)
            params = {"granularity": 3600, "start": cur_start, "end": cur_end}
            try:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        print(f"  [fetch {symbol}] HTTP {r.status}")
                        break
                    data = await r.json()
            except Exception as exc:
                print(f"  [fetch KO {symbol}] {str(exc)[:80]}")
                break
            if not data:
                break
            out.extend(data)
            cur_end = cur_start
            await asyncio.sleep(0.4)
    out.sort(key=lambda c: c[0])
    return [float(c[4]) for c in out]


def rsi(prices: list[float], i: int, p: int = RSI_P) -> float:
    if i < p:
        return 50.0
    gains = losses = 0.0
    for k in range(i - p + 1, i + 1):
        d = prices[k] - prices[k - 1]
        if d > 0: gains += d
        else: losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / p) / (losses / p)
    return 100.0 - 100.0 / (1.0 + rs)


def boll(prices: list[float], i: int, p: int = BB_P, k: float = BB_STD):
    seg = prices[i - p + 1:i + 1]
    m = sum(seg) / len(seg)
    var = sum((x - m) ** 2 for x in seg) / len(seg)
    sd = var ** 0.5
    return m - k * sd, m, m + k * sd


def simulate(prices: list[float]) -> dict:
    equity, in_pos, entry, qty = 10_000.0, False, 0.0, 0.0
    wins = losses = 0
    start = max(RSI_P, BB_P) + 1
    for i in range(start, len(prices)):
        px = prices[i]
        lo, mid, up = boll(prices, i)
        r = rsi(prices, i)
        if not in_pos:
            if r <= RSI_BUY and px <= lo:
                in_pos, entry, qty = True, px, equity / px * (1 - HALF_FEE)
        else:
            if px >= mid or r >= RSI_SELL or px <= entry * (1 - STOP):
                equity = qty * px * (1 - HALF_FEE)
                wins, losses = (wins + 1, losses) if px >= entry else (wins, losses + 1)
                in_pos, qty = False, 0.0
    if in_pos:
        equity = qty * prices[-1] * (1 - HALF_FEE)
    n = wins + losses
    return {"ret": (equity / 10_000 - 1) * 100, "n": n, "win": (wins / n * 100) if n else 0.0}


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD,ETH-USD,SOL-USD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    print("=" * 60)
    print(f"  MEAN-REVERSION horaire — {days}j — frais {FEE_RT:.1%} (Coinbase One)")
    print("=" * 60)
    agg = 0.0
    cnt = 0
    for sym in arg.split(","):
        sym = sym.strip().upper()
        px = await fetch_hourly(sym, days)
        if len(px) < 100:
            print(f"\n{sym}: pas assez de donnees ({len(px)})")
            continue
        m = simulate(px)
        cnt += 1
        agg += m["ret"]
        bh = (px[-1] / px[0] - 1) * 100
        print(f"\n{sym} — {len(px)} bougies horaires (buy&hold {bh:+.0f}%)")
        print(f"  MR net : {m['ret']:>+7.1f}%   ({m['n']} trades, win {m['win']:.0f}%)")
    if cnt:
        print("\n" + "=" * 60)
        print(f"  MOYENNE MR net (sur {cnt}) : {agg/cnt:+.1f}%")
        print("  Edge POSITIF net -> 2e moteur viable (a 0 frais). Negatif -> abandonner.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
