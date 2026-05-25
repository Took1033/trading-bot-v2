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
        "`/status`    — état complet (RSI, EMA, position, cooldown)\n"
        "`/positions` — positions ouvertes avec P&L live\n"
        "`/metrics`   — performances globales (WR, DD, etc.)\n"
        "`/decisions` — dernières décisions\n"
        "`/backtest`  — backtest 30j sur BTC-USD\n"
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
                "SELECT action, confidence, timestamp, metadata FROM decisions "
                "WHERE task_type='signal' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            last_order = conn.execute(
                "SELECT timestamp, action FROM decisions "
                "WHERE task_type='order' AND role='orchestrator' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except Exception:
        snap = None
        decision_count = 0
        last_signal = None
        last_order = None

    # État de la pause
    try:
        from agents import trading_state
        paused_txt = "⏸ *PAUSÉ*" if trading_state.is_paused() else "▶️ Actif"
    except ImportError:
        paused_txt = "—"

    lines = [
        f"*Trading Bot v2* — {now}",
        f"Mode : `{MODE.upper()}` | Statut : {paused_txt}",
        f"Décisions DB : `{decision_count}`",
    ]

    # Indicateurs techniques depuis le dernier signal
    if last_signal:
        conf = f"{last_signal['confidence']:.0%}" if last_signal["confidence"] else "—"
        meta_str = last_signal["metadata"] or ""
        # Extraire RSI et EMA depuis la metadata (format dict en str)
        import re
        rsi   = re.search(r"'rsi':\s*([\d.]+)",     meta_str)
        emaf  = re.search(r"'ema_fast':\s*([\d.]+)", meta_str)
        emas  = re.search(r"'ema_slow':\s*([\d.]+)", meta_str)
        lines.append(
            f"\n*Dernier signal* (`{last_signal['timestamp'][:16]}`)\n"
            f"  Action : `{last_signal['action']}` | Confiance : `{conf}`"
        )
        if rsi:
            lines.append(f"  RSI : `{rsi.group(1)}`")
        if emaf and emas:
            lines.append(f"  EMA9 : `{emaf.group(1)}` | EMA21 : `{emas.group(1)}`")

    # Cooldown
    if last_order:
        try:
            from datetime import datetime as dt
            ts = dt.fromisoformat(last_order["timestamp"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            cooldown = int(os.getenv("TRADE_COOLDOWN_S", "300"))
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                lines.append(f"\n⏳ Cooldown : `{remaining}s` restants ({last_order['action']})")
            else:
                lines.append(f"\nDernier ordre : `{last_order['action']}` il y a `{int(elapsed)}s`")
        except Exception:
            pass

    if snap:
        lines += [
            f"\n*Portefeuille*",
            f"  Total : `{snap['total_usdc']:.2f} USDC`",
            f"  P&L   : `{snap['pnl_pct']:+.2f}%`" if snap["pnl_pct"] is not None else "  P&L : —",
        ]
    else:
        lines.append("\n_Aucun snapshot — aucun trade exécuté._")

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


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les positions ouvertes avec P&L live."""
    try:
        with _db() as conn:
            snap = conn.execute(
                "SELECT positions, timestamp FROM portfolio_snapshots "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except Exception:
        snap = None

    if not snap:
        await update.message.reply_text(
            "_Aucune position — pas encore de trade exécuté._",
            parse_mode="Markdown",
        )
        return

    try:
        positions = json.loads(snap["positions"])
    except Exception:
        positions = {}

    if not positions:
        await update.message.reply_text(
            "_Aucune position ouverte actuellement._",
            parse_mode="Markdown",
        )
        return

    # Prix live pour P&L
    try:
        current_price = await _fetch_btc_price()
    except Exception:
        current_price = None

    lines = [f"*Positions ouvertes* (snapshot `{snap['timestamp'][:16]}`)"]
    for sym, pos in positions.items():
        qty   = pos["qty"]
        entry = pos["avg_price"]
        cost  = qty * entry

        if current_price and "BTC" in sym:
            value   = qty * current_price
            pnl     = value - cost
            pnl_pct = (current_price - entry) / entry * 100
            emoji   = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"\n{emoji} `{sym}`\n"
                f"  Quantité : `{qty:.6f}`\n"
                f"  Entrée   : `{entry:,.2f}` USDC\n"
                f"  Actuel   : `{current_price:,.2f}` USDC\n"
                f"  Valeur   : `{value:,.2f}` USDC\n"
                f"  P&L      : `{pnl:+,.2f}` USDC (`{pnl_pct:+.2f}%`)"
            )
        else:
            lines.append(
                f"\n`{sym}`\n"
                f"  Quantité : `{qty:.6f}`\n"
                f"  Entrée   : `{entry:,.2f}` USDC\n"
                f"  Coût     : `{cost:,.2f}` USDC"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les metriques de performance globales du bot."""
    try:
        with _db() as conn:
            # Tous les ordres executes
            buys = conn.execute(
                "SELECT timestamp, metadata FROM decisions "
                "WHERE role='orchestrator' AND task_type='order' AND action='buy' "
                "ORDER BY timestamp ASC"
            ).fetchall()
            sells = conn.execute(
                "SELECT timestamp, metadata FROM decisions "
                "WHERE role='orchestrator' AND task_type='order' AND action='sell' "
                "ORDER BY timestamp ASC"
            ).fetchall()

            # Snapshots pour le drawdown
            snaps = conn.execute(
                "SELECT total_usdc, timestamp FROM portfolio_snapshots "
                "ORDER BY timestamp ASC"
            ).fetchall()

            # Rejets et alertes
            rejected_count = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE action='rejected'"
            ).fetchone()[0]
            alerts_count = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE task_type='alert'"
            ).fetchone()[0]
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur DB : `{exc}`", parse_mode="Markdown")
        return

    if not snaps:
        await update.message.reply_text(
            "_Aucun trade enregistré — pas encore de métriques disponibles._",
            parse_mode="Markdown",
        )
        return

    # Pairing buy/sell pour P&L par trade
    import re
    def _extract_price(meta_str: str) -> float | None:
        m = re.search(r'"price":\s*([\d.]+)', meta_str or "")
        return float(m.group(1)) if m else None

    def _extract_qty(meta_str: str) -> float | None:
        m = re.search(r'"qty":\s*([\d.]+)', meta_str or "")
        return float(m.group(1)) if m else None

    trades_pnl: list[float] = []
    n = min(len(buys), len(sells))
    for i in range(n):
        bp, bq = _extract_price(buys[i]["metadata"]),  _extract_qty(buys[i]["metadata"])
        sp, sq = _extract_price(sells[i]["metadata"]), _extract_qty(sells[i]["metadata"])
        if bp and sp and bq:
            trades_pnl.append(sp - bp)   # P&L par unite

    n_trades  = len(trades_pnl)
    n_wins    = sum(1 for p in trades_pnl if p > 0)
    win_rate  = (n_wins / n_trades * 100) if n_trades else 0.0
    avg_pnl   = (sum(trades_pnl) / n_trades) if n_trades else 0.0

    # Drawdown max
    peak = snaps[0]["total_usdc"]
    max_dd = 0.0
    for s in snaps:
        v = s["total_usdc"]
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd

    # P&L total
    initial = snaps[0]["total_usdc"]
    current = snaps[-1]["total_usdc"]
    total_pct = (current - initial) / initial * 100 if initial > 0 else 0.0
    total_abs = current - initial

    # Duree
    from datetime import datetime as dt
    try:
        first_ts = dt.fromisoformat(snaps[0]["timestamp"].replace("Z", "+00:00"))
        days     = (datetime.now(timezone.utc) - first_ts).total_seconds() / 86400
    except Exception:
        days = 0

    emoji = "🟢" if total_pct >= 0 else "🔴"
    msg = (
        f"📊 *Métriques globales*\n\n"
        f"*Performance*\n"
        f"  Capital initial : `{initial:,.2f}` USDC\n"
        f"  Capital actuel  : `{current:,.2f}` USDC\n"
        f"  P&L total       : {emoji} `{total_abs:+,.2f}` USDC (`{total_pct:+.2f}%`)\n"
        f"  Max drawdown    : `{max_dd:.2f}%`\n"
        f"  Durée trading   : `{days:.1f}` jours\n\n"
        f"*Trades*\n"
        f"  Trades fermés   : `{n_trades}`\n"
        f"  Gagnants        : `{n_wins}` (WR = `{win_rate:.1f}%`)\n"
        f"  P&L moyen/trade : `{avg_pnl:+,.2f}` USDC/unité\n\n"
        f"*Filtres*\n"
        f"  Ordres rejetés  : `{rejected_count}`\n"
        f"  Alertes erreurs : `{alerts_count}`"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lance un backtest 30 jours sur BTC-USD."""
    await update.message.reply_text(
        "⏳ Lancement du backtest 30j sur BTC-USD…",
        parse_mode="Markdown",
    )

    try:
        from strategies.backtester import Backtester, fetch_prices_coinbase
        from strategies.simple_ma import analyze

        prices = await fetch_prices_coinbase("BTC-USD", granularity=86400, limit=30)
        bt     = Backtester(strategy=analyze, initial_usdc=10_000.0)
        result = await bt.run("BTC-USD", prices)

        emoji = "🟢" if result.total_return >= 0 else "🔴"
        msg = (
            f"{emoji} *Backtest BTC-USD (30j)*\n\n"
            f"Capital initial : `{result.initial_usdc:,.2f}` USDC\n"
            f"Capital final   : `{result.final_usdc:,.2f}` USDC\n"
            f"Rendement       : `{result.total_return:+.2f}%`\n"
            f"Trades fermés   : `{result.n_trades}` "
            f"({result.n_wins} gagnants, WR=`{result.win_rate:.1f}%`)\n"
            f"Max drawdown    : `{result.max_drawdown:.2f}%`\n\n"
            f"_Stratégie : EMA 9/21 + RSI filter_"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as exc:
        log.error("backtest_failed", error=str(exc))
        await update.message.reply_text(
            f"❌ Erreur backtest : `{exc}`",
            parse_mode="Markdown",
        )


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
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("backtest",  cmd_backtest))
    app.add_handler(CommandHandler("metrics",   cmd_metrics))
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
