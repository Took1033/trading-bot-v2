"""
main.py — Point d'entrée unifié du Trading Bot v2.

Lance en parallèle dans la même boucle asyncio :
  - L'Orchestrateur   (boucle de trading : signal → risque → ordre → DB)
  - Le Bot Telegram   (commandes utilisateur + notifications)

Usage :
    python main.py

Variables d'env requises (.env) :
    TELEGRAM_BOT_TOKEN   — token BotFather
    COINBASE_MODE        — "paper" (défaut) | "live"

Variables optionnelles :
    TELEGRAM_CHAT_ID     — pour les notifications (ou utilise /register dans le bot)
    TRADING_SYMBOL       — symbole (défaut: BTC-USDC)
    LOOP_INTERVAL_S      — intervalle de trading en secondes (défaut: 60)
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MODE  = os.getenv("COINBASE_MODE", "paper")


async def main() -> None:
    from agents.orchestrator import Orchestrator
    from interfaces.notifier import notify

    orc = Orchestrator()

    # ── Sans token Telegram : mode headless ──────────────────────────────────
    if not TOKEN:
        log.warning(
            "telegram_disabled",
            hint="Ajouter TELEGRAM_BOT_TOKEN dans .env pour activer le bot Telegram",
        )
        log.info("starting_headless", mode=MODE)
        await orc.run_forever()
        return

    # ── Avec Telegram : mode complet ─────────────────────────────────────────
    from telegram import Update
    from interfaces.telegram_bot import build_app

    tg_app = build_app()
    log.info("bot_starting", mode=MODE, symbol=os.getenv("TRADING_SYMBOL", "BTC-USDC"))

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Message de démarrage
        await notify(
            f"🤖 *Trading Bot v2 démarré*\n"
            f"Mode : `{MODE.upper()}`\n"
            f"Symbole : `{os.getenv('TRADING_SYMBOL', 'BTC-USDC')}`\n"
            f"Intervalle : `{os.getenv('LOOP_INTERVAL_S', '60')}s`\n\n"
            f"Tape /status pour l'état ou /register si tu ne reçois pas ce message."
        )

        try:
            await orc.run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            log.info("bot_shutting_down")
            await notify("🛑 *Bot arrêté.*")
            await tg_app.updater.stop()
            await tg_app.stop()


if __name__ == "__main__":
    log.info("main_start", pid=os.getpid(), python=sys.version.split()[0])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped_by_user")
