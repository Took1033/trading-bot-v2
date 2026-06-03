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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

load_dotenv()
log = structlog.get_logger()

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "memory/trading.db")
MODE    = os.getenv("COINBASE_MODE", "paper")
# Capital live initial + seuil paper/live (cf. dashboard) : en live, le P&L est
# ancre sur LIVE_INITIAL_USDC et on ignore les snapshots paper residuels (~10000).
LIVE_INITIAL_USDC = float(os.getenv("LIVE_INITIAL_USDC", "0") or "0")
PAPER_LIVE_SPLIT  = float(os.getenv("PAPER_LIVE_SPLIT_USDC", "1000"))
# Frais round-trip Coinbase (taker x2) — P&L affiche net de frais.
ROUND_TRIP_FEE_PCT = 2 * float(os.getenv("COINBASE_TAKER_FEE_PCT", "0.0075"))
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
        "🤖 *Kairos Alpha — Swarm*\n\n"
        "*Swarm & Contrôle*\n"
        "`/bots`          — état de tous les bots\n"
        "`/kill [raison]` — kill switch global (pause tous)\n"
        "`/release`       — relâcher le kill switch\n"
        "`/pause btc|eth|sol|dyn|all` — pause individuelle\n"
        "`/resume [bot|all]`          — reprendre\n\n"
        "*Configuration des paires :*\n"
        "`/setpair <bot> <SYMBOLE>`   — changer la paire d'un bot\n"
        "`/addbot <id> <SYMBOLE> [poids]` — ajouter un bot\n"
        "`/removebot <id>`            — retirer un bot\n\n"
        "*Infos*\n"
        "`/register`  — activer les notifications\n"
        "`/status`    — état complet (RSI, EMA, position)\n"
        "`/positions` — positions ouvertes avec P&L live\n"
        "`/metrics`   — performances globales (WR, DD…)\n"
        "`/decisions` — dernières décisions\n"
        "`/backtest`  — backtest 30j BTC-USD\n"
        "`/price`     — prix actuel BTC-USDC\n"
        "`/pnl`       — P&L du portefeuille\n"
        "`/mode`      — mode paper/live",
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
        f"*Kairos Alpha* — {now}",
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
            # En live, ignore les snapshots paper residuels (~10000) pour le capital initial.
            if MODE == "live":
                first_snap = conn.execute(
                    "SELECT total_usdc FROM portfolio_snapshots "
                    "WHERE total_usdc < ? ORDER BY timestamp ASC LIMIT 1",
                    (PAPER_LIVE_SPLIT,),
                ).fetchone()
            else:
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

    # En live, le capital initial canonique est LIVE_INITIAL_USDC (fallback snapshot filtre).
    if MODE == "live" and LIVE_INITIAL_USDC > 0:
        initial = LIVE_INITIAL_USDC
    else:
        initial = first_snap["total_usdc"] if first_snap else 10_000.0
    current = snap["total_usdc"]
    pnl_usdc = current - initial
    pnl_pct  = (pnl_usdc / initial) * 100 if initial else 0.0
    emoji    = "📈" if pnl_usdc >= 0 else "📉"

    # Essayer d'obtenir le prix live pour valoriser les positions
    positions = {}
    try:
        positions = json.loads(snap["positions"])
    except Exception:
        pass

    lines = [
        f"{emoji} *P&L {'Live' if MODE == 'live' else 'Paper'} Trading*",
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
    """Kill switch global : met TOUS les bots en pause."""
    try:
        from agents import trading_state
        reason = "Pause manuelle via /stop"
        trading_state.kill_switch(reason)
        await update.message.reply_text(
            "⏸ *Kill switch activé — tous les bots en pause.*\n"
            "Tape `/resume` ou `/release` pour reprendre.",
            parse_mode="Markdown",
        )
        log.info("kill_switch_manual_stop", user=update.effective_user.username)
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur : `{exc}`", parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Relâche le kill switch / reprend tous les bots."""
    try:
        from agents import trading_state
        if context.args:
            # /resume btc|eth|sol|dyn|all → pause individuelle
            await _resume_bot(update, context)
            return
        trading_state.release_kill_switch()
        await update.message.reply_text(
            "▶️ *Kill switch levé — tous les bots reprennent.*",
            parse_mode="Markdown",
        )
        log.info("kill_switch_released_by_user", user=update.effective_user.username)
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
            pnl     = value - cost - ROUND_TRIP_FEE_PCT * cost
            pnl_pct = ((current_price - entry) / entry - ROUND_TRIP_FEE_PCT) * 100
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
# Commandes Swarm
# ──────────────────────────────────────────────────────────────────────────────

def _get_swarm():
    import sys
    return getattr(sys.modules.get("__main__"), "SWARM", None)


async def cmd_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche l'état de tous les bots du swarm."""
    swarm = _get_swarm()
    if not swarm:
        await update.message.reply_text(
            "_Swarm non disponible — bot en mode standalone._",
            parse_mode="Markdown",
        )
        return

    from agents import trading_state
    ks_active = trading_state.is_kill_switch_active()
    lines = [f"*Swarm — {len(swarm.bots)} bots* {'🚨 KILL SWITCH' if ks_active else ''}"]

    for b in swarm.get_status():
        paused   = b["paused"]
        warmed   = b["warmed_up"]
        pos      = b.get("position") or {}
        has_pos  = pos.get("qty", 0) > 0

        if ks_active:       status = "🚨 KILL"
        elif paused:        status = "⏸ pausé"
        elif not warmed:    status = f"🔄 chauffe {b['history_len']}/51"
        else:               status = "▶️ actif"

        pos_txt = ""
        if has_pos:
            pos_txt = f"\n    📦 `{pos['qty']:.5f}` @ `{pos['avg_price']:,.2f}`"

        # Performances dynamiques
        dyn_txt = ""
        if b.get("dynamic_perfs"):
            parts = " | ".join(
                f"{s.split('-')[0]} `{v:+.1f}%`"
                for s, v in sorted(b["dynamic_perfs"].items(), key=lambda x: -x[1])
            )
            dyn_txt = f"\n    24h : {parts}"

        lines.append(
            f"\n*{b['name']}* `{b['symbol']}` ({b['weight']:.0%})\n"
            f"   {status}{pos_txt}{dyn_txt}"
        )

    if ks_active:
        reason = trading_state.get_kill_reason() or "—"
        lines.append(f"\n🚨 Raison : _{reason}_")

    # Boutons inline : pause/resume rapide + kill switch
    bot_ids = [b["bot_id"] if "bot_id" in b else b["name"].lower()
               for b in swarm.get_status()]
    rows: list[list[InlineKeyboardButton]] = []
    for bid in bot_ids[:4]:
        paused = trading_state.is_paused(bid)
        if paused:
            rows.append([
                InlineKeyboardButton(f"▶️ Reprendre {bid.upper()}", callback_data=f"resume:{bid}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"⏸ Pause {bid.upper()}", callback_data=f"pause:{bid}"),
            ])
    if ks_active:
        rows.append([InlineKeyboardButton("🟢 Relâcher KILL", callback_data="release_kill")])
    else:
        rows.append([InlineKeyboardButton("🛑 KILL SWITCH global", callback_data="trigger_kill")])
    rows.append([InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh_bots")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle les clics sur les boutons inline."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    from agents import trading_state

    if data == "refresh_bots":
        # Re-trigger cmd_bots avec une fake update
        msg = update.callback_query.message
        if msg:
            try:
                await msg.delete()
            except Exception:
                pass
        # Renvoyer le panneau a jour
        fake = Update(update.update_id, message=msg)
        await cmd_bots(fake, context)
        return

    if data == "trigger_kill":
        trading_state.kill_switch("Kill manuel via bouton Telegram")
        await query.message.reply_text("🛑 *Kill switch ACTIVE*", parse_mode="Markdown")
        return

    if data == "release_kill":
        trading_state.release_kill_switch()
        await query.message.reply_text("🟢 *Kill switch RELACHE*", parse_mode="Markdown")
        return

    if data.startswith("pause:"):
        bid = data.split(":", 1)[1]
        trading_state.pause(bid)
        await query.message.reply_text(f"⏸ Bot `{bid.upper()}` pausé.", parse_mode="Markdown")
        return

    if data.startswith("resume:"):
        bid = data.split(":", 1)[1]
        trading_state.resume(bid)
        await query.message.reply_text(f"▶️ Bot `{bid.upper()}` repris.", parse_mode="Markdown")
        return


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Active le kill switch manuellement avec raison optionnelle."""
    from agents import trading_state
    reason = " ".join(context.args) if context.args else "Arrêt manuel via /kill"
    trading_state.kill_switch(reason)
    log.info("kill_switch_manual_kill",
             user=update.effective_user.username, reason=reason)
    await update.message.reply_text(
        f"🚨 *Kill switch activé*\n"
        f"Raison : _{reason}_\n"
        f"Tous les bots sont en *PAUSE*.\n"
        f"Tape `/release` pour reprendre.",
        parse_mode="Markdown",
    )


async def cmd_release(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Relâche le kill switch."""
    from agents import trading_state
    if not trading_state.is_kill_switch_active():
        await update.message.reply_text(
            "ℹ️ Kill switch déjà inactif.", parse_mode="Markdown"
        )
        return
    trading_state.release_kill_switch()
    log.info("kill_switch_manual_release", user=update.effective_user.username)
    await update.message.reply_text(
        "✅ *Kill switch levé.*\nTrading repris sur tous les bots.",
        parse_mode="Markdown",
    )


_BOT_ALIASES = {
    "btc": "btc", "eth": "eth", "sol": "sol",
    "dyn": "dynamique", "dynamique": "dynamique", "all": "all",
}


async def cmd_pause_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause btc|eth|sol|dyn|all — pause individuelle ou globale."""
    from agents import trading_state
    swarm  = _get_swarm()
    target = (context.args[0].lower() if context.args else "all")
    bot_id = _BOT_ALIASES.get(target)

    if bot_id is None:
        await update.message.reply_text(
            "Usage : `/pause btc|eth|sol|dyn|all`", parse_mode="Markdown"
        )
        return

    if bot_id == "all":
        if swarm:
            for b in swarm.bots:
                trading_state.pause(b.bot_id)
        await update.message.reply_text(
            "⏸ *Tous les bots mis en pause.*", parse_mode="Markdown"
        )
    else:
        trading_state.pause(bot_id)
        await update.message.reply_text(
            f"⏸ Bot `{bot_id}` mis en *pause*.", parse_mode="Markdown"
        )
    log.info("bot_paused_by_user", target=target, user=update.effective_user.username)


async def _resume_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logique de resume d'un bot specifique (appelee par cmd_resume aussi)."""
    from agents import trading_state
    swarm  = _get_swarm()
    target = (context.args[0].lower() if context.args else "all")
    bot_id = _BOT_ALIASES.get(target)

    if bot_id is None:
        await update.message.reply_text(
            "Usage : `/resume btc|eth|sol|dyn|all`", parse_mode="Markdown"
        )
        return

    if bot_id == "all":
        if swarm:
            for b in swarm.bots:
                trading_state.resume(b.bot_id)
        await update.message.reply_text(
            "▶️ *Tous les bots relancés.*", parse_mode="Markdown"
        )
    else:
        trading_state.resume(bot_id)
        await update.message.reply_text(
            f"▶️ Bot `{bot_id}` *repris*.", parse_mode="Markdown"
        )
    log.info("bot_resumed_by_user", target=target, user=update.effective_user.username)


async def cmd_tune(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Lance manuellement le param tuner Opus."""
    await update.message.reply_text(
        "🧠 _Opus analyse les 30 derniers jours..._ (peut prendre 30-60s)",
        parse_mode="Markdown",
    )
    try:
        from agents.param_tuner import run_tuning_once
        sent = await run_tuning_once()
        if not sent:
            await update.message.reply_text(
                "⚠️ Aucune proposition générée (trop peu de trades ou Anthropic indisponible).",
                parse_mode="Markdown",
            )
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur : `{exc}`", parse_mode="Markdown")


async def cmd_ask(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pose une question a Claude sur le bot. Usage : /ask <question>"""
    if not update.message or not update.message.text:
        return

    question = update.message.text.replace("/ask", "", 1).strip()
    if not question:
        await update.message.reply_text(
            "Usage : `/ask <question>`\n\n"
            "Exemples :\n"
            "  `/ask pourquoi le bot n'a rien acheté aujourd'hui ?`\n"
            "  `/ask quelle est ma performance ?`\n"
            "  `/ask explique-moi le kill switch`",
            parse_mode="Markdown",
        )
        return

    from interfaces.claude_client import answer_user_question, ENABLED
    if not ENABLED:
        await update.message.reply_text(
            "⚠️ ANTHROPIC_API_KEY non configurée dans `.env` — `/ask` indisponible.",
            parse_mode="Markdown",
        )
        return

    # Construction du contexte courant pour Claude
    await update.message.reply_text("🧠 _Claude réfléchit..._", parse_mode="Markdown")

    swarm    = _resolve_swarm()
    director = _resolve_director()
    ctx: dict = {}
    try:
        if swarm:
            statuses = swarm.get_status()
            ctx["mode"]          = statuses.get("mode")
            ctx["n_bots"]        = len(statuses.get("bots", []))
            ctx["total_value"]   = statuses.get("total_value")
            ctx["pnl_pct"]       = statuses.get("pnl_pct")
            ctx["kill_switch"]   = statuses.get("kill_switch")
        if director:
            ctx["fear_greed"]       = getattr(director, "_fg_value", None)
            ctx["fear_greed_label"] = getattr(director, "_fg_label", None)
    except Exception:
        pass

    answer = await answer_user_question(question, ctx)
    if not answer:
        await update.message.reply_text(
            "❌ Pas de réponse (timeout ou erreur API).",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"🧠 *Réponse Claude :*\n\n{answer}",
        parse_mode="Markdown",
    )
    log.info("ask_answered", user=update.effective_user.username,
             question=question[:80], answer_chars=len(answer))


async def cmd_setpair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setpair <bot> <SYMBOL> — change la paire d'un bot à chaud."""
    swarm = _get_swarm()
    if not swarm:
        await update.message.reply_text("_Swarm non disponible._", parse_mode="Markdown")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : `/setpair <bot> <SYMBOLE>`\nEx : `/setpair eth LINK-USDC`",
            parse_mode="Markdown",
        )
        return
    bot_id  = _BOT_ALIASES.get(context.args[0].lower(), context.args[0].lower())
    symbol  = context.args[1].upper()
    res = await swarm.set_pair(bot_id, symbol)
    if res["ok"]:
        await update.message.reply_text(
            f"🔄 *Paire changée* — `{res['bot_id'].upper()}`\n"
            f"`{res['old']}` → `{res['new']}`\nWarm-up relancé, trading actif au prochain tick.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"⚠️ `{res['error']}`", parse_mode="Markdown")
    log.info("setpair_via_telegram", bot=bot_id, symbol=symbol, ok=res["ok"])


async def cmd_addbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addbot <bot_id> <SYMBOL> [poids] — ajoute un bot à la volée."""
    swarm = _get_swarm()
    if not swarm:
        await update.message.reply_text("_Swarm non disponible._", parse_mode="Markdown")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : `/addbot <id> <SYMBOLE> [poids]`\nEx : `/addbot ada ADA-USDC 0.1`",
            parse_mode="Markdown",
        )
        return
    bot_id = context.args[0].lower()
    symbol = context.args[1].upper()
    try:
        weight = float(context.args[2]) if len(context.args) > 2 else 0.1
    except ValueError:
        await update.message.reply_text("⚠️ Le poids doit être un nombre (ex: 0.1)", parse_mode="Markdown")
        return
    res = await swarm.add_bot(bot_id, symbol, weight=weight, name=bot_id.upper())
    if res["ok"]:
        await update.message.reply_text(
            f"➕ *Bot ajouté* — `{res['bot_id'].upper()}` sur `{res['symbol']}` "
            f"({res['weight']:.0%})\nTâche démarrée, warm-up en cours.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"⚠️ `{res['error']}`", parse_mode="Markdown")
    log.info("addbot_via_telegram", bot=bot_id, symbol=symbol, ok=res["ok"])


async def cmd_removebot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removebot <bot_id> — retire un bot du swarm."""
    swarm = _get_swarm()
    if not swarm:
        await update.message.reply_text("_Swarm non disponible._", parse_mode="Markdown")
        return
    if not context.args:
        await update.message.reply_text("Usage : `/removebot <id>`", parse_mode="Markdown")
        return
    bot_id = _BOT_ALIASES.get(context.args[0].lower(), context.args[0].lower())
    res = await swarm.remove_bot(bot_id)
    if res["ok"]:
        await update.message.reply_text(f"➖ Bot `{res['bot_id'].upper()}` retiré du swarm.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ `{res['error']}`", parse_mode="Markdown")
    log.info("removebot_via_telegram", bot=bot_id, ok=res["ok"])


def _resolve_swarm():
    """Récupère le swarm depuis __main__ (injecté par main.py)."""
    import sys
    return getattr(sys.modules.get("__main__"), "SWARM", None)


def _resolve_director():
    import sys
    return getattr(sys.modules.get("__main__"), "DIRECTOR", None)


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
    # Swarm commands
    app.add_handler(CommandHandler("bots",     cmd_bots))
    app.add_handler(CommandHandler("kill",     cmd_kill))
    app.add_handler(CommandHandler("release",  cmd_release))
    app.add_handler(CommandHandler("pause",    cmd_pause_bot))
    # Configuration des paires / roster (P1/P2)
    app.add_handler(CommandHandler("setpair",   cmd_setpair))
    app.add_handler(CommandHandler("addbot",    cmd_addbot))
    app.add_handler(CommandHandler("removebot", cmd_removebot))
    # Claude AI Q&A + param tuning
    app.add_handler(CommandHandler("ask",      cmd_ask))
    app.add_handler(CommandHandler("tune",     cmd_tune))
    # Boutons inline (callback queries)
    app.add_handler(CallbackQueryHandler(cmd_button_callback))
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
