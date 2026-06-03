"""
WeeklyStats - envoie un resume hebdomadaire avec metriques pro.

Calcule sur les 7 derniers jours :
  - Win rate     (% de trades gagnants)
  - Sharpe ratio (rendement / volatilite, annualise)
  - Max drawdown (chute max du portefeuille depuis un peak)
  - P&L 7j
  - Top/worst trade

Lance comme tache asyncio en parallele de l'orchestrateur (main.py).
Trigger : tous les lundi a 09:00 UTC (configurable via env).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import structlog
from dotenv import load_dotenv

from interfaces import notifier

load_dotenv()
log = structlog.get_logger()

DB_PATH      = os.getenv("DB_PATH", "memory/trading.db")
WEEKLY_DOW   = int(os.getenv("WEEKLY_STATS_DOW_UTC",  "0"))    # 0=Lundi
WEEKLY_HOUR  = int(os.getenv("WEEKLY_STATS_HOUR_UTC", "9"))
WEEKLY_MIN   = int(os.getenv("WEEKLY_STATS_MIN_UTC",  "5"))    # +5min apres daily


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seconds_until_next_weekday(dow: int, hour: int, minute: int) -> float:
    """Secondes jusqu'au prochain jour de la semaine donne (Mon=0, Sun=6) a HH:MM UTC."""
    now    = datetime.now(timezone.utc)
    days_ahead = (dow - now.weekday()) % 7
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) \
             + timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _max_drawdown(values: list[float]) -> float:
    """Retourne le max drawdown en %, calcule sur une serie chronologique."""
    if not values:
        return 0.0
    peak    = values[0]
    max_dd  = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _sharpe_ratio(returns: list[float], risk_free: float = 0.0) -> float | None:
    """
    Sharpe annualise sur des rendements horaires/journaliers.
    Hypothese : echantillons espaces uniformement (sinon biaise).
    """
    if len(returns) < 2:
        return None
    mean  = sum(returns) / len(returns)
    var   = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std   = math.sqrt(var)
    if std == 0:
        return None
    # Annualisation grossiere : 365 si quotidien, sqrt(periods/year) standard
    # On part de l'hypothese 1 snapshot par cycle ~ minute → on annualise prudemment
    # On utilise un facteur ~252 (jours ouvres) sur returns journaliers comme proxy
    return (mean - risk_free) / std * math.sqrt(252)


