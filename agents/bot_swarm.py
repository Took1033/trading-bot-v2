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
import time

import structlog
from dotenv import load_dotenv

from agents import autoclose
from agents.dynamic_bot  import DynamicBot
from agents.memory_agent import MemoryAgent
from agents.orchestrator import Orchestrator
from agents.trend_bot    import TrendBot
from interfaces.coinbase_client import CoinbaseClient
from bot_config import load_bots_config, save_bots_config, symbol_exists, validate_symbol_format

load_dotenv()
log = structlog.get_logger()

# Délai entre le démarrage de chaque bot au boot (anti-burst 429 Coinbase).
STARTUP_STAGGER_S = float(os.getenv("SWARM_STARTUP_STAGGER_S", "3.0"))
# Fraicheur max du snapshot servi au dashboard (lecture seule, pas de trading).
DASHBOARD_SNAPSHOT_TTL_S = float(os.getenv("DASHBOARD_SNAPSHOT_TTL_S", "5.0"))


class BotSwarm:
    """Gere un essaim de bots de trading en parallele."""

    def __init__(self, bots_config: list[dict] | None = None) -> None:
        # P0 : config chargée depuis config/bots.json (fallback defaults)
        config = bots_config if bots_config is not None else load_bots_config()

        # Ressources partagees entre tous les bots
        self._coinbase = CoinbaseClient()
        self._memory   = MemoryAgent()
        # (monotonic, snapshot) du dernier fetch servi au dashboard. Cf.
        # get_portfolio_snapshot : cache de lecture, jamais lu par les bots.
        self._dash_snap_cache: tuple[float, dict] | None = None
        # Restaure l'avg_price d'une position re-adoptee apres reboot depuis la DB
        # (dernier achat connu) au lieu de l'ecraser avec le prix courant.
        self._coinbase.entry_price_resolver = self._memory.last_entry_price

        # Tâches asyncio par bot (pour add/remove à chaud — P2)
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

        # Instancier un Orchestrateur par bot (DynamicBot pour "dynamique")
        self.bots: list[Orchestrator | DynamicBot] = []
        for cfg in config:
            self.bots.append(self._make_bot(cfg))

        log.info("bot_swarm_ready", n_bots=len(self.bots),
                 bots=[{"id": b.bot_id, "symbol": b.symbol, "weight": b.weight} for b in self.bots])

    # ─────────────────────────────────────────────────────────────────────────
    # Fabrique de bots
    # ─────────────────────────────────────────────────────────────────────────

    def _make_bot(self, cfg: dict):
        """Instancie un bot (Orchestrator / DynamicBot / TrendBot) depuis une config dict."""
        if cfg.get("type") == "trend":
            bot = TrendBot(
                symbol   = cfg["symbol"],
                bot_id   = cfg["bot_id"],
                weight   = cfg.get("weight", 0.1),
                coinbase = self._coinbase,
                memory   = self._memory,
            )
            bot.display_name = cfg.get("name", bot.display_name)
        elif cfg["bot_id"] == "dynamique":
            bot = DynamicBot(
                coinbase = self._coinbase,
                memory   = self._memory,
                weight   = cfg.get("weight", 0.15),
            )
            bot.display_name = cfg.get("name", "Dynamique")
        else:
            bot = Orchestrator(
                symbol   = cfg["symbol"],
                bot_id   = cfg["bot_id"],
                weight   = cfg.get("weight", 1.0),
                coinbase = self._coinbase,
                memory   = self._memory,
            )
            bot.display_name = cfg.get("name", cfg["bot_id"].upper())
        return bot

    def _find_bot(self, bot_id: str):
        return next((b for b in self.bots if b.bot_id == bot_id.lower()), None)

    def _current_config(self) -> list[dict]:
        """Reconstruit la config courante (pour persistance)."""
        out = []
        for b in self.bots:
            entry = {
                "bot_id": b.bot_id,
                "symbol": b.symbol,
                "weight": b.weight,
                "name":   getattr(b, "display_name", b.bot_id.upper()),
            }
            if isinstance(b, TrendBot):
                entry["type"] = "trend"
            out.append(entry)
        return out

    def _persist(self) -> None:
        """P3 : sauvegarde la config courante sur disque."""
        save_bots_config(self._current_config())

    # ─────────────────────────────────────────────────────────────────────────
    # Lancement en parallele (superviseur de tâches — P2)
    # ─────────────────────────────────────────────────────────────────────────

    async def run_all(self) -> None:
        """Lance chaque bot dans sa propre tâche asyncio, puis supervise."""
        log.info("bot_swarm_starting", n_bots=len(self.bots),
                 stagger_s=STARTUP_STAGGER_S)
        self._running = True
        # Réconcilie le suivi local avec les vrais soldes Coinbase AVANT de lancer
        # les bots : sinon, au 1er tick après reboot, get_position() renvoie None
        # (mémoire vide) et un TrendBot pourrait re-racheter une position déjà
        # détenue. No-op en mode paper.
        try:
            await self._coinbase.sync_live_positions()
        except Exception as exc:
            log.warning("swarm_initial_sync_failed", error=str(exc))
        # Démarrage échelonné : évite le pic de 429 Coinbase quand les N bots
        # warm-up (fetch candles) tous en même temps au boot.
        for i, bot in enumerate(self.bots):
            self._spawn(bot, delay=i * STARTUP_STAGGER_S)
        # Superviseur : maintient run_all() en vie pour permettre add/remove à chaud
        try:
            while self._running:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            for task in self._tasks.values():
                task.cancel()
            raise

    def _spawn(self, bot, delay: float = 0.0) -> None:
        """Crée (ou recrée) la tâche run_forever d'un bot, avec délai initial optionnel."""
        old = self._tasks.get(bot.bot_id)
        if old and not old.done():
            old.cancel()

        async def _delayed_run():
            if delay > 0:
                await asyncio.sleep(delay)
            await bot.run_forever()

        self._tasks[bot.bot_id] = asyncio.create_task(
            _delayed_run(), name=f"bot-{bot.bot_id}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # P1 : changement de paire à chaud
    # ─────────────────────────────────────────────────────────────────────────

    async def set_pair(self, bot_id: str, new_symbol: str) -> dict:
        """Change la paire d'un bot. Refuse si position ouverte ou symbole invalide."""
        new_symbol = new_symbol.upper()
        bot = self._find_bot(bot_id)
        if not bot:
            return {"ok": False, "error": f"bot inconnu : {bot_id}"}
        if bot.bot_id == "dynamique":
            return {"ok": False, "error": "le bot Dynamique choisit sa paire automatiquement"}
        if not validate_symbol_format(new_symbol):
            return {"ok": False, "error": f"format de paire invalide : {new_symbol}"}
        if not await symbol_exists(new_symbol):
            return {"ok": False, "error": f"paire introuvable sur Coinbase : {new_symbol}"}

        old_symbol = bot.symbol
        if old_symbol == new_symbol:
            return {"ok": False, "error": f"le bot trade déjà {new_symbol}"}

        pos = self._coinbase.get_position(old_symbol)
        if pos and pos.get("qty", 0) > 0:
            return {"ok": False, "error": f"position ouverte sur {old_symbol} — ferme-la avant de changer de paire"}

        await bot.switch_symbol(new_symbol)
        self._persist()
        log.info("bot_pair_changed", bot_id=bot.bot_id, old=old_symbol, new=new_symbol)
        return {"ok": True, "bot_id": bot.bot_id, "old": old_symbol, "new": new_symbol}

    # ─────────────────────────────────────────────────────────────────────────
    # P2 : ajout / suppression de bots à la volée
    # ─────────────────────────────────────────────────────────────────────────

    async def add_bot(self, bot_id: str, symbol: str,
                      weight: float = 0.1, name: str | None = None,
                      bot_type: str = "trend") -> dict:
        """Ajoute un bot sur une paire arbitraire et démarre sa tâche.

        bot_type="trend" par defaut : la strategie actuelle est trend-only.
        (bot_type autre que "trend" -> Orchestrator scalpeur, a la demande.)
        """
        bot_id = bot_id.lower().strip()
        symbol = symbol.upper()
        if not bot_id.isalnum():
            return {"ok": False, "error": "bot_id doit être alphanumérique (ex: ada)"}
        if self._find_bot(bot_id):
            return {"ok": False, "error": f"bot_id déjà utilisé : {bot_id}"}
        if not validate_symbol_format(symbol):
            return {"ok": False, "error": f"format de paire invalide : {symbol}"}
        if not await symbol_exists(symbol):
            return {"ok": False, "error": f"paire introuvable sur Coinbase : {symbol}"}

        cfg = {"bot_id": bot_id, "symbol": symbol,
               "weight": weight, "name": name or bot_id.upper()}
        # Strategie actuelle = trend-only : un bot ajoute doit etre un TrendBot.
        # Sans "type", _make_bot retombait sur un Orchestrator (scalpeur) — soit un
        # bot d'un AUTRE type que la strategie validee (stops serres, take-profit).
        if bot_id != "dynamique":
            cfg["type"] = bot_type
        bot = self._make_bot(cfg)
        self.bots.append(bot)
        if self._running:
            self._spawn(bot)
        self._persist()
        log.info("bot_added", bot_id=bot_id, symbol=symbol, weight=weight)
        return {"ok": True, "bot_id": bot_id, "symbol": symbol, "weight": weight}

    async def remove_bot(self, bot_id: str) -> dict:
        """Retire un bot du swarm. Refuse si position ouverte."""
        bot_id = bot_id.lower().strip()
        bot = self._find_bot(bot_id)
        if not bot:
            return {"ok": False, "error": f"bot inconnu : {bot_id}"}

        pos = self._coinbase.get_position(bot.symbol)
        if pos and pos.get("qty", 0) > 0:
            return {"ok": False, "error": f"position ouverte sur {bot.symbol} — ferme-la avant de retirer le bot"}

        task = self._tasks.pop(bot_id, None)
        if task and not task.done():
            task.cancel()
        self.bots = [b for b in self.bots if b.bot_id != bot_id]
        self._persist()
        log.info("bot_removed", bot_id=bot_id)
        return {"ok": True, "bot_id": bot_id}

    # ─────────────────────────────────────────────────────────────────────────
    # Clôture manuelle d'une position (bouton "Clôturer" dashboard / Telegram)
    # ─────────────────────────────────────────────────────────────────────────

    async def force_close(self, bot_id: str) -> dict:
        """Vend toute la position d'un bot au marché, puis met le bot en pause.

        Pensé pour verrouiller le P&L affiché à l'instant T (le trend-following n'a
        pas de take-profit). On met en pause AVANT de vendre pour qu'aucun tick ne
        re-rentre pendant l'opération, et on LAISSE en pause après (sinon un trend
        encore haussier rachèterait au cycle suivant). Réversible : reprendre le bot.
        """
        from agents import trading_state
        bot_id = bot_id.lower().strip()
        bot = self._find_bot(bot_id)
        if not bot:
            return {"ok": False, "error": f"bot inconnu : {bot_id}"}

        symbol = bot.symbol
        pos    = self._coinbase.get_position(symbol)
        if not pos or pos.get("qty", 0) <= 0:
            return {"ok": False, "error": f"aucune position ouverte sur {symbol}"}

        # 1) Pause d'abord : empêche un tick de racheter pendant/après la vente.
        trading_state.pause(bot_id)

        qty       = pos["qty"]
        avg_price = pos.get("avg_price", 0.0) or 0.0
        try:
            order = await self._coinbase.place_order(symbol, "sell", qty, force=True)
        except Exception as exc:
            log.error("force_close_failed", bot_id=bot_id, symbol=symbol, error=str(exc))
            return {"ok": False, "error": f"vente refusée : {exc}",
                    "paused": True, "symbol": symbol}

        fill_price = getattr(order, "price", 0.0) or avg_price
        pnl_pct    = ((fill_price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0

        # Trace immuable + snapshot (mode live/paper résolu automatiquement).
        self._memory.record_decision(
            role="user", task_type="order", symbol=symbol, action="sell",
            confidence=1.0,
            reasoning=f"CLOTURE MANUELLE : {pnl_pct:+.2f}% (vente {qty:.8f} @ {fill_price:.6f})",
            metadata=(f'{{"order_id":"{getattr(order, "order_id", "")}",'
                      f'"price":{round(fill_price, 6)},"qty":{round(qty, 8)},"manual":true}}'),
        )
        try:
            self._memory.record_snapshot(await self._coinbase.get_portfolio_snapshot())
        except Exception as exc:
            log.debug("force_close_snapshot_failed", error=str(exc))

        # Notification Telegram best-effort (ne doit jamais casser la clôture).
        try:
            from interfaces import notifier
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            await notifier.notify(
                f"✅ *Clôture manuelle* `{symbol}`\n"
                f"Vente `{qty:.6f}` @ `{fill_price:,.4f}` | P&L {emoji} `{pnl_pct:+.2f}%`\n"
                f"_Bot_ `{bot_id}` _mis en pause._"
            )
        except Exception as exc:
            log.debug("force_close_notify_failed", error=str(exc))

        log.info("force_close_done", bot_id=bot_id, symbol=symbol,
                 qty=round(qty, 8), price=round(fill_price, 6), pnl_pct=round(pnl_pct, 2))
        return {"ok": True, "bot_id": bot_id, "symbol": symbol,
                "qty": qty, "price": fill_price, "pnl_pct": pnl_pct, "paused": True}

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
            entry = {
                "bot_id":        bot.bot_id,
                "name":          getattr(bot, "display_name", bot.bot_id.upper()),
                "symbol":        bot.symbol,
                "weight":        bot.weight,
                "paused":        paused,
                "warmed_up":     warmed_up,
                "history_len":   len(bot._market.price_history),
                "position":      pos,
                "last_trade":    bot._last_trade_ts,
                "signal_streak": bot._signal_streak,
            }
            # Infos supplementaires pour le bot dynamique
            if bot.bot_id == "dynamique" and hasattr(bot, "get_last_perfs"):
                entry["dynamic_perfs"] = bot.get_last_perfs()
            # Config "close reglable" (TrendBots seulement)
            if isinstance(bot, TrendBot):
                entry["autoclose"] = autoclose.get(bot.bot_id)
            out.append(entry)
        return out

    async def get_portfolio_total(self) -> float:
        """Retourne la valeur totale du portefeuille partage."""
        snap = await self._coinbase.get_portfolio_snapshot()
        return snap["total_usdc"]

    async def get_portfolio_snapshot(self) -> dict:
        """Snapshot pour le DASHBOARD uniquement — cache TTL court.

        Le dashboard poll /api/portfolio toutes les ~2s et chaque appel declenchait
        un fetch Coinbase complet (soldes + un prix par position), soit ~500 fetches
        par 10 min : de loin la premiere source d'appels API, et donc de 429.
        Les bots ne passent PAS par ici (ils appellent le client directement) :
        leurs decisions gardent des donnees fraiches.
        """
        now = time.monotonic()
        cached = self._dash_snap_cache
        if cached is not None and (now - cached[0]) < DASHBOARD_SNAPSHOT_TTL_S:
            return cached[1]
        snap = await self._coinbase.get_portfolio_snapshot()
        self._dash_snap_cache = (now, snap)
        return snap

    @property
    def coinbase(self) -> CoinbaseClient:
        return self._coinbase
