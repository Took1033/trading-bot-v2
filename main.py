"""
main.py - Point d'entree unifie de Kairos Alpha.

Lance en parallele dans la meme boucle asyncio :
  - Le Swarm de bots   (boucle de trading : signal -> risque -> ordre -> DB)
  - Le Director Agent  (kill switch, Fear & Greed)
  - Le Bot Telegram    (commandes utilisateur + notifications)
  - Le Daily Summary   (resume quotidien a heure fixe)
  - Le Dashboard web   (http://localhost:8080)

Usage :
    python main.py

Variables d'env requises (.env) :
    TELEGRAM_BOT_TOKEN   - token BotFather
    COINBASE_MODE        - "paper" (defaut) | "live"
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from logging_config import configure_logging
configure_logging()

import structlog

log               = structlog.get_logger()
TOKEN             = os.getenv("TELEGRAM_BOT_TOKEN", "")
MODE              = os.getenv("COINBASE_MODE", "paper")
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("true", "1", "yes")
DASHBOARD_PORT    = int(os.getenv("DASHBOARD_PORT", "8080"))


def _build_tasks(swarm, director) -> list:
    """Construit la liste des coroutines a lancer en parallele."""
    from agents.daily_summary import daily_summary_loop

    tasks = [
        swarm.run_all(),
        director.run_forever(),
        daily_summary_loop(),
    ]
    if DASHBOARD_ENABLED:
        from interfaces.dashboard import run_dashboard
        tasks.append(run_dashboard())
        log.info("dashboard_enabled", port=DASHBOARD_PORT)
    return tasks


async def main() -> None:
    from agents.bot_swarm      import BotSwarm
    from agents.director_agent import DirectorAgent
    from interfaces.notifier   import notify

    # ── Init swarm + director ────────────────────────────────────────────────
    swarm    = BotSwarm()
    director = DirectorAgent(swarm)

    # Expose pour le dashboard et Telegram
    sys.modules["__main__"].SWARM    = swarm
    sys.modules["__main__"].DIRECTOR = director

    # ── Mode headless (sans Telegram) ────────────────────────────────────────
    if not TOKEN:
        log.warning("telegram_disabled",
                    hint="Ajoute TELEGRAM_BOT_TOKEN dans .env pour activer Telegram")
        log.info("starting_headless", mode=MODE, n_bots=len(swarm.bots))
        await asyncio.gather(*_build_tasks(swarm, director))
        return

    # ── Mode complet avec Telegram ───────────────────────────────────────────
    from telegram import Update
    from interfaces.telegram_bot import build_app

    tg_app = build_app()
    log.info("bot_starting", mode=MODE, n_bots=len(swarm.bots),
             bots=[b.symbol for b in swarm.bots])

    async with tg_app:
        await tg_app.start()

        # Demarrer le polling — detecte conflit si autre instance en cours
        try:
            await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as exc:
            err = str(exc)
            if "conflict" in err.lower() or "Conflict" in type(exc).__name__:
                # Arreter proprement avant de quitter le context manager
                await tg_app.stop()
                log.error("telegram_conflict", error=err)
                print(
                    "\n"
                    "  ❌  CONFLIT TELEGRAM\n"
                    "  Une autre instance du bot tourne deja.\n\n"
                    "  Pour la fermer, ouvre un nouveau terminal et tape :\n\n"
                    "       taskkill /F /IM python.exe /T\n\n"
                    "  Puis relance : python main.py\n"
                )
                return
            # Autre erreur : on remonte
            await tg_app.stop()
            raise

        # Message de demarrage Telegram
        bot_list = "\n".join(
            f"  • `{b.bot_id.upper()}` ({b.symbol}, {b.weight:.0%})"
            for b in swarm.bots
        )
        await notify(
            f"🤖 *Kairos Alpha — Swarm démarré*\n"
            f"Mode : `{MODE.upper()}`\n"
            f"Bots actifs :\n{bot_list}\n"
            f"Director : Kill Switch + Fear & Greed activés\n"
            f"Dashboard : `http://localhost:{DASHBOARD_PORT}`\n\n"
            f"Commandes : /bots /kill /release /pause /stop"
        )

        # Lancer toutes les taches en parallele
        try:
            await asyncio.gather(*_build_tasks(swarm, director))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            log.info("bot_shutting_down")
            await notify("🛑 *Kairos Alpha arrêté.*")
            await tg_app.updater.stop()
            await tg_app.stop()


if __name__ == "__main__":
    log.info("main_start", pid=os.getpid(), python=sys.version.split()[0])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped_by_user")