def _compute_7d_metrics() -> dict:
    """Calcule les metriques sur les 7 derniers jours depuis la DB."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db() as conn:
        # Ordres executes (sells = trades fermes)
        sells = conn.execute(
            "SELECT symbol, timestamp, metadata FROM decisions "
            "WHERE task_type='order' AND role='orchestrator' AND action='sell' "
            "AND timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()

        buys = conn.execute(
            "SELECT symbol, timestamp, metadata FROM decisions "
            "WHERE task_type='order' AND role='orchestrator' AND action='buy' "
            "AND timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()

        # Snapshots pour drawdown et Sharpe
        snapshots = conn.execute(
            "SELECT total_usdc, timestamp FROM portfolio_snapshots "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()

    # Win rate : on apparie BUY → SELL par symbole (FIFO simple)
    open_positions: dict[str, list[dict]] = {}   # symbol -> stack de BUYs
    trade_pnls:      list[float] = []
    best_trade:     float = 0.0
    worst_trade:    float = 0.0

    for row in buys:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            open_positions.setdefault(row["symbol"], []).append({
                "price": float(meta.get("price", 0)),
                "qty":   float(meta.get("qty",   0)),
            })
        except Exception:
            continue

    for row in sells:
        try:
            meta       = json.loads(row["metadata"]) if row["metadata"] else {}
            sell_price = float(meta.get("price", 0))
            sell_qty   = float(meta.get("qty",   0))
            stack      = open_positions.get(row["symbol"], [])
            if not stack or sell_price == 0:
                continue
            # FIFO : on prend les BUYs dans l'ordre
            buy = stack.pop(0)
            if buy["price"] > 0:
                pnl_pct = (sell_price - buy["price"]) / buy["price"] * 100
                trade_pnls.append(pnl_pct)
                if pnl_pct > best_trade:  best_trade  = pnl_pct
                if pnl_pct < worst_trade: worst_trade = pnl_pct
        except Exception:
            continue

    n_trades = len(trade_pnls)
    n_wins   = sum(1 for p in trade_pnls if p > 0)
    win_rate = (n_wins / n_trades * 100) if n_trades > 0 else None

    # Max drawdown sur snapshots
    values   = [s["total_usdc"] for s in snapshots]
    max_dd   = _max_drawdown(values) if values else 0.0

    # Sharpe sur rendements entre snapshots
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            returns.append((values[i] - values[i - 1]) / values[i - 1])
    sharpe = _sharpe_ratio(returns)

    # P&L 7j
    pnl_7d_usdc = (values[-1] - values[0]) if len(values) >= 2 else None
    pnl_7d_pct  = (pnl_7d_usdc / values[0] * 100) if pnl_7d_usdc and values[0] > 0 else None

    return {
        "n_trades":     n_trades,
        "n_wins":       n_wins,
        "win_rate":     win_rate,
        "best_trade":   best_trade   if n_trades else None,
        "worst_trade":  worst_trade  if n_trades else None,
        "max_drawdown": max_dd,
        "sharpe":       sharpe,
        "pnl_7d_usdc":  pnl_7d_usdc,
        "pnl_7d_pct":   pnl_7d_pct,
        "current_value": values[-1] if values else None,
        "n_snapshots":  len(snapshots),
    }


def _format_stats(m: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"📈 *Stats hebdomadaires* — {now}",
        f"",
        f"*Trades 7j*",
    ]

    if m["n_trades"] == 0:
        lines.append("  _Aucun trade ferme sur la periode._")
    else:
        win_emoji = "🟢" if (m["win_rate"] or 0) >= 50 else "🔴"
        lines += [
            f"  Trades fermes : `{m['n_trades']}`",
            f"  Win rate      : {win_emoji} `{m['win_rate']:.1f}%`",
            f"  Best trade    : `{m['best_trade']:+.2f}%`",
            f"  Worst trade   : `{m['worst_trade']:+.2f}%`",
        ]

    lines += [f"", f"*Performance*"]

    if m["pnl_7d_usdc"] is not None:
        emoji = "📈" if m["pnl_7d_usdc"] >= 0 else "📉"
        lines.append(
            f"  P&L 7j        : {emoji} `{m['pnl_7d_usdc']:+.2f}` USDC "
            f"(`{m['pnl_7d_pct']:+.2f}%`)"
        )
    if m["current_value"] is not None:
        lines.append(f"  Valeur        : `{m['current_value']:.2f}` USDC")
    if m["max_drawdown"] > 0:
        emoji = "🔴" if m["max_drawdown"] > 5 else "🟡" if m["max_drawdown"] > 2 else "🟢"
        lines.append(f"  Max drawdown  : {emoji} `{m['max_drawdown']:.2f}%`")
    if m["sharpe"] is not None:
        emoji = "🟢" if m["sharpe"] > 1 else "🟡" if m["sharpe"] > 0 else "🔴"
        lines.append(f"  Sharpe ratio  : {emoji} `{m['sharpe']:.2f}` (annualise)")

    return "\n".join(lines)


async def weekly_stats_loop() -> None:
    """Boucle infinie : attend lundi 09:05 UTC, envoie les stats, recommence."""
    log.info("weekly_stats_started",
             dow=WEEKLY_DOW, hour_utc=WEEKLY_HOUR, minute=WEEKLY_MIN)

    while True:
        try:
            wait_s = _seconds_until_next_weekday(WEEKLY_DOW, WEEKLY_HOUR, WEEKLY_MIN)
            log.info("weekly_stats_sleeping",
                     seconds=int(wait_s),
                     next_at=f"jour={WEEKLY_DOW} {WEEKLY_HOUR:02d}:{WEEKLY_MIN:02d} UTC")
            await asyncio.sleep(wait_s)

            metrics = _compute_7d_metrics()
            message = _format_stats(metrics)

            # Enrichissement Claude Sonnet : analyse marche en 3 phrases
            try:
                from interfaces.claude_client import weekly_market_analysis, ENABLED
                if ENABLED:
                    analysis = await weekly_market_analysis(metrics)
                    if analysis:
                        message += f"\n\n🧠 *Analyse Claude :*\n_{analysis}_"
            except Exception as exc:
                log.debug("weekly_narrative_skip", error=str(exc))

            sent = await notifier.notify(message)
            log.info("weekly_stats_sent", success=sent,
                     n_trades=metrics["n_trades"],
                     win_rate=metrics["win_rate"])

            await asyncio.sleep(70)   # evite double-trigger

        except asyncio.CancelledError:
            log.info("weekly_stats_stopped")
            raise
        except Exception as exc:
            log.error("weekly_stats_error", error=str(exc))
            await asyncio.sleep(300)
