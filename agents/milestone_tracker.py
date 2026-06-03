"""
MilestoneTracker - detecte les paliers de performance et notifie.

Le reinvestissement des profits est AUTOMATIQUE depuis l'origine :
le RiskAgent calcule la taille de position comme `portfolio_usdc * dynamic_pct`.
Si le portfolio croit, les positions grandissent mecaniquement (compounding).

Ce module ajoute la VISIBILITE de ce compounding :
  - Notif Telegram a chaque palier franchi : +10%, +25%, +50%, +100%, +200%, ...
  - Notif aussi en cas de chute majeure : -10%, -25%, -50%
  - Marqueurs persistes dans memory/.milestones.json (idempotent au redemarrage)

Lance comme tache asyncio en parallele de l'orchestrateur.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import structlog
from dotenv import load_dotenv

from interfaces import notifier

load_dotenv()
log = structlog.get_logger()

DB_PATH        = os.getenv("DB_PATH", "memory/trading.db")
MARKER_FILE    = Path(DB_PATH).parent / ".milestones.json"
CHECK_INTERVAL = int(os.getenv("MILESTONE_CHECK_INTERVAL_S", "300"))   # toutes les 5 min

# Paliers en % depuis le capital initial (positifs = gains, negatifs = pertes)
POSITIVE_MILESTONES = [10, 25, 50, 100, 200, 500, 1000]
NEGATIVE_MILESTONES = [-10, -25, -50, -75]


def _load_marker() -> dict:
    if MARKER_FILE.exists():
        try:
            return json.loads(MARKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_marker(data: dict) -> None:
    try:
        MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("milestone_save_failed", error=str(exc))


def _check_milestones(pnl_pct: float, reached: list[float]) -> list[float]:
    """Retourne les NOUVEAUX paliers franchis (pas encore notifies)."""
    new = []
    for m in POSITIVE_MILESTONES:
        if pnl_pct >= m and m not in reached:
            new.append(m)
    for m in NEGATIVE_MILESTONES:
        if pnl_pct <= m and m not in reached:
            new.append(m)
    return new


async def milestone_loop(swarm) -> None:
    """
    Boucle infinie : verifie le portfolio toutes les N secondes,
    notifie a chaque nouveau palier franchi.
    """
    log.info("milestone_tracker_started", interval_s=CHECK_INTERVAL,
             positive=POSITIVE_MILESTONES, negative=NEGATIVE_MILESTONES)

    state = _load_marker()
    state.setdefault("initial_capital", None)
    state.setdefault("reached", [])

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)

            total = await swarm.get_portfolio_total()
            if total <= 0:
                continue

            # Premier passage : on enregistre le capital initial
            if state["initial_capital"] is None:
                state["initial_capital"] = total
                _save_marker(state)
                log.info("milestone_capital_init", initial=round(total, 2))
                continue

            pnl_pct = (total - state["initial_capital"]) / state["initial_capital"] * 100
            new_milestones = _check_milestones(pnl_pct, state["reached"])

            for m in new_milestones:
                state["reached"].append(m)
                emoji = "🚀" if m > 0 else "⚠️"
                direction = "Gain" if m > 0 else "Perte"
                await notifier.notify(
                    f"{emoji} *PALIER FRANCHI : {direction} {m:+d}%* {emoji}\n"
                    f"Capital initial  : `{state['initial_capital']:.2f}` USDC\n"
                    f"Capital actuel   : `{total:.2f}` USDC\n"
                    f"P&L total        : `{pnl_pct:+.2f}%`\n"
                    f"Variation USDC   : `{total - state['initial_capital']:+.2f}` USDC\n"
                    f"_Compounding actif : les prochains trades sont sizes sur ce nouveau capital._"
                )
                log.info("milestone_reached", pct=m, pnl=round(pnl_pct, 2),
                         capital=round(total, 2))

            if new_milestones:
                _save_marker(state)

        except asyncio.CancelledError:
            log.info("milestone_tracker_stopped")
            raise
        except Exception as exc:
            log.warning("milestone_error", error=str(exc))
            await asyncio.sleep(60)
