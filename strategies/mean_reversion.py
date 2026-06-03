"""
Stratégie Mean Reversion : RSI extremes + Bollinger Bands.

Complementaire a multi_indicator (trend-following). Trade quand le marche est
"trop etire" et susceptible de revenir vers la moyenne.

  BUY  : RSI <= 30  ET  prix <= Bollinger Lower
  SELL : RSI >= 70  ET  prix >= Bollinger Upper

Interface : async def analyze(symbol, prices, volumes=None) -> Signal
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()


@dataclass
class Signal:
    action:     Literal["buy", "sell", "hold"]
    confidence: float
    reasoning:  str
    symbol:     str
    metadata:   dict = field(default_factory=dict)


RSI_PERIOD       = 14
RSI_BUY_THRESH   = float(os.getenv("MR_RSI_BUY",  "32"))     # acheter sous 32
RSI_SELL_THRESH  = float(os.getenv("MR_RSI_SELL", "68"))     # vendre au-dessus de 68
BB_PERIOD        = 20
BB_STD           = 2.0
MIN_POINTS       = max(RSI_PERIOD + 1, BB_PERIOD)


def _rsi(prices: np.ndarray, period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0).mean()
    losses = np.where(deltas < 0, -deltas, 0.0).mean()
    if losses == 0:
        return 100.0
    rs = gains / losses
    return float(100.0 - 100.0 / (1.0 + rs))


def _bollinger(prices: np.ndarray, period: int = BB_PERIOD, std_mult: float = BB_STD) -> tuple[float, float, float]:
    if len(prices) < period:
        m = float(prices.mean())
        return m, m, m
    recent = prices[-period:]
    middle = float(recent.mean())
    std    = float(recent.std())
    return middle - std_mult * std, middle, middle + std_mult * std


async def analyze(symbol: str, prices: list[float], volumes: list[float] | None = None) -> Signal:
    if len(prices) < MIN_POINTS:
        return Signal("hold", 0.0,
                      f"Donnees insuffisantes : {len(prices)}/{MIN_POINTS}", symbol)

    arr   = np.array(prices, dtype=float)
    price = float(arr[-1])
    rsi   = _rsi(arr)
    bb_lo, bb_mid, bb_hi = _bollinger(arr)

    # Distance par rapport aux bandes (normalisee)
    band_width = max(bb_hi - bb_lo, 1e-8)
    pos_in_band = (price - bb_lo) / band_width   # 0 = bas, 1 = haut

    meta = {
        "rsi":         round(rsi, 1),
        "bb_lower":    round(bb_lo, 2),
        "bb_middle":   round(bb_mid, 2),
        "bb_upper":    round(bb_hi, 2),
        "pos_in_band": round(pos_in_band, 2),
        "price":       round(price, 2),
    }

    # BUY : RSI tres bas + prix sous bande basse
    if rsi <= RSI_BUY_THRESH and price <= bb_lo:
        # Confidence : plus le RSI est bas et plus le prix est sous la bande, plus on est confiant
        rsi_score = (RSI_BUY_THRESH - rsi) / RSI_BUY_THRESH   # 0 a ~1
        bb_score  = max(0, (bb_lo - price) / bb_lo) * 10      # depasse la bande
        conf      = round(min(0.55 + rsi_score * 0.20 + bb_score * 0.10, 0.92), 2)
        return Signal("buy", conf,
                      f"Mean Reversion BUY : RSI={rsi:.1f} oversold + prix sous BB_lo",
                      symbol, meta)

    # SELL : RSI tres haut + prix au-dessus bande haute
    if rsi >= RSI_SELL_THRESH and price >= bb_hi:
        rsi_score = (rsi - RSI_SELL_THRESH) / (100 - RSI_SELL_THRESH)
        bb_score  = max(0, (price - bb_hi) / bb_hi) * 10
        conf      = round(min(0.55 + rsi_score * 0.20 + bb_score * 0.10, 0.92), 2)
        return Signal("sell", conf,
                      f"Mean Reversion SELL : RSI={rsi:.1f} overbought + prix au-dessus BB_hi",
                      symbol, meta)

    return Signal("hold", 0.5,
                  f"Pas d'extreme : RSI={rsi:.1f}, prix dans bande (pos={pos_in_band:.2f})",
                  symbol, meta)
