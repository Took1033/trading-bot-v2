"""
main.py - Point d'entree unifie du Kairos Alpha.

Lance en parallele dans la meme boucle asyncio :
  - L'Orchestrateur     (boucle de trading : signal -> risque -> ordre -> DB)
  - Le Bot Telegram     (commandes utilisateur + notifications)
  - Le Daily Summary    (resume quotidien a heure fixe)

Usage :
    python main.py

Variables d'env requises (.env) :
    TELEGRAM_BOT_TOKEN   - token BotFather
    COINBASE_MODE        - "paper" (defaut) | "live"

Variables optionnelles :
    TELEGRAM_CHAT_ID         - notifications (ou /register dans le bot)
    TRADING_SYMBOL           - symbole (defaut: BTC-USDC)
    LOOP_INTERVAL_S          - intervalle de trading (defaut: 60)
    DAILY_SUMMARY_HOUR_UTC   - heure UTC du resume quotidien (defaut: 9)
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Configurer le logging AVANT tout autre import qui logge
from logging_config import configure_logging
configure_logging()

import structlog

log   = structlog.get_logger()
TOKEN             = os.getenv("TELEGRAM_BOT_TOKEN", "")
MODE              = os.getenv("COINBASE_MODE", "paper")
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("true", "1", "yes")


async def main() -> None:
    from agents.bot_swarm       import BotSwarm
    from agents.director_agent  import DirectorAgent
    from agents.daily_summary   import daily_summary_loop
    from interfaces.notifier    import notify

    # ── Initialisation du swarm + director ──────────────────────────────────
    swarm    = BotSwarm()
    director = DirectorAgent(swarm)

    # Exposer globalement pour le dashboard et Telegram
    import sys
    sys.modules["__main__"].SWARM    = swarm
    sys.modules["__main__"].DIRECTOR = director

    # ── Sans token Telegram : mode headless ──────────────────────────────────
    if not TOKEN:
        log.warning("telegram_disabled",
                    hint="Ajouter TELEGRAM_BOT_TOKEN dans .env pour activer le bot Telegram")
        log.info("starting_headless", mode=MODE, n_bots=len(swarm.bots))
        await asyncio.gather(
            swarm.run_all(),
            director.run_forever(),
        )
        return

    # ── Avec Telegram : mode complet ─────────────────────────────────────────
    from telegram import Update
    from interfaces.telegram_bot import build_app

    tg_app = build_app()
    log.info("bot_starting", mode=MODE, n_bots=len(swarm.bots),
             bots=[b.symbol for b in swarm.bots])

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        bot_list = "\n".join(
            f"  • `{b.bot_id.upper()}` ({b.symbol}, poids {b.weight:.0%})"
            for b in swarm.bots
        )
        await notify(
            f"🤖 *Kairos Alpha — Swarm démarré*\n"
            f"Mode : `{MODE.upper()}`\n"
            f"Bots actifs :\n{bot_list}\n"
            f"Director Agent : Kill Switch + Fear & Greed activés\n"
            f"Dashboard : `http://localhost:{os.getenv('DASHBOARD_PORT', '8080')}`\n\n"
            f"Commandes swarm : /bots /kill /release /pause /stop"
        )

        # Lancer swarm + director + daily summary + dashboard en parallele
        tasks = [
            swarm.run_all(),
            director.run_forever(),
            daily_summary_loop(),
        ]
        if DASHBOARD_ENABLED:
            from interfaces.dashboard import run_dashboard
            tasks.append(run_dashboard())
            log.info("dashboard_enabled", port=int(os.getenv("DASHBOARD_PORT", "8080")))

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
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
