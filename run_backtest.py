"""
run_backtest.py - Backtest la strategie sur les N derniers jours.

Recupere les prix historiques Coinbase et rejoue la strategie avec
les memes parametres que la production (depuis .env).

Usage :
    python run_backtest.py                          # BTC sur 30 jours
    python run_backtest.py ETH-USDC 60              # ETH sur 60 jours
    python run_backtest.py BTC-USDC 90 hourly       # BTC sur 90j, granularite 1h
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import aiohttp


GRANULARITIES = {
    "minute":  60,
    "5min":   300,
    "15min":  900,
    "hourly": 3600,
    "6h":   21600,
    "daily":86400,
}


async def fetch_prices_coinbase(
    symbol:      str   = "BTC-USDC",
    granularity: int   = 3600,
    days:        int   = 30,
) -> list[float]:
    """Recupere les prix de cloture (close) depuis l'API publique Coinbase Exchange."""
    # Coinbase Exchange API : max 300 points par requete
    # On boucle si besoin
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url   = f"https://api.exchange.coinbase.com/products/{symbol.replace('USDC', 'USD')}/candles"

    all_candles: list = []
    cursor_end = end

    async with aiohttp.ClientSession() as s:
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(seconds=granularity * 300))
            params = {
                "granularity": granularity,
                "start":       cursor_start.isoformat(),
                "end":         cursor_end.isoformat(),
            }
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    print(f"  HTTP {r.status} : {await r.text()}")
                    break
                data = await r.json()
                if not data:
                    break
                all_candles.extend(data)
            cursor_end = cursor_start
            await asyncio.sleep(0.3)   # respect du rate limit

    # Format Coinbase candle : [time, low, high, open, close, volume]
    # On trie chronologiquement et on prend les close
    all_candles.sort(key=lambda c: c[0])
    closes = [float(c[4]) for c in all_candles]
    return closes


async def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC-USDC"
    days   = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    gran   = sys.argv[3] if len(sys.argv) > 3 else "hourly"

    granularity = GRANULARITIES.get(gran, 3600)
    n_points    = days * 86400 // granularity

    print("=" * 60)
    print(f"  Backtest Kairos Alpha")
    print("=" * 60)
    print(f"  Symbole       : {symbol}")
    print(f"  Periode       : {days} jours")
    print(f"  Granularite   : {gran} ({granularity}s)")
    print(f"  Points cibles : ~{n_points}")
    print()
    print(f"  Recuperation prix Coinbase...")

    prices = await fetch_prices_coinbase(symbol, granularity, days)
    if len(prices) < 25:
        print(f"  ❌  Pas assez de prix ({len(prices)}/25 minimum)")
        return 1

    print(f"  ✅  {len(prices)} prix recuperes\n")
    print(f"  Prix initial  : {prices[0]:>12,.2f} USDC")
    print(f"  Prix final    : {prices[-1]:>12,.2f} USDC")
    print(f"  Buy & Hold    : {(prices[-1]/prices[0] - 1) * 100:>+11.2f}%")
    print()
    print(f"  Lancement backtest...")
    print()

    from strategies.backtester import Backtester
    from strategies.simple_ma import analyze

    bt     = Backtester(strategy=analyze)
    result = await bt.run(symbol, prices)
    print(result.summary())

    # Comparaison avec buy & hold
    bh = (prices[-1] / prices[0] - 1) * 100
    diff = result.total_return - bh
    sign = "+" if diff >= 0 else ""
    print(f"\n  Strategie vs Buy & Hold : {sign}{diff:.2f} points")
    if diff > 0:
        print(f"  ✅  La strategie BAT le buy & hold de {diff:.2f}%")
    else:
        print(f"  ⚠️   La strategie SOUS-PERFORME de {-diff:.2f}%")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
