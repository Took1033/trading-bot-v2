"""
BotSwarm - orchestre plusieurs bots de trading en parallele.

Architecture inspiree de Kairos Alpha v2 :
  - 3 bots specialises (BTC, ETH, SOL) avec poids d'allocation
  - 1 bot Dynamique (place reserve, a implementer)
  - 1 Director Agent qui monitore et applique le Kill Switch

Les bots partagent :
  - 1 seul PaperPortfolio (via CoinbaseClient partage)
  - 1 seule DB SQLite (via MemoryAgent partage)
  - 1 etat de pause partage (trading_state)

Chaque bot a :
  - Son propre symbol (BTC-USDC, ETH-USDC, SOL-USDC)
  - Son propre MarketAgent (historique de prix isole)
  - Son propre RiskAgent (config commune mais decisions independantes)
  - Son weight (allocation capital indicative pour le Director)
"""
from __future__ import annotations

import asyncio
import os

import structlog
from dotenv import load_dotenv

from agents.memory_agent import MemoryAgent
from agents.orchestrator import Orchestrator
from interfaces.coinbase_client import CoinbaseClient

load_dotenv()
log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Configuration des bots du swarm
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_BOTS = [
    {"bot_id": "btc",       "symbol": "BTC-USDC", "weight": 0.35, "name": "Bitcoin"},
    {"bot_id": "eth",       "symbol": "ETH-USDC", "weight": 0.30, "name": "Ethereum"},
    {"bot_id": "sol",       "symbol": "SOL-USDC", "weight": 0.20, "name": "Solana"},
    # Bot Dynamique : pour l'instant trade aussi BTC, sera switch dynamique
    {"bot_id": "dynamique", "symbol": "BTC-USDC", "weight": 0.15, "name": "Dynamique"},
]


class BotSwarm:
    """Gere un essaim de bots de trading en parallele."""

    def __init__(self, bots_config: list[dict] | None = None) -> None:
        config = bots_config or DEFAULT_BOTS

        # Ressources partagees entre tous les bots
        self._coinbase = CoinbaseClient()
        self._memory   = MemoryAgent()

        # Instancier un Orchestrateur par bot
        self.bots: list[Orchestrator] = []
        for cfg in config:
            bot = Orchestrator(
                symbol   = cfg["symbol"],
                bot_id   = cfg["bot_id"],
                weight   = cfg["weight"],
                coinbase = self._coinbase,
                memory   = self._memory,
            )
            # On stocke le nom pour le dashboard
            bot.display_name = cfg.get("name", cfg["bot_id"].upper())
            self.bots.append(bot)

        log.info("bot_swarm_ready", n_bots=len(self.bots),
                 bots=[{"id": b.bot_id, "symbol": b.symbol, "weight": b.weight} for b in self.bots])

    # ─────────────────────────────────────────────────────────────────────────
    # Lancement en parallele
    # ─────────────────────────────────────────────────────────────────────────

    async def run_all(self) -> None:
        """Lance tous les bots en parallele dans la meme boucle asyncio."""
        log.info("bot_swarm_starting", n_bots=len(self.bots))
        await asyncio.gather(
            *[bot.run_forever() for bot in self.bots],
            return_exceptions=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Accesseurs pour Director / dashboard
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> list[dict]:
        """Retourne l'etat de tous les bots."""
        from agents import trading_state
        out = []
        for bot in self.bots:
            pos       = bot._coinbase.get_position(bot.symbol)
            warmed_up = bot._market.is_warmed_up
            paused    = trading_state.is_paused(bot.bot_id)
            out.append({
                "bot_id":       bot.bot_id,
                "name":         getattr(bot, "display_name", bot.bot_id.upper()),
                "symbol":       bot.symbol,
                "weight":       bot.weight,
                "paused":       paused,
                "warmed_up":    warmed_up,
                "history_len":  len(bot._market.price_history),
                "position":     pos,
                "last_trade":   bot._last_trade_ts,
                "signal_streak": bot._signal_streak,
            })
        return out

    async def get_portfolio_total(self) -> float:
        """Retourne la valeur totale du portefeuille partage."""
        snap = await self._coinbase.get_portfolio_snapshot()
        return snap["total_usdc"]

    async def get_portfolio_snapshot(self) -> dict:
        return await self._coinbase.get_portfolio_snapshot()

    @property
    def coinbase(self) -> CoinbaseClient:
        return self._coinbase
