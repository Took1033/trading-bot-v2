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

import aiohttp
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
WARN_DRAWDOWN_PCT   = float(os.getenv("WARN_DRAWDOWN_PCT",      "0.05"))   # alerte soft avant le kill (5%)
MAX_HOURLY_LOSS_PCT = float(os.getenv("KILL_MAX_HOURLY_LOSS",   "0.03"))   # 3%
MAX_DAILY_LOSS_PCT  = float(os.getenv("KILL_MAX_DAILY_LOSS",    "0.10"))   # 10%
RESUME_AFTER_MIN    = int(os.getenv("KILL_RESUME_AFTER_MIN",    "60"))     # 60 min
CHECK_INTERVAL_S    = int(os.getenv("DIRECTOR_CHECK_INTERVAL_S", "30"))    # 30s
FG_CACHE_S          = 3600                                                  # cache Fear & Greed 1h
FG_EXTREME_FEAR     = int(os.getenv("FG_EXTREME_FEAR_THRESHOLD", "20"))    # Kill switch si FG < 20
FG_URL              = "https://api.alternative.me/fng/?limit=1"


class DirectorAgent:
    """Monitor + Kill Switch pour le swarm de bots."""

    def __init__(self, swarm: BotSwarm) -> None:
        self.swarm           = swarm
        self._initial_value  : float | None = None
        self._peak_value     : float | None = None
        self._kill_switch_at : float = 0.0           # timestamp d'activation
        self._kill_is_fg     : bool  = False         # pause causee par Fear & Greed ?
        self._daily_stop_at  : float = 0.0           # timestamp arret journalier
        self._dd_warned      : bool  = False         # alerte drawdown soft deja envoyee ?

        # Snapshots horaires pour calcul de perte horaire (60 min)
        self._hourly_window: deque[tuple[float, float]] = deque(maxlen=120)

        # Fear & Greed Index
        self._fg_value    : int | None = None
        self._fg_label    : str        = "—"
        self._fg_cached_at: float      = 0.0

        log.info("director_agent_ready",
                 max_dd=f"{MAX_DRAWDOWN_PCT:.0%}",
                 max_hour=f"{MAX_HOURLY_LOSS_PCT:.0%}",
                 max_day=f"{MAX_DAILY_LOSS_PCT:.0%}",
                 check_s=CHECK_INTERVAL_S,
                 fg_extreme_fear=FG_EXTREME_FEAR)

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
    # Fear & Greed Index
    # ─────────────────────────────────────────────────────────────────────────

    async def _fetch_fear_greed(self) -> int | None:
        """Retourne le Fear & Greed Index (0-100). Cache 1h."""
        now = time.time()
        if self._fg_cached_at > 0 and now - self._fg_cached_at < FG_CACHE_S:
            return self._fg_value

        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(FG_URL, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        return self._fg_value
                    data = await r.json()

            val   = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]
            self._fg_value     = val
            self._fg_label     = label
            self._fg_cached_at = now
            # Publie pour le RiskAgent
            from agents import trading_state
            trading_state.set_fear_greed(val, label)
            log.info("fear_greed_fetched", value=val, label=label)
            return val
        except Exception as exc:
            log.warning("fear_greed_fetch_error", error=str(exc))
            return self._fg_value   # retourne valeur en cache meme si expiree

    # ─────────────────────────────────────────────────────────────────────────
    # Logique de check
    # ─────────────────────────────────────────────────────────────────────────

    async def _check(self) -> None:
        now   = time.time()
        value = await self.swarm.get_portfolio_total()

        # Garde-fou anti-glitch de lecture : un snapshot a 0 (ou une chute > 50% du
        # pic en un seul tick) trahit un get_accounts incomplet cote Coinbase, PAS un
        # vrai drawdown — un portefeuille reel ne s'evapore pas en 30s, et le kill
        # switch se serait deja declenche a 8%. On ignore ce tick (ni peak, ni kill
        # switch, ni fenetre horaire) pour ne jamais declencher de faux kill switch.
        if value <= 0 or (self._peak_value and value < self._peak_value * 0.5):
            log.warning("snapshot_value_implausible_skipped",
                        value=round(value, 2),
                        peak=round(self._peak_value or 0, 2))
            return

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

        # ── 0. Fear & Greed Index ────────────────────────────────────────────
        fg = await self._fetch_fear_greed()
        if fg is not None:
            if fg < FG_EXTREME_FEAR:
                await self._trigger_kill_switch(
                    f"Extreme Fear (Fear & Greed = {fg}/100 — {self._fg_label})",
                    now,
                    cause_fg=True,
                )
                return
            elif fg >= 80:
                # Extreme Greed : notifier seulement (pas de kill switch automatique)
                log.warning("fear_greed_extreme_greed", value=fg, label=self._fg_label)

        # ── 1. Drawdown global ───────────────────────────────────────────────
        if self._peak_value and self._peak_value > 0:
            drawdown = (self._peak_value - value) / self._peak_value

            # Alerte "soft" anticipee : une seule fois entre WARN et le kill switch.
            # Re-armee quand on est nettement remonte (evite le spam au seuil).
            if WARN_DRAWDOWN_PCT <= drawdown < MAX_DRAWDOWN_PCT and not self._dd_warned:
                self._dd_warned = True
                log.warning("drawdown_warning", drawdown=round(drawdown, 4),
                            peak=round(self._peak_value, 2), value=round(value, 2))
                await notifier.notify(
                    f"⚠️ *Alerte drawdown* `{drawdown:.2%}`\n"
                    f"Sous le pic `{self._peak_value:.2f}` USDC (actuel `{value:.2f}`).\n"
                    f"Kill switch automatique à `{MAX_DRAWDOWN_PCT:.0%}`."
                )
            elif drawdown < WARN_DRAWDOWN_PCT * 0.5 and self._dd_warned:
                self._dd_warned = False

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
                  hourly_loss=round(hourly_loss, 4),
                  fear_greed=self._fg_value)

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

    async def _trigger_kill_switch(self, reason: str, now: float,
                                   cause_fg: bool = False) -> None:
        """Active le kill switch global avec notification."""
        self._kill_switch_at = now
        self._kill_is_fg     = cause_fg
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
        if elapsed_min < RESUME_AFTER_MIN:
            return

        # Pause causee par le Fear & Greed : ne lever que si le marche est sorti
        # de l'Extreme Fear. Sinon prolonger silencieusement (evite le thrash
        # KILL/release horaire + le spam Telegram en regime de peur prolonge).
        if self._kill_is_fg:
            fg = await self._fetch_fear_greed()
            if fg is not None and fg < FG_EXTREME_FEAR:
                self._kill_switch_at = now   # prolonge sans notification
                log.info("kill_switch_fg_hold", fg=fg,
                         elapsed_min=round(elapsed_min, 1))
                return

        trading_state.release_kill_switch()
        self._kill_switch_at = 0
        self._kill_is_fg     = False
        # Reset du peak pour eviter de re-trigger immediatement
        self._peak_value = await self.swarm.get_portfolio_total()

        log.info("kill_switch_released", elapsed_min=round(elapsed_min, 1))
        await notifier.notify(
            f"✅ *Kill switch leve* apres `{int(elapsed_min)} min`\n"
            f"Trading repris automatiquement."
        )
