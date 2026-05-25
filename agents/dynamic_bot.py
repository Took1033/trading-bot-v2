"""
DynamicBot - bot qui choisit dynamiquement le meilleur symbole a trader.

Logique :
  - Evalue la performance 24h de BTC/ETH/SOL toutes les SWITCH_INTERVAL_H heures
  - Choisit le symbole avec le meilleur momentum (plus haute variation 24h)
  - Remplace l'orchestrateur interne si un meilleur candidat est detecte
  - Warm-up automatique apres chaque switch

Expose exactement la meme interface qu'un Orchestrator (pour BotSwarm.get_status).
"""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp
import structlog
from dotenv import load_dotenv

from agents.orchestrator import Orchestrator
from interfaces import notifier

load_dotenv()
log = structlog.get_logger()

CANDIDATES        = ["BTC-USDC", "ETH-USDC", "SOL-USDC"]
SWITCH_INTERVAL_H = float(os.getenv("DYNAMIC_SWITCH_INTERVAL_H", "4"))
LOOP_INTERVAL_S   = int(os.getenv("LOOP_INTERVAL_S", "60"))

# Description texte du Fear & Greed (utilise pour les logs uniquement)
_FG_LABELS = {range(0, 20): "Extreme Fear", range(20, 40): "Fear",
              range(40, 60): "Neutral",      range(60, 80): "Greed",
              range(80, 101): "Extreme Greed"}


class DynamicBot:
    """
    Bot dynamique : change de symbole selon la performance 24h.
    Expose la meme interface qu'un Orchestrator pour BotSwarm.get_status().
    """

    def __init__(self, coinbase, memory, weight: float = 0.15) -> None:
        self._coinbase    = coinbase
        self._memory      = memory
        self.weight       = weight
        self.bot_id       = "dynamique"
        self.display_name = "Dynamique"

        # Orchestrateur courant (remplace si symbole change)
        self._orc            = self._make_orc("BTC-USDC")
        self._last_switch_ts = 0.0
        self._last_perfs: dict[str, float] = {}

        log.info("dynamic_bot_ready",
                 candidates=CANDIDATES,
                 switch_interval_h=SWITCH_INTERVAL_H)

    # ─────────────────────────────────────────────────────────────────────────
    # Proxy vers l'orchestrateur courant (interface BotSwarm.get_status)
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def symbol(self) -> str:
        return self._orc.symbol

    @property
    def _market(self):
        return self._orc._market

    @property
    def _last_trade_ts(self) -> float:
        return self._orc._last_trade_ts

    @property
    def _signal_streak(self) -> dict:
        return self._orc._signal_streak

    def get_last_perfs(self) -> dict[str, float]:
        """Retourne les dernieres performances 24h evaluees."""
        return dict(self._last_perfs)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _make_orc(self, symbol: str) -> Orchestrator:
        return Orchestrator(
            symbol=symbol,
            bot_id="dynamique",
            weight=self.weight,
            coinbase=self._coinbase,
            memory=self._memory,
        )

    async def _get_24h_change(self, symbol: str) -> float | None:
        """
        Retourne la variation % 24h du symbole via API Exchange Coinbase.
        Format candle : [time, low, high, open, close, volume]
        candles[0] = bougie la plus recente.
        """
        try:
            url = (
                f"https://api.exchange.coinbase.com/products/{symbol}/candles"
                f"?granularity=86400&limit=2"
            )
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        return None
                    candles = await r.json()

            if not candles or len(candles) < 2:
                return None

            current_close = float(candles[0][4])
            prev_close    = float(candles[1][4])
            if prev_close <= 0:
                return None
            return (current_close - prev_close) / prev_close * 100

        except Exception as exc:
            log.warning("dynamic_24h_fetch_error", symbol=symbol, error=str(exc))
            return None

    async def _evaluate_and_switch(self) -> None:
        """Evalue les 3 candidats et switche vers le meilleur si necessaire."""
        performances: dict[str, float] = {}
        for sym in CANDIDATES:
            chg = await self._get_24h_change(sym)
            if chg is not None:
                performances[sym] = chg

        if not performances:
            log.warning("dynamic_bot_no_perf_data")
            return

        self._last_perfs = performances
        best = max(performances, key=lambda s: performances[s])

        log.info("dynamic_bot_evaluated",
                 perfs={k: round(v, 2) for k, v in performances.items()},
                 best=best, current=self._orc.symbol)

        if best == self._orc.symbol:
            log.info("dynamic_bot_stays", symbol=best)
            return

        # Nouvelle cible
        old_symbol = self._orc.symbol

        # Ne pas switcher si on a une position ouverte (attendre qu'elle soit cloturee)
        pos = self._coinbase.get_position(old_symbol)
        if pos and pos.get("qty", 0) > 0:
            log.info("dynamic_bot_switch_deferred",
                     reason="position_open", symbol=old_symbol, target=best)
            return

        self._orc = self._make_orc(best)
        await self._orc._market.warmup_from_history()

        perf_lines = " | ".join(
            f"`{s}`: `{p:+.1f}%`"
            for s, p in sorted(performances.items(), key=lambda x: -x[1])
        )
        await notifier.notify(
            f"🔄 *Bot Dynamique — Nouveau symbole*\n"
            f"`{old_symbol}` → `{best}`\n"
            f"Performances 24h : {perf_lines}"
        )
        log.info("dynamic_bot_switched", old=old_symbol, new=best)

    # ─────────────────────────────────────────────────────────────────────────
    # Boucle principale
    # ─────────────────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Boucle : warm-up → premiere evaluation → ticks periodiques."""
        log.info("dynamic_bot_start", symbol=self._orc.symbol)

        # Warm-up initial
        await self._orc._market.warmup_from_history()

        # Premiere evaluation immediate
        self._last_switch_ts = time.time()
        try:
            await self._evaluate_and_switch()
        except Exception as exc:
            log.error("dynamic_bot_initial_eval_error", error=str(exc))

        try:
            while True:
                # Reevaluation periodique
                now = time.time()
                if now - self._last_switch_ts >= SWITCH_INTERVAL_H * 3600:
                    self._last_switch_ts = now
                    try:
                        await self._evaluate_and_switch()
                    except Exception as exc:
                        log.error("dynamic_bot_switch_error", error=str(exc))

                # Tick du bot courant
                try:
                    await self._orc._tick()
                except Exception as exc:
                    log.error("dynamic_bot_tick_error", error=str(exc))

                await asyncio.sleep(LOOP_INTERVAL_S)

        except asyncio.CancelledError:
            log.info("dynamic_bot_stopped")
