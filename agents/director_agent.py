"""
DirectorAgent - chef d'orchestre du swarm de bots.

Inspire de Kairos Alpha v2 "director-agent" :
  - Monitore en continu le portefeuille global
  - Applique le Kill Switch en cas de :
    * Drawdown total > 8%  -> pause tous les bots (60 min)
    * Perte horaire > 3%   -> pause tous les bots (60 min)
    * Perte journaliere > 10% -> arret 24h
  - Notifie via Telegram a chaque declenchement / reprise
  - Reprise automatique apres la duree configuree

Loop independante (asyncio.task), check toutes les 30s.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque

import structlog
from dotenv import load_dotenv

from agents import trading_state
from agents.bot_swarm import BotSwarm
from interfaces import notifier

load_dotenv()
log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Configuration des seuils Kill Switch
# ─────────────────────────────────────────────────────────────────────────────

MAX_DRAWDOWN_PCT    = float(os.getenv("KILL_MAX_DRAWDOWN_PCT",  "0.08"))   # 8%
MAX_HOURLY_LOSS_PCT = float(os.getenv("KILL_MAX_HOURLY_LOSS",   "0.03"))   # 3%
MAX_DAILY_LOSS_PCT  = float(os.getenv("KILL_MAX_DAILY_LOSS",    "0.10"))   # 10%
RESUME_AFTER_MIN    = int(os.getenv("KILL_RESUME_AFTER_MIN",    "60"))     # 60 min
CHECK_INTERVAL_S    = int(os.getenv("DIRECTOR_CHECK_INTERVAL_S", "30"))    # 30s


class DirectorAgent:
    """Monitor + Kill Switch pour le swarm de bots."""

    def __init__(self, swarm: BotSwarm) -> None:
        self.swarm           = swarm
        self._initial_value  : float | None = None
        self._peak_value     : float | None = None
        self._kill_switch_at : float = 0.0           # timestamp d'activation
        self._daily_stop_at  : float = 0.0           # timestamp arret journalier

        # Snapshots horaires pour calcul de perte horaire (60 min)
        # On garde (timestamp, value) toutes les minutes max
        self._hourly_window: deque[tuple[float, float]] = deque(maxlen=120)

        log.info("director_agent_ready",
                 max_dd=f"{MAX_DRAWDOWN_PCT:.0%}",
                 max_hour=f"{MAX_HOURLY_LOSS_PCT:.0%}",
                 max_day=f"{MAX_DAILY_LOSS_PCT:.0%}",
                 check_s=CHECK_INTERVAL_S)

    # ─────────────────────────────────────────────────────────────────────────
    # Boucle principale
    # ─────────────────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Loop infini : check toutes les 30s."""
        # Petit delai initial pour laisser les bots demarrer
        await asyncio.sleep(15)

        while True:
            try:
                await self._check()
            except Exception as exc:
                log.error("director_check_error", error=str(exc))
            await asyncio.sleep(CHECK_INTERVAL_S)

    # ─────────────────────────────────────────────────────────────────────────
    # Logique de check
    # ─────────────────────────────────────────────────────────────────────────

    async def _check(self) -> None:
        now   = time.time()
        value = await self.swarm.get_portfolio_total()

        # Init au premier check
        if self._initial_value is None:
            self._initial_value = value
            self._peak_value    = value
            self._hourly_window.append((now, value))
            log.info("director_initial_capital", value=round(value, 2))
            return

        # Mise a jour du peak
        if value > (self._peak_value or 0):
            self._peak_value = value

        # Mise a jour de la fenetre horaire (1 sample / 30s = 120 samples / heure)
        self._hourly_window.append((now, value))

        # ── Si kill switch actif : check de reprise ──────────────────────────
        if trading_state.is_kill_switch_active():
            await self._maybe_release_kill_switch(now)
            return

        # ── Si arret journalier actif : ne rien faire ────────────────────────
        if self._daily_stop_at > 0 and (now - self._daily_stop_at) < 86400:
            return

        # ── 1. Drawdown global ───────────────────────────────────────────────
        if self._peak_value and self._peak_value > 0:
            drawdown = (self._peak_value - value) / self._peak_value
            if drawdown >= MAX_DRAWDOWN_PCT:
                await self._trigger_kill_switch(
                    f"Drawdown {drawdown:.2%} >= {MAX_DRAWDOWN_PCT:.0%}",
                    now,
                )
                return

        # ── 2. Perte horaire ─────────────────────────────────────────────────
        hourly_loss = self._compute_hourly_loss(now)
        if hourly_loss >= MAX_HOURLY_LOSS_PCT:
            await self._trigger_kill_switch(
                f"Perte horaire {hourly_loss:.2%} >= {MAX_HOURLY_LOSS_PCT:.0%}",
                now,
            )
            return

        # ── 3. Perte journaliere (depuis init) ───────────────────────────────
        if self._initial_value > 0:
            daily_loss = (self._initial_value - value) / self._initial_value
            if daily_loss >= MAX_DAILY_LOSS_PCT:
                self._daily_stop_at = now
                trading_state.kill_switch(f"Perte journaliere {daily_loss:.2%} - arret 24h")
                await notifier.notify(
                    f"🚨 *KILL SWITCH JOURNALIER*\n"
                    f"Perte 24h : `{daily_loss:.2%}` (limite `{MAX_DAILY_LOSS_PCT:.0%}`)\n"
                    f"Arret de tous les bots pour `24h`."
                )
                log.error("daily_kill_switch", loss=round(daily_loss, 4))
                return

        log.debug("director_check_ok",
                  value=round(value, 2),
                  peak=round(self._peak_value, 2),
                  hourly_loss=round(hourly_loss, 4))

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_hourly_loss(self, now: float) -> float:
        """
        Retourne la perte relative entre la valeur d'il y a 1h et maintenant.
        0 si on n'a pas encore 1h de donnees.
        """
        threshold = now - 3600
        old_values = [v for ts, v in self._hourly_window if ts <= threshold]
        if not old_values:
            return 0.0
        ref     = max(old_values)   # le plus haut sur la fenetre 1h+
        current = self._hourly_window[-1][1]
        if ref <= 0:
            return 0.0
        return max(0.0, (ref - current) / ref)

    async def _trigger_kill_switch(self, reason: str, now: float) -> None:
        """Active le kill switch global avec notification."""
        self._kill_switch_at = now
        trading_state.kill_switch(reason)

        log.error("kill_switch_triggered", reason=reason)
        await notifier.notify(
            f"🚨 *KILL SWITCH ACTIVE*\n"
            f"Raison : {reason}\n"
            f"Tous les bots sont mis en *PAUSE* pour `{RESUME_AFTER_MIN} min`.\n"
            f"Reprise automatique sauf intervention."
        )

    async def _maybe_release_kill_switch(self, now: float) -> None:
        """Verifie si le kill switch peut etre desactive (apres delai)."""
        if self._kill_switch_at == 0:
            return
        elapsed_min = (now - self._kill_switch_at) / 60
        if elapsed_min >= RESUME_AFTER_MIN:
            trading_state.release_kill_switch()
            self._kill_switch_at = 0
            # Reset du peak pour eviter de re-trigger immediatement
            self._peak_value = await self.swarm.get_portfolio_total()

            log.info("kill_switch_released", elapsed_min=round(elapsed_min, 1))
            await notifier.notify(
                f"✅ *Kill switch leve* apres `{int(elapsed_min)} min`\n"
                f"Trading repris automatiquement."
            )
