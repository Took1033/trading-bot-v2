"""
Ensemble strategy : vote pondere entre N strategies.

Logique :
  - Chaque strategie produit un Signal (buy/sell/hold + confidence)
  - On agrege par vote pondere par la confiance de chaque signal
  - Decision finale = action majoritaire si score > seuil, sinon HOLD
  - Confidence finale = moyenne des confidences des votants gagnants

Permet de detecter quand TOUS les indicateurs convergent (= signal fort).
Reduit drastiquement les faux signaux.

Interface : async def analyze(symbol, prices, volumes=None) -> Signal
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

import structlog
from dotenv import load_dotenv

from strategies.multi_indicator import analyze as multi_analyze
from strategies.mean_reversion  import analyze as mr_analyze
from strategies.simple_ma       import analyze as ma_analyze

load_dotenv()
log = structlog.get_logger()


@dataclass
class Signal:
    action:     Literal["buy", "sell", "hold"]
    confidence: float
    reasoning:  str
    symbol:     str
    metadata:   dict = field(default_factory=dict)


# Strategies actives et leur poids dans le vote
# Multi-indicator a le plus de poids car le plus robuste
STRATEGIES: list[tuple[str, Callable, float]] = [
    ("multi_indicator", multi_analyze, 1.5),
    ("simple_ma",       ma_analyze,    1.0),
    ("mean_reversion",  mr_analyze,    1.0),
]

# Seuil de score pour declencher un trade
MIN_VOTE_SCORE = float(os.getenv("ENSEMBLE_MIN_SCORE", "1.2"))


async def analyze(symbol: str, prices: list[float], volumes: list[float] | None = None) -> Signal:
    """Vote pondere entre toutes les strategies actives."""
    signals: list[tuple[str, float, "Signal"]] = []   # (name, weight, signal)

    for name, fn, weight in STRATEGIES:
        try:
            # Toutes les strategies ont la meme signature
            try:
                sig = await fn(symbol, prices, volumes)
            except TypeError:
                # Strategie qui ne prend pas volumes (simple_ma)
                sig = await fn(symbol, prices)
            signals.append((name, weight, sig))
        except Exception as exc:
            log.warning("ensemble_strategy_failed", strategy=name, error=str(exc))

    if not signals:
        return Signal("hold", 0.0, "Ensemble : aucune strategie n'a repondu", symbol)

    # Vote pondere : score = weight * confidence pour chaque vote
    buy_score   = 0.0
    sell_score  = 0.0
    buy_voters  = []
    sell_voters = []
    hold_voters = []

    for name, weight, sig in signals:
        score = weight * sig.confidence
        if sig.action == "buy":
            buy_score += score
            buy_voters.append((name, sig.confidence))
        elif sig.action == "sell":
            sell_score += score
            sell_voters.append((name, sig.confidence))
        else:
            hold_voters.append((name, sig.confidence))

    log.info("ensemble_vote",
             symbol=symbol,
             buy=round(buy_score, 2), sell=round(sell_score, 2),
             buy_voters=[n for n, _ in buy_voters],
             sell_voters=[n for n, _ in sell_voters],
             hold_voters=[n for n, _ in hold_voters])

    # Metadata enrichie : on prend celle de multi_indicator (la plus complete)
    meta = {}
    for name, _, sig in signals:
        if name == "multi_indicator":
            meta = dict(sig.metadata)
            break

    meta["ensemble_buy"]   = round(buy_score, 2)
    meta["ensemble_sell"]  = round(sell_score, 2)
    meta["ensemble_voters_buy"]  = [n for n, _ in buy_voters]
    meta["ensemble_voters_sell"] = [n for n, _ in sell_voters]

    # Decision : action gagnante si score > seuil ET > l'autre
    if buy_score > sell_score and buy_score >= MIN_VOTE_SCORE:
        confs = [c for _, c in buy_voters]
        return Signal(
            action="buy",
            confidence=round(sum(confs) / len(confs), 2),
            reasoning=(
                f"Ensemble BUY (score={buy_score:.2f} vs SELL={sell_score:.2f}) "
                f"| voters: {', '.join(n for n, _ in buy_voters)}"
            ),
            symbol=symbol, metadata=meta,
        )

    if sell_score > buy_score and sell_score >= MIN_VOTE_SCORE:
        confs = [c for _, c in sell_voters]
        return Signal(
            action="sell",
            confidence=round(sum(confs) / len(confs), 2),
            reasoning=(
                f"Ensemble SELL (score={sell_score:.2f} vs BUY={buy_score:.2f}) "
                f"| voters: {', '.join(n for n, _ in sell_voters)}"
            ),
            symbol=symbol, metadata=meta,
        )

    return Signal(
        action="hold", confidence=0.5,
        reasoning=(
            f"Ensemble HOLD : pas de consensus (buy={buy_score:.2f}, sell={sell_score:.2f}, "
            f"seuil={MIN_VOTE_SCORE})"
        ),
        symbol=symbol, metadata=meta,
    )
