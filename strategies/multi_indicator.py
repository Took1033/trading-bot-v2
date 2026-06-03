"""
Stratégie multi-indicateurs : EMA + MACD + RSI + Bollinger + Volume + ATR.

Renforce simple_ma en ajoutant :
  - MACD(12,26,9)         : confirme momentum
  - Bollinger Bands(20,2) : detecte les extremes
  - Volume filter         : bloque les signaux a volume bas
  - ATR(14)               : sera utilise par l'orchestrator pour SL/TP dynamiques

Signal BUY  : EMA9>EMA21 (cross OR maintenue) + MACD haussier + RSI<70 + volume OK
Signal SELL : EMA9<EMA21 + MACD baissier + RSI>30 + volume OK

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


# Parametres (configurables via .env)
EMA_FAST       = 9
EMA_SLOW       = 21
EMA_TREND      = 50
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD   = 30.0
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
BB_PERIOD      = 20
BB_STD         = 2.0
ATR_PERIOD     = 14
VOL_FILTER     = float(os.getenv("MULTI_VOL_FILTER",       "0.7"))   # 0.7 = bloque si vol < 70% moyenne
VOL_MIN_PCT    = float(os.getenv("STRATEGY_VOL_MIN_PCT",   "0.05"))


# ─────────────────────────────────────────────────────────────────────────────
# Indicateurs
# ─────────────────────────────────────────────────────────────────────────────

def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    out = np.empty(len(prices))
    out[0] = prices[0]
    for i in range(1, len(prices)):
        out[i] = prices[i] * k + out[i - 1] * (1 - k)
    return out


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


def _macd(prices: np.ndarray) -> tuple[float, float, float]:
    """Retourne (macd_line, signal_line, histogram). Histogram > 0 = haussier."""
    ema_fast   = _ema(prices, MACD_FAST)
    ema_slow   = _ema(prices, MACD_SLOW)
    macd_line  = ema_fast - ema_slow
    signal     = _ema(macd_line, MACD_SIGNAL)
    histogram  = macd_line - signal
    return float(macd_line[-1]), float(signal[-1]), float(histogram[-1])


def _bollinger(prices: np.ndarray, period: int = BB_PERIOD, std_mult: float = BB_STD) -> tuple[float, float, float]:
    """Retourne (lower, middle, upper)."""
    if len(prices) < period:
        m = float(prices.mean())
        return m, m, m
    recent = prices[-period:]
    middle = float(recent.mean())
    std    = float(recent.std())
    return middle - std_mult * std, middle, middle + std_mult * std


def _atr(prices: np.ndarray, period: int = ATR_PERIOD) -> float:
    """
    ATR simplifie (high-low estime depuis close-to-close).
    Sans OHLC reel, on utilise abs(close[i] - close[i-1]) comme True Range proxy.
    """
    if len(prices) < period + 1:
        return 0.0
    trs = np.abs(np.diff(prices[-(period + 1):]))
    return float(trs.mean())


# ─────────────────────────────────────────────────────────────────────────────
# Stratégie principale
# ─────────────────────────────────────────────────────────────────────────────

async def analyze(symbol: str, prices: list[float], volumes: list[float] | None = None) -> Signal:
    if len(prices) < EMA_SLOW + 1:
        return Signal("hold", 0.0,
                      f"Donnees insuffisantes : {len(prices)}/{EMA_SLOW + 1}", symbol)

    arr      = np.array(prices, dtype=float)
    price    = float(arr[-1])
    ema_f    = _ema(arr, EMA_FAST)
    ema_s    = _ema(arr, EMA_SLOW)
    has_trend = len(arr) >= EMA_TREND + 1
    ema_t    = float(_ema(arr, EMA_TREND)[-1]) if has_trend else None
    rsi      = _rsi(arr)
    macd, macd_sig, macd_hist = _macd(arr)
    bb_lo, bb_mid, bb_hi      = _bollinger(arr)
    atr                       = _atr(arr)
    atr_pct                   = (atr / price * 100) if price > 0 else 0.0

    # Volatilite des 20 derniers prix
    recent_vol = float(np.std(arr[-20:]) / price * 100) if price > 0 and len(arr) >= 20 else 0.0

    # Volume check : si fourni, bloque si volume actuel < VOL_FILTER * moyenne
    volume_ok = True
    vol_ratio = 1.0
    if volumes and len(volumes) >= 20:
        v_arr     = np.array(volumes[-20:], dtype=float)
        v_mean    = float(v_arr.mean())
        v_curr    = float(v_arr[-1])
        vol_ratio = v_curr / v_mean if v_mean > 0 else 1.0
        volume_ok = vol_ratio >= VOL_FILTER

    # Filtre volatilite globale
    if recent_vol < VOL_MIN_PCT:
        meta = _meta(price, ema_f[-1], ema_s[-1], ema_t, rsi, macd_hist, bb_lo, bb_hi, atr, atr_pct, recent_vol, vol_ratio)
        return Signal("hold", 0.3,
                      f"Marche trop plat - vol={recent_vol:.3f}% < min {VOL_MIN_PCT}%",
                      symbol, meta)

    if not volume_ok:
        meta = _meta(price, ema_f[-1], ema_s[-1], ema_t, rsi, macd_hist, bb_lo, bb_hi, atr, atr_pct, recent_vol, vol_ratio)
        return Signal("hold", 0.3,
                      f"Volume insuffisant : ratio={vol_ratio:.2f} < seuil {VOL_FILTER}",
                      symbol, meta)

    # Score multi-indicateurs : 4 conditions a verifier (2/4 minimum pour signal)
    diff_curr = float(ema_f[-1] - ema_s[-1])
    diff_prev = float(ema_f[-2] - ema_s[-2])

    # ── Logique BUY ─────────────────────────────────────────────────────────
    buy_score    = 0
    buy_reasons  = []
    if diff_curr > 0 and diff_prev <= 0:
        buy_score += 2;  buy_reasons.append("EMA cross+")
    elif diff_curr > 0:
        buy_score += 1;  buy_reasons.append("EMA bullish")
    if macd_hist > 0:
        buy_score += 1;  buy_reasons.append("MACD+")
    if rsi < RSI_OVERBOUGHT and rsi > 45:
        buy_score += 1;  buy_reasons.append(f"RSI={rsi:.0f}")
    if price < bb_mid:    # achete dans la moitie basse
        buy_score += 1;  buy_reasons.append("BB<mid")
    if has_trend and price > ema_t:
        buy_score += 1;  buy_reasons.append("uptrend")

    # ── Logique SELL ────────────────────────────────────────────────────────
    sell_score   = 0
    sell_reasons = []
    if diff_curr < 0 and diff_prev >= 0:
        sell_score += 2;  sell_reasons.append("EMA cross-")
    elif diff_curr < 0:
        sell_score += 1;  sell_reasons.append("EMA bearish")
    if macd_hist < 0:
        sell_score += 1;  sell_reasons.append("MACD-")
    if rsi > RSI_OVERSOLD and rsi < 55:
        sell_score += 1;  sell_reasons.append(f"RSI={rsi:.0f}")
    if price > bb_mid:
        sell_score += 1;  sell_reasons.append("BB>mid")
    if has_trend and price < ema_t:
        sell_score += 1;  sell_reasons.append("downtrend")

    meta = _meta(price, ema_f[-1], ema_s[-1], ema_t, rsi, macd_hist, bb_lo, bb_hi, atr, atr_pct, recent_vol, vol_ratio)

    # Filtre RSI extremes (anti-FOMO / anti-panic)
    if rsi >= RSI_OVERBOUGHT:
        return Signal("hold", 0.4, f"RSI suracheté ({rsi:.1f}>={RSI_OVERBOUGHT})", symbol, meta)
    if rsi <= RSI_OVERSOLD:
        return Signal("hold", 0.4, f"RSI survendu ({rsi:.1f}<={RSI_OVERSOLD})", symbol, meta)

    # Decision : need 3+ pour BUY ou SELL (sur ~6 conditions)
    if buy_score >= 3 and buy_score > sell_score:
        conf = round(min(0.50 + buy_score * 0.08, 0.95), 2)
        return Signal("buy", conf,
                      f"BUY {buy_score}/6 [{', '.join(buy_reasons)}]",
                      symbol, meta)
    if sell_score >= 3 and sell_score > buy_score:
        conf = round(min(0.50 + sell_score * 0.08, 0.95), 2)
        return Signal("sell", conf,
                      f"SELL {sell_score}/6 [{', '.join(sell_reasons)}]",
                      symbol, meta)

    return Signal("hold", 0.5,
                  f"Score insuffisant : buy={buy_score}/6, sell={sell_score}/6",
                  symbol, meta)


def _meta(price, ema_f, ema_s, ema_t, rsi, macd_hist, bb_lo, bb_hi, atr, atr_pct, vol_pct, vol_ratio) -> dict:
    return {
        "ema_fast":   round(float(ema_f), 2),
        "ema_slow":   round(float(ema_s), 2),
        "ema_trend":  round(ema_t, 2) if ema_t else None,
        "rsi":        round(rsi, 1),
        "macd_hist":  round(macd_hist, 4),
        "bb_lower":   round(bb_lo, 2),
        "bb_upper":   round(bb_hi, 2),
        "atr":        round(atr, 4),
        "atr_pct":    round(atr_pct, 3),    # utilise pour SL/TP dynamiques
        "vol_pct":    round(vol_pct, 3),
        "vol_ratio":  round(vol_ratio, 2),
        "price":      round(price, 2),
    }
