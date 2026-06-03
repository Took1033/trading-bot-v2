"""
MarketAgent - recupere les prix et genere des signaux de trading.

Maintient un historique glissant en memoire (deque).
Appelle la strategie configuree et retourne un artifact MCP.

Optimisation : warmup_from_history() prefetche les prix recents au demarrage
pour etre operationnel instantanement (sans attendre 22 ticks).
"""
from __future__ import annotations

import os
from collections import deque

import structlog
from dotenv import load_dotenv

from interfaces.coinbase_client import CoinbaseClient
from strategies.simple_ma   import Signal
from strategies.ensemble    import analyze as ensemble_analyze
from strategies.simple_ma   import analyze as simple_ma_analyze

load_dotenv()
log = structlog.get_logger()

PRICE_HISTORY_SIZE = int(os.getenv("PRICE_HISTORY_SIZE", "60"))
LOOP_INTERVAL_S    = int(os.getenv("LOOP_INTERVAL_S", "60"))

# Active la strategie ensemble (vote pondere entre 3 strategies)
# Sinon utilise simple_ma seul (fallback / debug)
USE_ENSEMBLE = os.getenv("USE_ENSEMBLE_STRATEGY", "true").lower() in ("true", "1", "yes")
analyze = ensemble_analyze if USE_ENSEMBLE else simple_ma_analyze

# Correspondance intervalle -> granularite Coinbase Exchange API (en secondes)
_GRAN_MAP = {60: 60, 300: 300, 900: 900, 3600: 3600, 21600: 21600, 86400: 86400}


class MarketAgent:
    """Analyse de marche en temps reel."""

    def __init__(self, symbol: str = "BTC-USDC") -> None:
        self.symbol  = symbol
        self._client = CoinbaseClient()
        self._prices: deque[float] = deque(maxlen=PRICE_HISTORY_SIZE)

    # ─────────────────────────────────────────────────────────────────────────
    # Warm-up instantane
    # ─────────────────────────────────────────────────────────────────────────

    async def warmup_from_history(self, n: int | None = None) -> None:
        """
        Pre-remplit l'historique de prix depuis l'API publique Coinbase Exchange.
        Permet d'etre operationnel immediatement sans attendre N ticks.

        Args:
            n: nombre de prix a charger (defaut: PRICE_HISTORY_SIZE)
        """
        n = n or PRICE_HISTORY_SIZE
        # Convertir BTC-USDC -> BTC-USD pour l'API Exchange (qui n'a pas USDC)
        exchange_symbol = self.symbol.replace("USDC", "USD")
        granularity     = _GRAN_MAP.get(LOOP_INTERVAL_S, 60)

        try:
            from strategies.backtester import fetch_prices_coinbase
            prices = await fetch_prices_coinbase(
                symbol=exchange_symbol,
                granularity=granularity,
                limit=n + 5,   # marge de securite
            )
            # Garder les N plus recents
            for p in prices[-n:]:
                self._prices.append(float(p))

            log.info(
                "market_agent_warmed_up",
                symbol=self.symbol,
                history_len=len(self._prices),
                granularity_s=granularity,
            )
        except Exception as exc:
            log.warning(
                "warmup_failed",
                error=str(exc),
                hint="Warm-up progressif en cours (1 tick/min)",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Interface MCP
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self) -> dict:
        """
        Fetch prix, met a jour l'historique, genere un signal.
        Retourne un artifact ou error au format MCP.
        """
        try:
            price = await self._client.get_price(self.symbol)
            self._prices.append(price)

            signal: Signal = await analyze(self.symbol, list(self._prices))

            log.info(
                "market_tick",
                symbol=self.symbol,
                price=round(price, 2),
                action=signal.action,
                confidence=round(signal.confidence, 2),
                rsi=signal.metadata.get("rsi", "n/a"),
                history_len=len(self._prices),
            )

            return {
                "node_type": "artifact",
                "sender":    "market_agent",
                "receiver":  "orchestrator",
                "payload": {
                    "symbol":      self.symbol,
                    "price":       price,
                    "signal": {
                        "action":     signal.action,
                        "confidence": signal.confidence,
                        "reasoning":  signal.reasoning,
                        "metadata":   signal.metadata,
                    },
                    "history_len": len(self._prices),
                },
            }

        except Exception as exc:
            log.error("market_agent_error", error=str(exc), symbol=self.symbol)
            return {
                "node_type": "error",
                "sender":    "market_agent",
                "receiver":  "orchestrator",
                "payload":   {"error": str(exc)},
            }

    # ─────────────────────────────────────────────────────────────────────────
    # Accesseurs
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def price_history(self) -> list[float]:
        return list(self._prices)

    @property
    def is_warmed_up(self) -> bool:
        """True quand l'historique est suffisant pour calculer les EMAs."""
        return len(self._prices) >= 22
