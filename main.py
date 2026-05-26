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

Variables optionnelles :
    TELEGRAM_CHAT_ID         - notifications (ou /register dans le bot)
    TRADING_SYMBOL           - symbole (defaut: BTC-USDC)
    LOOP_INTERVAL_S          - intervalle de trading (defaut: 60)
    DAILY_SUMMARY_HOUR_UTC   - heure UTC du resume quotidien (defaut: 9)
    DASHBOARD_PORT           - port du dashboard (defaut: 8080)
    DASHBOARD_ENABLED        - activer le dashboard (defaut: true)
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

log               = structlog.get_logger()
TOKEN             = os.getenv("TELEGRAM_BOT_TOKEN", "")
MODE              = os.getenv("COINBASE_MODE", "paper")
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("true", "1", "yes")
DASHBOARD_PORT    = int(os.getenv("DASHBOARD_PORT", "8080"))


async def main() -> None:
    from agents.bot_swarm      import BotSwarm
    from agents.director_agent import DirectorAgent
    from agents.daily_summary  import daily_summary_loop
    from interfaces.notifier   import notify

    # ── Initialisation du swarm + director ──────────────────────────────────
    swarm    = BotSwarm()
    director = DirectorAgent(swarm)

    # Exposer globalement pour le dashboard et Telegram
    sys.modules["__main__"].SWARM    = swarm
    sys.modules["__main__"].DIRECTOR = director

    # ── Sans token Telegram : mode headless ──────────────────────────────────
    if not TOKEN:
        log.warning("telegram_disabled",
                    hint="Ajouter TELEGRAM_BOT_TOKEN dans .env pour activer Telegram")
        log.info("starting_headless", mode=MODE, n_bots=len(swarm.bots))
        tasks = [swarm.run_all(), director.run_forever()]
        if DASHBOARD_ENABLED:
            from interfaces.dashboard import run_dashboard
            tasks.append(run_dashboard())
        await asyncio.gather(*tasks)
        return

    # ── Avec Telegram : mode complet ─────────────────────────────────────────
    from telegram import Update
    from telegram.error import Conflict as TelegramConflict
    from interfaces.telegram_bot import build_app

    tg_app = build_app()
    log.info("bot_starting", mode=MODE, n_bots=len(swarm.bots),
             bots=[b.symbol for b in swarm.bots])

    # ── Detect conflit Telegram (autre instance en cours) ────────────────────
    try:
        async with tg_app:
            await tg_app.start()
            await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    except TelegramConflict:
        log.error(
            "telegram_conflict",
            hint="Une autre instance du bot tourne deja. "
                 "Fermez-la avec : taskkill /F /IM python.exe /T",
        )
        print(
            "\n"
            "  ❌  CONFLIT TELEGRAM — une autre instance du bot est deja en cours.\n"
            "  Pour la fermer, ouvrez un nouveau terminal et tapez :\n\n"
            "       taskkill /F /IM python.exe /T\n\n"
            "  Puis relancez : python main.py\n"
        )
        return

    # ── Dashboard + taches en parallele ─────────────────────────────────────
    bot_list = "\n".join(
        f"  • `{b.bot_id.upper()}` ({b.symbol}, poids {b.weight:.0%})"
        for b in swarm.bots
    )

    tasks = [
        swarm.run_all(),
        director.run_forever(),
        daily_summary_loop(),
    ]

    if DASHBOARD_ENABLED:
        from interfaces.dashboard import run_dashboard
        # Verifier si le port est libre avant de lancer
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port_free = sock.connect_ex(("127.0.0.1", DASHBOARD_PORT)) != 0
        if port_free:
            tasks.append(run_dashboard())
            log.info("dashboard_enabled", port=DASHBOARD_PORT)
        else:
            log.warning("dashboard_port_busy",
                        port=DASHBOARD_PORT,
                        hint=f"Port {DASHBOARD_PORT} deja utilise - dashboard desactive")

    await notify(
        f"🤖 *Kairos Alpha — Swarm démarré*\n"
        f"Mode : `{MODE.upper()}`\n"
        f"Bots actifs :\n{bot_list}\n"
        f"Director Agent : Kill Switch + Fear & Greed activés\n"
        f"Dashboard : `http://localhost:{DASHBOARD_PORT}`\n\n"
        f"Commandes : /bots /kill /release /pause /stop"
    )

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        pass
    finally:
        log.info("bot_shutting_down")
        await notify("🛑 *Kairos Alpha arrêté.*")
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
        except Exception:
            pass


if __name__ == "__main__":
    log.info("main_start", pid=os.getpid(), python=sys.version.split()[0])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped_by_user")
