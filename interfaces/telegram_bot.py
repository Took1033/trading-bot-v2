"""
Bot Telegram — interface utilisateur du trading bot.

Commandes disponibles :
  /start    — message de bienvenue + liste des commandes
  /register — enregistre ce chat pour les notifications automatiques
  /status   — état du bot et dernier snapshot DB
  /decisions [N] — N dernières décisions (défaut: 5)
  /price    — prix actuel BTC-USDC (API publique)
  /pnl      — P&L du dernier snapshot
  /mode     — mode actuel (paper/live)
  /stop     — met le trading en pause
  /resume   — reprend le trading

Deux modes de démarrage :
  - Standalone  : python interfaces/telegram_bot.py  (main())
  - Intégré     : from interfaces.telegram_bot import build_app  (main.py)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import structlog
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
log = structlog.get_logger()

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "memory/trading.db")
MODE    = os.getenv("COINBASE_MODE", "paper")
_CONFIG = Path(DB_PATH).parent / "telegram_config.json"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def _fetch_btc_price() -> float:
    """Prix BTC-USDC via API publique Coinbase (sans auth)."""
    import aiohttp
    url = "https://api.coinbase.com/v2/prices/BTC-USDC/spot"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
            return float(data["data"]["amount"])


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Trading Bot v2*\n\n"
        "Commandes disponibles :\n"
        "`/register`  — activer les notifications\n"
        "`/status`    — état du bot\n"
        "`/decisions` — dernières décisions\n"
        "`/price`     — prix actuel BTC-USDC\n"
        "`/pnl`       — P&L du portefeuille\n"
        "`/mode`      — mode paper/live\n"
        "`/stop`      — mettre en pause\n"
        "`/resume`    — reprendre le trading",
        parse_mode="Markdown",
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enregistre ce chat pour recevoir les notifications automatiques."""
    chat_id = str(update.effective_chat.id)

    # Sauvegarder dans le fichier de config (lu par le notifier)
    _CONFIG.write_text(
        json.dumps({"chat_id": chat_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    # Mettre à jour l'env runtime (pour la session courante)
    os.environ["TELEGRAM_CHAT_ID"] = chat_id

    log.info("chat_registered", chat_id=chat_id)
    await update.message.reply_text(
        f"✅ *Chat enregistré !*\n"
        f"Chat ID : `{chat_id}`\n\n"
        f"Tu recevras désormais les notifications automatiques "
        f"(signaux, ordres, alertes).",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        with _db() as conn:
            snap = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            decision_count = conn.execute(
                "SELECT COUNT(*) FROM decisions"
            ).fetchone()[0]
            last_signal = conn.execute(
                "SELECT action, confidence, timestamp FROM decisions "
                "WHERE task_type='signal' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except Exception:
        snap = None
        decision_count = 0
        last_signal = None

    # État de la pause
    try:
        from agents import trading_state
        paused_txt = "⏸ *PAUSÉ*" if trading_state.is_paused() else "▶️ Actif"
    except ImportError:
        paused_txt = "—"

    lines = [
        f"*Trading Bot v2* — {now}",
        f"Mode : `{MODE.upper()}` | Statut : {paused_txt}",
        f"Décisions enregistrées : `{decision_count}`",
    ]

    if last_signal:
        conf = f"{last_signal['confidence']:.0%}" if last_signal["confidence"] else "—"
        lines.append(
            f"Dernier signal : `{last_signal['action']}` ({conf}) "
            f"à `{last_signal['timestamp'][:16]}`"
        )

    if snap:
        lines += [
            f"\n*Dernier snapshot* : `{snap['timestamp'][:16]}`",
            f"Portefeuille : `{snap['total_usdc']:.2f} USDC`",
            f"P&L : `{snap['pnl_pct']:+.2f}%`" if snap["pnl_pct"] else "P&L : —",
        ]
    else:
        lines.append("\n_Aucun snapshot de portefeuille — aucun ordre exécuté._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_decisions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 5
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), 20))
        except ValueError:
            pass

    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT timestamp, role, task_type, symbol, action, confidence, reasoning "
                "FROM decisions ORDER BY timestamp DESC LIMIT ?",
                (n,),
            ).fetchall()
    except Exception:
        rows = []

    if not rows:
        await update.message.reply_text("Aucune décision enregistrée.")
        return

    lines = [f"*{n} dernières décisions :*"]
    for r in rows:
        conf   = f"{r['confidence']:.0%}" if r["confidence"] is not None else "—"
        action = r["action"] or "—"
        lines.append(
            f"\n`{r['timestamp'][:16]}` [{r['role']}]\n"
            f"  {r['task_type']} | {r['symbol'] or '—'} | {action} | {conf}\n"
            f"  _{r['reasoning'][:80]}_"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche le prix actuel BTC-USDC."""
    try:
        price = await _fetch_btc_price()
        await update.message.reply_text(
            f"💰 *BTC-USDC* : `{price:,.2f} USDC`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur : `{exc}`", parse_mode="Markdown")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche le P&L depuis le dernier snapshot."""
    try:
        with _db() as conn:
            snap = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            first_snap = conn.execute(
                "SELECT total_usdc FROM portfolio_snapshots ORDER BY timestamp ASC LIMIT 1"
            ).fetchone()
    except Exception:
        snap = None
        first_snap = None

    if not snap:
        await update.message.reply_text(
            "_Aucun snapshot disponible — aucun ordre paper exécuté._",
            parse_mode="Markdown",
        )
        return

    initial = first_snap["total_usdc"] if first_snap else 10_000.0
    current = snap["total_usdc"]
    pnl_usdc = current - initial
    pnl_pct  = (pnl_usdc / initial) * 100
    emoji    = "📈" if pnl_usdc >= 0 else "📉"

    # Essayer d'obtenir le prix live pour valoriser les positions
    positions = {}
    try:
        positions = json.loads(snap["positions"])
    except Exception:
        pass

    lines = [
        f"{emoji} *P&L Paper Trading*",
        f"Capital initial : `{initial:,.2f} USDC`",
        f"Capital actuel  : `{current:,.2f} USDC`",
        f"P&L absolu      : `{pnl_usdc:+,.2f} USDC`",
        f"P&L relatif     : `{pnl_pct:+.2f}%`",
        f"_Snapshot : {snap['timestamp'][:16]}_",
    ]

    if positions:
        lines.append("\n*Positions ouvertes :*")
        for sym, pos in positions.items():
            lines.append(
                f"  `{sym}` : `{pos['qty']:.6f}` @ `{pos['avg_price']:,.2f}`"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    emoji = "🟡" if MODE == "paper" else "🔴"
    await update.message.reply_text(
        f"{emoji} Mode actuel : *{MODE.upper()}*",
        parse_mode="Markdown",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Met le trading en pause."""
    try:
        from agents import trading_state
        trading_state.pause()
        await update.message.reply_text(
            "⏸ *Trading mis en pause.*\n"
            "Aucun ordre ne sera passé. Tape `/resume` pour reprendre.",
            parse_mode="Markdown",
        )
        log.info("trading_paused_by_user")
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur : `{exc}`", parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reprend le trading après une pause."""
    try:
        from agents import trading_state
        trading_state.resume()
        await update.message.reply_text(
            "▶️ *Trading repris.*",
            parse_mode="Markdown",
        )
        log.info("trading_resumed_by_user")
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur : `{exc}`", parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────────────────────
# Construction de l'application
# ──────────────────────────────────────────────────────────────────────────────

def build_app() -> Application:
    """
    Construit et retourne l'Application Telegram sans la démarrer.
    Utilisé par main.py pour intégrer le bot dans la boucle asyncio principale.
    """
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans .env")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("register",  cmd_register))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("decisions", cmd_decisions))
    app.add_handler(CommandHandler("price",     cmd_price))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("mode",      cmd_mode))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    return app


# ──────────────────────────────────────────────────────────────────────────────
# Point d'entrée standalone
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Démarre le bot seul (sans orchestrateur)."""
    app = build_app()
    log.info("bot_started", mode=MODE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
