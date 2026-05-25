"""
Strategie Moving Average Crossover (EMA 9 / EMA 21) + filtre RSI 14.

Signaux :
  BUY  - golden cross EMA9 > EMA21, RSI non suracheté (< RSI_OVERBOUGHT)
  SELL - death cross  EMA9 < EMA21, RSI non survendu  (> RSI_OVERSOLD)
  HOLD - pas de croisement, ou RSI extreme filtre le signal

Filtre RSI :
  - BUY  bloque si RSI >= 70 (zone surachtée, risque de retournement)
  - SELL bloque si RSI <= 30 (zone survendue, risque de rebond)
  - La confiance est ajustee selon la position RSI par rapport a 50

Interface standardisee : async def analyze(symbol, prices) -> Signal
Requires au moins 22 prix (EMA21 + 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import structlog

log = structlog.get_logger()

# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    action:     Literal["buy", "sell", "hold"]
    confidence: float          # 0.0 - 1.0
    reasoning:  str
    symbol:     str
    metadata:   dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Parametres
# ─────────────────────────────────────────────────────────────────────────────

EMA_FAST        = 9
EMA_SLOW        = 21
EMA_TREND       = 50     # Filtre tendance long-terme
RSI_PERIOD      = 14
RSI_OVERBOUGHT  = 70.0   # BUY bloque au-dessus
RSI_OVERSOLD    = 30.0   # SELL bloque en-dessous
VOL_MIN_PCT     = 0.10   # Volatilite min : std/price >= 0.10% (sinon marche plat)
MIN_POINTS      = EMA_SLOW + 1   # 22 points minimum pour signal de base
MIN_POINTS_FULL = EMA_TREND + 1  # 51 points pour avoir le trend filter actif


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    """EMA via methode exponentielle classique."""
    k = 2.0 / (period + 1)
    result = np.empty(len(prices))
    result[0] = prices[0]
    for i in range(1, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1.0 - k)
    return result


def _rsi(prices: np.ndarray, period: int = RSI_PERIOD) -> float:
    """
    RSI (Relative Strength Index) sur `period` periodes.
    Retourne 50.0 (neutre) si pas assez de donnees.
    """
    if len(prices) < period + 1:
        return 50.0
    deltas   = np.diff(prices[-(period + 1):])
    gains    = np.where(deltas > 0, deltas, 0.0)
    losses   = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _crossover_confidence(diff_curr: float, ref_price: float, rsi: float) -> float:
    """
    Confiance basee sur l'amplitude du croisement, ajustee par le RSI.
    - RSI confirme la direction (loin de 50) -> boost de +5%
    - RSI neutre (proche de 50)              -> pas de changement
    """
    raw = abs(float(diff_curr)) / float(ref_price) * 1000
    conf = float(round(min(max(raw, 0.55), 0.90), 4))

    # Boost si RSI confirme (BUY avec RSI < 50, SELL avec RSI > 50)
    rsi_distance = abs(rsi - 50.0) / 50.0   # 0.0 (neutre) a 1.0 (extreme)
    boost = 0.05 * rsi_distance
    conf = float(round(min(conf + boost, 0.95), 4))

    return conf


# ─────────────────────────────────────────────────────────────────────────────
# Strategie principale
# ─────────────────────────────────────────────────────────────────────────────

async def analyze(symbol: str, prices: list[float]) -> Signal:
    """
    Analyse les prix et retourne un Signal de trading.

    Args:
        symbol: ex. "BTC-USDC"
        prices: liste chronologique de prix (le plus recent en dernier)

    Returns:
        Signal avec action, confidence, reasoning, metadata
    """
    if len(prices) < MIN_POINTS:
        return Signal(
            action="hold",
            confidence=0.0,
            reasoning=f"Donnees insuffisantes : {len(prices)}/{MIN_POINTS} points requis",
            symbol=symbol,
        )

    arr      = np.array(prices, dtype=float)
    ema_fast = _ema(arr, EMA_FAST)
    ema_slow = _ema(arr, EMA_SLOW)
    rsi      = _rsi(arr)

    curr_diff = float(ema_fast[-1] - ema_slow[-1])
    prev_diff = float(ema_fast[-2] - ema_slow[-2])
    price     = float(arr[-1])

    # EMA50 disponible uniquement avec >= 51 points
    has_trend  = len(prices) >= MIN_POINTS_FULL
    ema_trend  = float(_ema(arr, EMA_TREND)[-1]) if has_trend else None
    trend_up   = (ema_trend is not None) and (price > ema_trend)
    trend_down = (ema_trend is not None) and (price < ema_trend)

    # Volatilite : ecart-type relatif des 20 derniers prix (%)
    recent     = arr[-20:] if len(arr) >= 20 else arr
    vol_pct    = float(np.std(recent) / price * 100) if price > 0 else 0.0
    too_flat   = vol_pct < VOL_MIN_PCT

    meta = {
        "ema_fast":  round(float(ema_fast[-1]), 2),
        "ema_slow":  round(float(ema_slow[-1]), 2),
        "ema_trend": round(ema_trend, 2) if ema_trend else None,
        "diff":      round(curr_diff, 2),
        "rsi":       round(rsi, 1),
        "vol_pct":   round(vol_pct, 3),
        "price":     round(price, 2),
        "n_points":  len(prices),
    }

    # ── Filtre volatilite globale (s'applique a tout signal) ─────────────────
    if too_flat:
        return Signal(
            action="hold",
            confidence=0.3,
            reasoning=(
                f"Marche trop plat - vol={vol_pct:.3f}% < min {VOL_MIN_PCT}% "
                f"(eviter whipsaws)"
            ),
            symbol=symbol,
            metadata=meta,
        )

    # ── Golden cross : EMA9 passe au-dessus d'EMA21 ──────────────────────────
    if prev_diff <= 0 and curr_diff > 0:
        # Filtre tendance : ne BUY que si le prix est au-dessus de l'EMA50
        if has_trend and not trend_up:
            log.info("signal_filtered", symbol=symbol, action="buy->hold",
                     reason="downtrend", price=price, ema_trend=ema_trend)
            return Signal(
                action="hold",
                confidence=0.5,
                reasoning=(
                    f"Golden cross filtre - prix {price:.2f} < EMA{EMA_TREND}={ema_trend:.2f} "
                    f"(downtrend, eviter contre-tendance)"
                ),
                symbol=symbol,
                metadata=meta,
            )

        if rsi >= RSI_OVERBOUGHT:
            log.info("signal_filtered", symbol=symbol, action="buy->hold",
                     reason="RSI overbought", rsi=round(rsi, 1))
            return Signal(
                action="hold",
                confidence=0.5,
                reasoning=(
                    f"Golden cross filtre - RSI suracheté ({rsi:.1f} >= {RSI_OVERBOUGHT:.0f}) "
                    f"EMA{EMA_FAST}-EMA{EMA_SLOW}={curr_diff:+.2f}"
                ),
                symbol=symbol,
                metadata=meta,
            )

        confidence = _crossover_confidence(curr_diff, ema_slow[-1], rsi)
        # Boost +5% si le trend filter confirme
        if has_trend and trend_up:
            confidence = float(round(min(confidence + 0.05, 0.95), 4))
        log.info("signal_generated", symbol=symbol, action="buy",
                 confidence=confidence, **meta)
        return Signal(
            action="buy",
            confidence=confidence,
            reasoning=(
                f"Golden cross EMA{EMA_FAST}/EMA{EMA_SLOW} "
                f"(diff={curr_diff:+.2f}) | RSI={rsi:.1f}"
            ),
            symbol=symbol,
            metadata=meta,
        )

    # ── Death cross : EMA9 passe en-dessous d'EMA21 ──────────────────────────
    if prev_diff >= 0 and curr_diff < 0:
        # Filtre tendance : ne SELL que si le prix est en-dessous de l'EMA50
        if has_trend and not trend_down:
            log.info("signal_filtered", symbol=symbol, action="sell->hold",
                     reason="uptrend", price=price, ema_trend=ema_trend)
            return Signal(
                action="hold",
                confidence=0.5,
                reasoning=(
                    f"Death cross filtre - prix {price:.2f} > EMA{EMA_TREND}={ema_trend:.2f} "
                    f"(uptrend, garder la position)"
                ),
                symbol=symbol,
                metadata=meta,
            )

        if rsi <= RSI_OVERSOLD:
            log.info("signal_filtered", symbol=symbol, action="sell->hold",
                     reason="RSI oversold", rsi=round(rsi, 1))
            return Signal(
                action="hold",
                confidence=0.5,
                reasoning=(
                    f"Death cross filtre - RSI survendu ({rsi:.1f} <= {RSI_OVERSOLD:.0f}) "
                    f"EMA{EMA_FAST}-EMA{EMA_SLOW}={curr_diff:+.2f}"
                ),
                symbol=symbol,
                metadata=meta,
            )

        confidence = _crossover_confidence(curr_diff, ema_slow[-1], rsi)
        # Boost +5% si le trend filter confirme
        if has_trend and trend_down:
            confidence = float(round(min(confidence + 0.05, 0.95), 4))
        log.info("signal_generated", symbol=symbol, action="sell",
                 confidence=confidence, **meta)
        return Signal(
            action="sell",
            confidence=confidence,
            reasoning=(
                f"Death cross EMA{EMA_FAST}/EMA{EMA_SLOW} "
                f"(diff={curr_diff:+.2f}) | RSI={rsi:.1f}"
            ),
            symbol=symbol,
            metadata=meta,
        )

    # ── Pas de croisement ────────────────────────────────────────────────────
    trend = "haussiere" if curr_diff > 0 else "baissiere"
    rsi_zone = (
        "suracheté"  if rsi >= RSI_OVERBOUGHT else
        "survendu"   if rsi <= RSI_OVERSOLD   else
        "neutre"
    )
    return Signal(
        action="hold",
        confidence=0.5,
        reasoning=(
            f"Pas de croisement - tendance {trend} "
            f"(diff={curr_diff:+.2f}) | RSI={rsi:.1f} [{rsi_zone}]"
        ),
        symbol=symbol,
        metadata=meta,
    )
