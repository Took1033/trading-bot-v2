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

# Bande d'hysteresis autour de la SMA (anti-whipsaw). Sans elle, le signal flippe
# buy<->sell a la moindre traversee de la SMA : 17/19 sorties se sont declenchees a
# <=0.5% sous la SMA, chaque A/R coutant ~2.4% de frais round-trip = la 1re source
# de perte. Avec la bande : on n'ENTRE qu'au-dessus de SMA*(1+entry%) et on ne SORT
# qu'en-dessous de SMA*(1-exit%) ; entre les deux on HOLD (garde la position). A 0
# (defaut) = comportement historique inchange. A CALIBRER en backtest sur 5 ans
# (run_backtest_hysteresis.py) AVANT d'activer en live.
TREND_ENTRY_BUFFER_PCT = float(os.getenv("TREND_ENTRY_BUFFER_PCT", "0.0"))
TREND_EXIT_BUFFER_PCT  = float(os.getenv("TREND_EXIT_BUFFER_PCT", "0.0"))

# Cache des clotures journalieres par symbole : symbol -> (fetched_at, closes)
_cache: dict[str, tuple[float, list[float]]] = {}


def _trend_indicators(closes: list[float], sma_long: float, period: int) -> dict:
    """Indicateurs de tendance derives des clotures journalieres — AFFICHAGE SEUL.

    N'influence JAMAIS la decision buy/sell (appele sous try/except par
    compute_trend_signal). Pur Python, aucune dependance.
    Renvoie : sma_short, sma_slope_pct, trend_age_days(+side/capped),
              trend_r2 (regime), volatility_pct.
    """
    out: dict = {}
    n = len(closes)
    if n < 5:
        return out

    # SMA courte (moitie de la periode longue, plancher 5 jours) + son ecart a la longue
    short_p = max(5, period // 2)
    if n >= short_p:
        sma_short = sum(closes[-short_p:]) / short_p
        out["sma_short"] = round(sma_short, 4)
        out["sma_short_period"] = short_p
        if sma_long > 0:
            out["sma_spread_pct"] = round((sma_short - sma_long) / sma_long * 100, 2)

    # Pente de la SMA longue : variation moyenne en %/jour sur les 5 derniers jours
    look = 5
    if n >= period + look:
        sma_prev = sum(closes[-period - look:-look]) / period
        if sma_prev > 0:
            out["sma_slope_pct"] = round((sma_long - sma_prev) / sma_prev / look * 100, 3)

    # Age de la tendance : jours consecutifs ou la cloture reste du meme cote de SA SMA
    if n >= period + 1:
        side = None
        age = 0
        max_age = n - period + 1
        for i in range(n, period - 1, -1):
            sma_i = sum(closes[i - period:i]) / period
            up = closes[i - 1] >= sma_i
            if side is None:
                side = up
            if up == side:
                age += 1
            else:
                break
        out["trend_age_days"] = age
        out["trend_age_side"] = "up" if side else "down"
        out["trend_age_capped"] = age >= max_age

    # Regime tendance/range : R2 d'une regression lineaire sur les N derniers closes
    win = min(n, max(period // 2, 20))
    if win >= 5:
        ys = closes[-win:]
        xs = list(range(win))
        mx = sum(xs) / win
        my = sum(ys) / win
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(win))
        if sxx > 0 and syy > 0:
            r = sxy / (sxx * syy) ** 0.5
            out["trend_r2"] = round(r * r, 3)
            out["trend_regime"] = "trend" if r * r >= 0.5 else "range"

    # Volatilite : ecart-type des rendements journaliers sur ~20 jours, en %
    vw = min(n - 1, 20)
    if vw >= 2:
        rets = [closes[-i] / closes[-i - 1] - 1.0
                for i in range(1, vw + 1) if closes[-i - 1] > 0]
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
            out["volatility_pct"] = round(var ** 0.5 * 100, 2)

    return out


def compute_trend_signal(
    symbol: str,
    live_price: float,
    daily_closes: list[float],
    sma_period: int = TREND_SMA_PERIOD,
    entry_buffer_pct: float = TREND_ENTRY_BUFFER_PCT,
    exit_buffer_pct: float = TREND_EXIT_BUFFER_PCT,
) -> Signal:
    """
    Signal trend pur (fonction pure, testable) : compare le prix live a la SMA
    des clotures journalieres, avec bande d'hysteresis optionnelle (anti-whipsaw).
      - buy  si live_price > SMA*(1+entry%)  (franchit clairement par le haut)
      - sell si live_price < SMA*(1-exit%)   (franchit clairement par le bas)
      - hold dans la bande neutre (garde la position ouverte, reste flat sinon)
      - hold si pas assez de donnees
    Avec entry%=exit%=0 (defaut) : flip strict a la SMA (comportement historique).
    """
    if len(daily_closes) < sma_period:
        return Signal("hold", 0.0,
                      f"Trend: donnees insuffisantes ({len(daily_closes)}/{sma_period})",
                      symbol, {"sma": None, "sma_period": sma_period})

    sma = sum(daily_closes[-sma_period:]) / sma_period
    dist_pct = (live_price - sma) / sma * 100 if sma > 0 else 0.0
    meta = {"sma": round(sma, 4), "sma_period": sma_period,
            "dist_pct": round(dist_pct, 2), "live_price": round(live_price, 4)}

    # Indicateurs d'AFFICHAGE : isoles, n'influencent jamais la decision ci-dessous.
    try:
        meta.update(_trend_indicators(daily_closes, sma, sma_period))
    except Exception:
        pass

    upper = sma * (1 + entry_buffer_pct / 100)
    lower = sma * (1 - exit_buffer_pct / 100)
    meta["entry_buffer_pct"] = entry_buffer_pct
    meta["exit_buffer_pct"]  = exit_buffer_pct

    if live_price > upper:
        return Signal("buy", 0.90,
                      f"Trend HAUSSIER : prix {live_price:.4f} > SMA{sma_period} {sma:.4f} ({dist_pct:+.1f}%)",
                      symbol, meta)
    if live_price < lower:
        return Signal("sell", 0.90,
                      f"Trend BAISSIER : prix {live_price:.4f} < SMA{sma_period} {sma:.4f} ({dist_pct:+.1f}%)",
                      symbol, meta)
    # Bande neutre (hysteresis) : ni entree ni sortie. Garde la position si ouverte,
    # reste flat sinon -> supprime les allers-retours ruineux autour de la SMA.
    return Signal("hold", 0.50,
                  f"Trend NEUTRE : prix {live_price:.4f} dans la bande "
                  f"[-{exit_buffer_pct:.1f}%,+{entry_buffer_pct:.1f}%] SMA{sma_period} {sma:.4f} ({dist_pct:+.1f}%)",
                  symbol, meta)


async def fetch_daily_closes(symbol: str, n: int | None = None) -> list[float]:
    """Clotures journalieres (API publique Coinbase Exchange), avec cache TTL."""
    n = n or (TREND_SMA_PERIOD + 70)   # +70 : historique pour age/regime (n'affecte ni la SMA ni la decision)
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
