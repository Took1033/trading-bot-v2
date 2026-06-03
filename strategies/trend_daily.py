"""
trend_daily.py — strategie trend-following DAILY long-only (edge valide le 2026-06-03).

Logique (la plus simple qui a un edge prouve, cf. research_edge.py) :
  - long quand prix > SMA(period) des clotures JOURNALIERES (tendance haussiere)
  - flat sinon (sortie au passage sous la SMA)
Pas de stop-loss serre : on sort sur retournement de tendance, on tient les DD.

Validation (net frais maker, 5 ans) : ETH +108% vs buy&hold -28% ; BTC +100% vs
+92% (DD plus faibles). SOL exclu (non fiable). -> univers BTC/ETH.

Fonction pure `compute_trend_signal` (testable) + fetch des clotures daily (cache).
"""
from __future__ import annotations

import os
import time

import structlog
from dotenv import load_dotenv

from strategies.simple_ma import Signal

load_dotenv()
log = structlog.get_logger()

TREND_SMA_PERIOD = int(os.getenv("TREND_SMA_PERIOD", "50"))
_DAILY_TTL_S     = int(os.getenv("TREND_DAILY_CACHE_S", "21600"))   # 6h (daily bouge 1x/j)

# Cache des clotures journalieres par symbole : symbol -> (fetched_at, closes)
_cache: dict[str, tuple[float, list[float]]] = {}


def compute_trend_signal(
    symbol: str,
    live_price: float,
    daily_closes: list[float],
    sma_period: int = TREND_SMA_PERIOD,
) -> Signal:
    """
    Signal trend pur (fonction pure, testable) : compare le prix live a la SMA
    des clotures journalieres.
      - buy  si live_price > SMA  (tendance haussiere -> etre/rester long)
      - sell si live_price < SMA  (tendance baissiere -> etre/rester flat)
      - hold si pas assez de donnees
    """
    if len(daily_closes) < sma_period:
        return Signal("hold", 0.0,
                      f"Trend: donnees insuffisantes ({len(daily_closes)}/{sma_period})",
                      symbol, {"sma": None, "sma_period": sma_period})

    sma = sum(daily_closes[-sma_period:]) / sma_period
    dist_pct = (live_price - sma) / sma * 100 if sma > 0 else 0.0
    meta = {"sma": round(sma, 4), "sma_period": sma_period,
            "dist_pct": round(dist_pct, 2), "live_price": round(live_price, 4)}

    if live_price > sma:
        return Signal("buy", 0.90,
                      f"Trend HAUSSIER : prix {live_price:.4f} > SMA{sma_period} {sma:.4f} ({dist_pct:+.1f}%)",
                      symbol, meta)
    return Signal("sell", 0.90,
                  f"Trend BAISSIER : prix {live_price:.4f} < SMA{sma_period} {sma:.4f} ({dist_pct:+.1f}%)",
                  symbol, meta)


async def fetch_daily_closes(symbol: str, n: int | None = None) -> list[float]:
    """Clotures journalieres (API publique Coinbase Exchange), avec cache TTL."""
    n = n or (TREND_SMA_PERIOD + 10)
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < _DAILY_TTL_S:
        return cached[1]

    from strategies.backtester import fetch_prices_coinbase
    exchange_symbol = symbol.replace("USDC", "USD")
    closes = await fetch_prices_coinbase(exchange_symbol, granularity=86400, limit=max(n, 60))
    if closes:
        _cache[symbol] = (now, closes)
        log.info("trend_daily_fetched", symbol=symbol, n=len(closes))
    elif cached:
        return cached[1]   # garde le cache meme expire si le fetch echoue
    return closes


async def analyze(symbol: str, live_price: float) -> Signal:
    """Recupere les clotures daily (cache) et renvoie le signal trend."""
    closes = await fetch_daily_closes(symbol)
    return compute_trend_signal(symbol, live_price, closes)
