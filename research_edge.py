"""
research_edge.py — banc d'essai R&D : une these a-t-elle un EDGE BRUT ?

Le constat (backtest minute 2026-06-03) : l'ensemble actuel a un edge brut ~0,
les frais font toute la perte. Avant de construire quoi que ce soit, on cherche
si UNE these simple et propre a un rendement BRUT positif (= un vrai signal).

Teste plusieurs strategies long-only ET long/short sur les memes donnees et
compare : rendement BRUT (frais=0), net maker (~0.4%/cote), nb trades, win rate,
max drawdown, vs Buy & Hold. La colonne qui compte : GROSS. Si gross <= 0 et
<= buy&hold, la these n'a pas d'edge — inutile d'aller plus loin.

Usage :
    python research_edge.py                       # BTC/ETH/SOL, 720j daily
    python research_edge.py BTC-USDC 365 daily
    python research_edge.py ETH-USDC 60 hourly

Pas d'authentification, pas d'ordre : lecture seule de candles publiques.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
import numpy as np

GRAN = {"hourly": 3600, "6h": 21600, "daily": 86400, "minute": 60, "5min": 300}
MAKER_FEE_SIDE = 0.004     # ~0.4%/cote (estimation maker palier actuel)
DEFAULT_SYMBOLS = ["BTC-USDC", "ETH-USDC", "SOL-USDC"]


# ── Donnees ──────────────────────────────────────────────────────────────────
async def fetch_closes(symbol: str, granularity: int, days: int) -> list[float]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = f"https://api.exchange.coinbase.com/products/{symbol.replace('USDC', 'USD')}/candles"
    candles: list = []
    cur = end
    async with aiohttp.ClientSession() as s:
        while cur > start:
            cs = max(start, cur - timedelta(seconds=granularity * 300))
            params = {"granularity": granularity, "start": cs.isoformat(), "end": cur.isoformat()}
            async with s.get(url, params=params,
                             headers={"User-Agent": "TradingBot/2.0 (research)"}) as r:
                if r.status != 200:
                    break
                data = await r.json()
                if not data:
                    break
                candles.extend(data)
            cur = cs
            await asyncio.sleep(0.3)
    candles.sort(key=lambda c: c[0])
    return [float(c[4]) for c in candles]


# ── Indicateurs ──────────────────────────────────────────────────────────────
def _sma(p: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(p, np.nan, dtype=float)
    if len(p) >= n:
        c = np.cumsum(np.insert(p, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


# ── Strategies : renvoient un tableau de positions cibles dans {-1, 0, +1} ────
def pos_buy_hold(p):
    return np.ones(len(p))

def pos_trend_long(p, slow=50):
    s = _sma(p, slow)
    pos = (p > s).astype(float)
    pos[np.isnan(s)] = 0.0
    return pos

def pos_trend_ls(p, slow=50):
    s = _sma(p, slow)
    pos = np.where(p > s, 1.0, -1.0)
    pos[np.isnan(s)] = 0.0
    return pos

def pos_donchian_ls(p, n=20):
    pos = np.zeros(len(p)); state = 0.0
    for t in range(n, len(p)):
        win = p[t - n:t]
        if p[t] >= win.max():   state = 1.0
        elif p[t] <= win.min(): state = -1.0
        pos[t] = state
    return pos

def pos_tsmom_ls(p, lookback=30):
    pos = np.zeros(len(p))
    for t in range(lookback, len(p)):
        pos[t] = 1.0 if p[t] > p[t - lookback] else -1.0
    return pos

STRATS = {
    "Buy&Hold":        pos_buy_hold,
    "Trend-MA long":   pos_trend_long,
    "Trend-MA L/S":    pos_trend_ls,
    "Donchian L/S":    pos_donchian_ls,
    "TS-momentum L/S": pos_tsmom_ls,
}


# ── Simulateur equity ─────────────────────────────────────────────────────────
def simulate(prices: list[float], pos: np.ndarray, fee_side: float) -> dict:
    p = np.asarray(prices, dtype=float)
    eq = 1.0; peak = 1.0; maxdd = 0.0
    trades = 0; wins = 0; entry_eq = None; prev = 0.0
    for t in range(len(p) - 1):
        # cout de changement de position a t (entrer/sortir/flip)
        change = abs(pos[t] - prev)
        if change > 0:
            if prev != 0 and entry_eq is not None:        # cloture d'un trade
                trades += 1
                if eq > entry_eq:
                    wins += 1
            eq *= (1 - fee_side * change)
            entry_eq = eq if pos[t] != 0 else None
            prev = pos[t]
        # rendement de t -> t+1 selon la position tenue
        ret = p[t + 1] / p[t] - 1.0
        eq *= (1 + pos[t] * ret)
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    return {
        "ret_pct": (eq - 1) * 100,
        "trades":  trades,
        "win":     (wins / trades * 100) if trades else 0.0,
        "maxdd":   maxdd * 100,
    }


async def run_symbol(symbol: str, days: int, granularity: int) -> None:
    try:
        prices = await fetch_closes(symbol, granularity, days)
    except Exception as exc:
        print(f"  {symbol}: fetch KO ({exc})"); return
    if len(prices) < 80:
        print(f"  {symbol}: pas assez de prix ({len(prices)})"); return

    print(f"\n  {symbol}  ({len(prices)} pts)")
    print(f"  {'strategie':<17}{'GROSS%':>9}{'net maker%':>12}{'trades':>8}{'win%':>7}{'maxDD%':>8}")
    print("  " + "-" * 61)
    for name, fn in STRATS.items():
        pos = fn(prices)
        g = simulate(prices, pos, 0.0)            # BRUT (sans frais)
        n = simulate(prices, pos, MAKER_FEE_SIDE)  # net maker
        flag = "  <-- edge brut +" if (name != "Buy&Hold" and g["ret_pct"] > 0) else ""
        print(f"  {name:<17}{g['ret_pct']:>8.1f}%{n['ret_pct']:>11.1f}%"
              f"{g['trades']:>8}{g['win']:>6.0f}%{g['maxdd']:>7.0f}%{flag}")


async def main() -> int:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 720
    gran = GRAN.get(sys.argv[3] if len(sys.argv) > 3 else "daily", 86400)

    print("=" * 70)
    print(f"  RECHERCHE D'EDGE — {days}j {sys.argv[3] if len(sys.argv) > 3 else 'daily'}"
          f" — frais maker {MAKER_FEE_SIDE:.2%}/cote")
    print("  Colonne GROSS = edge avant frais. Si <= Buy&Hold partout : pas d'edge.")
    print("=" * 70)
    for sym in symbols:
        await run_symbol(sym, days, gran)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
