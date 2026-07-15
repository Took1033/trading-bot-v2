"""
DailySummary - envoie un resume quotidien via Telegram a heure fixe.

Lance comme tache asyncio en parallele de l'orchestrateur (main.py).
Calcule les metriques des dernieres 24h depuis la DB et notifie.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import time
from datetime import datetime, time as dt_time, timedelta, timezone

import structlog
from dotenv import load_dotenv

from agents import trading_state
from interfaces import notifier

load_dotenv()
log = structlog.get_logger()

DB_PATH       = os.getenv("DB_PATH", "memory/trading.db")
SUMMARY_HOUR  = int(os.getenv("DAILY_SUMMARY_HOUR_UTC", "9"))   # 9h UTC par defaut
SUMMARY_MIN   = int(os.getenv("DAILY_SUMMARY_MIN_UTC",  "0"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seconds_until_next(hour: int, minute: int = 0) -> float:
    """Retourne le nombre de secondes jusqu'au prochain HH:MM UTC."""
    now    = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _compute_24h_metrics() -> dict:
    """Calcule les metriques sur les dernieres 24h depuis la DB."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with _db() as conn:
        # Compteurs par type
        signals = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='signal' AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        orders_executed = conn.execute(
            # role IN (...) : orchestrator (scalpeur historique) + trend_bot
            # (bots actuels) + user (cloture manuelle) comptent tous comme
            # ordre execute — sinon les clotures manuelles disparaissent du resume.
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='order' AND role IN ('orchestrator','trend_bot','user') "
            "AND action IN ('buy','sell') AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        orders_rejected = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='order' AND action='rejected' AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        alerts = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='alert' AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        # Snapshots pour le P&L 24h
        first_snap = conn.execute(
            "SELECT total_usdc FROM portfolio_snapshots "
            "WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (since,),
        ).fetchone()

        last_snap = conn.execute(
            "SELECT total_usdc, pnl_pct FROM portfolio_snapshots "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

    pnl_24h_usdc = None
    pnl_24h_pct  = None
    if first_snap and last_snap:
        pnl_24h_usdc = last_snap["total_usdc"] - first_snap["total_usdc"]
        if first_snap["total_usdc"] > 0:
            pnl_24h_pct = pnl_24h_usdc / first_snap["total_usdc"] * 100

    return {
        "signals":          signals,
        "orders_executed":  orders_executed,
        "orders_rejected":  orders_rejected,
        "alerts":           alerts,
        "current_value":    last_snap["total_usdc"] if last_snap else None,
        "pnl_total_pct":    last_snap["pnl_pct"]   if last_snap else None,
        "pnl_24h_usdc":     pnl_24h_usdc,
        "pnl_24h_pct":      pnl_24h_pct,
    }


def _format_summary(metrics: dict) -> str:
    """Met en forme le resume Telegram (Markdown)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"📊 *Résumé quotidien* — {now}",
        f"",
        f"*Activité 24h*",
        f"  Signaux générés : `{metrics['signals']}`",
        f"  Ordres exécutés : `{metrics['orders_executed']}`",
        f"  Ordres rejetés  : `{metrics['orders_rejected']}`",
    ]

    if metrics["alerts"]:
        lines.append(f"  ⚠️ Alertes      : `{metrics['alerts']}`")

    if metrics["current_value"] is not None:
        lines += [
            f"",
            f"*Portefeuille*",
            f"  Valeur actuelle : `{metrics['current_value']:.2f} USDC`",
        ]
        if metrics["pnl_total_pct"] is not None:
            emoji = "🟢" if metrics["pnl_total_pct"] >= 0 else "🔴"
            lines.append(f"  P&L total       : {emoji} `{metrics['pnl_total_pct']:+.2f}%`")
        if metrics["pnl_24h_usdc"] is not None:
            emoji = "📈" if metrics["pnl_24h_usdc"] >= 0 else "📉"
            lines.append(
                f"  P&L 24h         : {emoji} `{metrics['pnl_24h_usdc']:+.2f}` USDC "
                f"(`{metrics['pnl_24h_pct']:+.2f}%`)"
            )
    else:
        lines.append("")
        lines.append("_Aucune activité de trading sur 24h._")

    return "\n".join(lines)


def _format_system_state() -> str:
    """Bloc 'heartbeat' : etat live du systeme (kill switch, Fear & Greed, mode).

    Complete le resume P&L pour qu'il serve aussi de preuve de vie et explique
    POURQUOI le bot est eventuellement a l'arret (kill switch / Extreme Fear).
    """
    mode               = os.getenv("COINBASE_MODE", "paper").upper()
    fg_value, fg_label = trading_state.get_fear_greed()
    killed             = trading_state.is_kill_switch_active()

    lines = ["", "*État système*"]
    if killed:
        reason = trading_state.get_kill_reason() or "—"
        since  = trading_state.get_kill_since()
        days   = f" depuis `{int((time.time() - since) // 86400)} j`" if since else ""
        lines.append(f"  Kill switch : 🔴 *ACTIF*{days} — _{reason}_")
    else:
        lines.append("  Kill switch : 🟢 inactif")
    if fg_value is not None:
        lines.append(f"  Fear & Greed : `{fg_value}` ({fg_label})")
    lines.append(f"  Mode : `{mode}`")
    return "\n".join(lines)


def _format_trend_state() -> str:
    """Bloc tendance : dernier signal de chaque TrendBot (distance a la SMA50).

    Rend le silence lisible : « flat sous la SMA50 » est un etat normal qui
    doit se VOIR dans le resume, pas se deviner. Un signal vieux de plus de
    24h est marque stale — c'est le symptome d'une boucle d'evaluation morte.
    """
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT symbol, action, metadata, MAX(timestamp) AS ts "
                "FROM decisions WHERE role='trend_bot' AND task_type='signal' "
                "GROUP BY symbol ORDER BY symbol"
            ).fetchall()
    except Exception as exc:
        log.debug("trend_state_skip", error=str(exc))
        return ""
    if not rows:
        return ""

    lines = ["", "*Tendance (dernier signal)*"]
    now = datetime.now(timezone.utc)
    for r in rows:
        dist_txt = "?"
        try:
            meta = ast.literal_eval(r["metadata"] or "{}")
            dist_txt = f"`{float(meta['dist_pct']):+.1f}%` vs SMA50"
        except Exception:
            pass
        state = "long" if r["action"] == "buy" else "flat"
        stale = ""
        try:
            ts = datetime.fromisoformat(r["ts"])
            if (now - ts).total_seconds() > 86400:
                stale = " ⚠️ _signal > 24h_"
        except Exception:
            pass
        lines.append(f"  `{r['symbol']}` : {dist_txt} → {state}{stale}")
    return "\n".join(lines)


async def daily_summary_loop() -> None:
    """
    Boucle infinie : attend l'heure cible, envoie le resume, recommence.
    A lancer comme tache asyncio en parallele de l'orchestrateur.
    """
    log.info("daily_summary_started", hour_utc=SUMMARY_HOUR, minute=SUMMARY_MIN)

    while True:
        try:
            wait_s = _seconds_until_next(SUMMARY_HOUR, SUMMARY_MIN)
            log.info("daily_summary_sleeping", seconds=int(wait_s),
                     next_at=f"{SUMMARY_HOUR:02d}:{SUMMARY_MIN:02d} UTC")
            await asyncio.sleep(wait_s)

            metrics = _compute_24h_metrics()
            message = (_format_summary(metrics) + "\n" + _format_system_state()
                       + _format_trend_state())

            # Enrichissement Claude Haiku : narration en 2 phrases
            try:
                from interfaces.claude_client import narrate_daily, ENABLED
                if ENABLED:
                    narrative = await narrate_daily(metrics)
                    if narrative:
                        message += f"\n\n🤖 _{narrative}_"
            except Exception as exc:
                log.debug("daily_narrative_skip", error=str(exc))

            sent = await notifier.notify(message)
            log.info("daily_summary_sent", success=sent, metrics=metrics)

            # Petite marge pour eviter de redeclencher dans la meme minute
            await asyncio.sleep(70)

        except asyncio.CancelledError:
            log.info("daily_summary_stopped")
            raise
        except Exception as exc:
            log.error("daily_summary_error", error=str(exc))
            await asyncio.sleep(60)   # retry dans 1 min
