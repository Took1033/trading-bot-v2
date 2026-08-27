"""
TrendBot — bot de trend-following DAILY long-only (edge valide le 2026-06-03).

Independant de l'Orchestrator scalpeur : PAS de stop-loss/take-profit serres
(qui tueraient un trend-follower). Logique :
  - long quand prix > SMA50 des clotures journalieres
  - sortie quand prix repasse sous la SMA50 (retournement de tendance)
On tient les drawdowns ; on sort sur signal, pas sur un -3%.

Expose la meme interface qu'un Orchestrator pour BotSwarm.get_status (dashboard).
Trade rarement (~quelques fois/an) -> check espace (TREND_CHECK_S, defaut 5 min).

⚠️ Place de VRAIS ordres en mode live. Active via la config swarm (type=trend).
"""
from __future__ import annotations

import asyncio
import os
import time

import structlog
from dotenv import load_dotenv

from agents import trading_state
from agents import autoclose
from agents.market_agent import MarketAgent
from interfaces import notifier
from interfaces.coinbase_client import UnsellableDustError
from strategies.simple_ma  import Signal
from strategies.trend_daily import analyze as trend_analyze

load_dotenv()
log = structlog.get_logger()

TREND_CHECK_S       = int(os.getenv("TREND_CHECK_S", "300"))         # frequence de check (5 min)
TREND_POSITION_PCT  = float(os.getenv("TREND_POSITION_PCT", "0.03"))  # % du portefeuille par position
TREND_MIN_USDC      = float(os.getenv("TREND_MIN_USDC", "5.0"))       # mise mini
# Trailing-stop optionnel (OFF par defaut). >0 = fraction de recul depuis le pic
# qui declenche une sortie (ex: 0.10 = sortir si -10% depuis le plus haut atteint
# depuis l'entree). Verrouille une partie du gain ouvert ; reduit l'edge trend pur
# (on se fait sortir de certains gros runs). A activer en conscience.
TREND_TRAIL_PCT     = float(os.getenv("TREND_TRAILING_STOP_PCT", "0") or "0")
# Stop catastrophe (OFF par defaut) : coupe une position qui perd plus de X% depuis
# l'entree, AVANT que la SMA (qui retarde) ne rattrape un krach. Filet pour une
# exposition elevee ; ne gene pas le trend normal (une vague casse la SMA bien avant
# -15/-20%). A valider en backtest (run_backtest_stop.py) avant d'activer.
TREND_STOP_LOSS_PCT = float(os.getenv("TREND_STOP_LOSS_PCT", "0") or "0")
# Alerte "gros gain ouvert" : notif Telegram quand une position depasse +X% depuis
# l'entree (defaut 10%), pour decider de tenir (surfer) ou couper. Envoyee UNE fois
# par position (re-armee a la sortie). 0 = desactive.
TREND_ALERT_GAIN_PCT = float(os.getenv("TREND_ALERT_GAIN_PCT", "10.0"))
# Taker par cote (meme source que coinbase_client) pour chiffrer le P&L NET en notif.
TAKER_FEE_PCT        = float(os.getenv("COINBASE_TAKER_FEE_PCT", "0.0075"))
# Filtre de regime macro (OFF par defaut) : n'ouvre les ALTS que si l'actif directeur
# (BTC) est lui-meme haussier (prix > sa SMA daily). Evite d'acheter des alts en plein
# bear global — la ou AVAX/DOT/LTC saignent au backtest. A VALIDER avant d'activer.
REGIME_FILTER_ENABLED = os.getenv("REGIME_FILTER_ENABLED", "false").lower() in ("true", "1", "yes")
REGIME_SYMBOL         = os.getenv("REGIME_FILTER_SYMBOL", "BTC-USDC")
# Cap d'exposition combinee du swarm (fraction du portefeuille deployee, tous bots
# confondus). Applique a CHAQUE entree (defaut 1.0 = pas de plafond si non defini).
# Sans ca, N bots correles peuvent tous entrer sur la meme vague -> exposition non
# bornee (limitee seulement par le cash). Reduit ou refuse l'entree si le cap serait
# depasse. Ne peut que reduire l'exposition (jamais l'augmenter) : sans risque.
RISK_MAX_COMBINED_EXPOSURE_PCT = float(os.getenv("RISK_MAX_COMBINED_EXPOSURE_PCT", "1.0") or "1.0")


class TrendBot:
    """Trend-following daily long-only. Interface compatible BotSwarm.get_status."""

    def __init__(self, symbol: str, coinbase, memory, weight: float = 0.1,
                 bot_id: str | None = None) -> None:
        self.symbol       = symbol
        self.bot_id       = bot_id or f"trend_{symbol.split('-')[0].lower()}"
        self.weight       = weight
        self.display_name = f"Trend {symbol.split('-')[0]}"
        self._coinbase    = coinbase
        self._memory      = memory
        self._market      = MarketAgent(symbol=symbol)   # pour price_history/warmup/dashboard
        self._last_trade_ts: float = 0.0
        self._signal_streak: dict  = {"action": None, "count": 0}
        self._last_dist: float | None = None   # distance prix vs SMA (%) du dernier signal
        self._peak_price:    float = 0.0   # plus haut depuis l'entree (trailing-stop)
        self._gain_alerted:  bool  = False  # alerte "+X%" deja envoyee pour cette position
        # Serialise lecture-position + ordre : empeche un tick (entree/sortie) et une
        # cloture manuelle (force_close) / switch de paire de s'entrelacer sur la meme
        # position -> jamais deux ventes sur la meme qty (audit [21][22]).
        self._order_lock = asyncio.Lock()

        log.info("trend_bot_ready", bot_id=self.bot_id, symbol=symbol,
                 sma_check_s=TREND_CHECK_S, position_pct=TREND_POSITION_PCT)

    # ── Interface compat (set_pair) ──────────────────────────────────────────
    async def switch_symbol(self, new_symbol: str) -> None:
        self.symbol = new_symbol
        self.display_name = f"Trend {new_symbol.split('-')[0]}"
        self._market = MarketAgent(symbol=new_symbol)
        await self._market.warmup_from_history()

    # ── Boucle principale ────────────────────────────────────────────────────
    async def run_forever(self) -> None:
        log.info("trend_bot_start", bot_id=self.bot_id, symbol=self.symbol)
        await self._market.warmup_from_history()
        try:
            while True:
                try:
                    async with self._order_lock:   # exclut force_close/switch pendant le tick
                        await self._tick()
                except Exception as exc:
                    log.error("trend_bot_tick_error", bot_id=self.bot_id, error=str(exc))
                await asyncio.sleep(TREND_CHECK_S)
        except asyncio.CancelledError:
            log.info("trend_bot_stopped", bot_id=self.bot_id)
            raise

    async def _tick(self) -> None:
        # Pause individuelle : tick saute. Le kill switch global ne bloque que
        # l'ENTREE (plus bas) : evaluation, persistance des signaux et sorties
        # (trailing / retournement SMA50) continuent — un trend-follower doit
        # pouvoir fermer sa position meme en plein Extreme Fear.
        if trading_state.is_bot_paused(self.bot_id):
            return

        live_price = await self._coinbase.get_price(self.symbol)
        self._market._prices.append(live_price)   # garde l'historique dashboard frais

        sig = await trend_analyze(self.symbol, live_price)
        self._signal_streak = {"action": sig.action, "count": 1}
        try:
            self._last_dist = (sig.metadata or {}).get("dist_pct")
        except Exception:
            self._last_dist = None

        self._memory.record_decision(
            role="trend_bot", task_type="signal", symbol=self.symbol,
            action=sig.action, confidence=sig.confidence,
            reasoning=sig.reasoning, metadata=str(sig.metadata),
        )

        pos      = self._coinbase.get_position(self.symbol)
        have_pos = bool(pos and pos.get("qty", 0) > 0)

        # Sorties de protection sur position ouverte (avant l'evaluation SMA) :
        #  1) "Close reglable" par bot (autoclose) : take-profit fixe OU trailing,
        #     OFF par defaut, arme a la demande depuis le dashboard. Apres
        #     declenchement le bot RESTE ACTIF (rachat possible au tick suivant si
        #     la tendance tient) — choix du 2026-07-12.
        #  2) Trailing-stop GLOBAL (TREND_TRAIL_PCT, env) : conserve, OFF par defaut.
        if have_pos:
            self._peak_price = max(self._peak_price, live_price)
            avg = pos.get("avg_price", 0.0) or 0.0

            # Stop catastrophe (prioritaire) : coupe avant que la SMA lente ne
            # rattrape un krach. OFF par defaut ; ne se declenche que sur chute brutale.
            if TREND_STOP_LOSS_PCT > 0 and avg > 0 and live_price <= avg * (1 - TREND_STOP_LOSS_PCT / 100):
                loss = (live_price - avg) / avg * 100
                stop_sig = Signal(
                    "sell", 0.98,
                    f"STOP CATASTROPHE : {loss:.1f}% (seuil -{TREND_STOP_LOSS_PCT:.0f}%)",
                    self.symbol, {"loss_pct": round(loss, 2)})
                await self._exit(live_price, pos, stop_sig)
                return

            # Alerte "gros gain ouvert" (une fois par position) : laisse Brice decider
            # de tenir la vague ou de couper. Ne force aucune action.
            if TREND_ALERT_GAIN_PCT > 0 and avg > 0 and not self._gain_alerted:
                gain = (live_price - avg) / avg * 100
                if gain >= TREND_ALERT_GAIN_PCT:
                    self._gain_alerted = True
                    log.info("trend_gain_alert", bot_id=self.bot_id, gain_pct=round(gain, 2))
                    await notifier.notify(
                        f"🚀 *{self.symbol}* — position à `+{gain:.1f}%`\n"
                        f"Entrée `{avg:,.4f}` → `{live_price:,.4f}`\n"
                        f"_Tendance en cours : tenir pour surfer, ou verrouiller le gain ?_"
                    )

            ac = autoclose.get(self.bot_id)
            if ac.get("active") and avg > 0:
                thr = float(ac.get("threshold_pct", 0) or 0)
                if ac.get("mode") == "take_profit":
                    gain = (live_price - avg) / avg * 100
                    if thr > 0 and gain >= thr:
                        meta = {"gain_pct": round(gain, 2), "threshold_pct": thr}
                        tp_sig = Signal(
                            "sell", 0.95,
                            f"TAKE-PROFIT reglable : +{gain:.1f}% (seuil +{thr:.0f}%)",
                            self.symbol, meta)
                        await self._exit(live_price, pos, tp_sig)
                        return
                elif self._peak_price > 0:                       # mode trailing
                    drop = (self._peak_price - live_price) / self._peak_price * 100
                    if thr > 0 and drop >= thr:
                        meta = {"peak": round(self._peak_price, 6),
                                "drop_pct": round(drop, 2), "threshold_pct": thr}
                        tr_sig = Signal(
                            "sell", 0.95,
                            f"TRAILING reglable : -{drop:.1f}% depuis pic "
                            f"{self._peak_price:.4f} (seuil {thr:.0f}%)",
                            self.symbol, meta)
                        await self._exit(live_price, pos, tr_sig)
                        return

            if TREND_TRAIL_PCT > 0 and self._peak_price > 0:
                drop = (self._peak_price - live_price) / self._peak_price
                if drop >= TREND_TRAIL_PCT:
                    meta = {"peak": round(self._peak_price, 6),
                            "drop_pct": round(drop * 100, 2),
                            "trail_pct": round(TREND_TRAIL_PCT * 100, 2)}
                    trail_sig = Signal(
                        "sell", 0.95,
                        f"TRAILING-STOP : -{drop * 100:.1f}% depuis pic "
                        f"{self._peak_price:.4f} (seuil {TREND_TRAIL_PCT * 100:.0f}%)",
                        self.symbol, meta)
                    await self._exit(live_price, pos, trail_sig)
                    return
        else:
            self._peak_price   = 0.0
            self._gain_alerted = False   # re-arme l'alerte pour la prochaine position

        if sig.action == "buy" and not have_pos:
            if trading_state.is_kill_switch_active():
                log.info("trend_entry_blocked_kill_switch", bot_id=self.bot_id,
                         reason=trading_state.get_kill_reason())
                return
            # Grace de demarrage : tant que le preflight/Director n'a pas confirme
            # que le marche n'est pas en Extreme Fear, on ne prend pas de nouvelle
            # position (ferme le trou ~45s du boot). Ne gene jamais les sorties.
            grace = trading_state.entry_grace_remaining()
            if grace > 0:
                log.info("trend_entry_blocked_boot_grace", bot_id=self.bot_id,
                         remaining_s=round(grace, 1))
                return
            # Filtre de regime : on n'ouvre les ALTS que si le marche directeur (BTC)
            # est haussier. Les bots BTC ne se filtrent pas eux-memes. OFF par defaut.
            if (REGIME_FILTER_ENABLED and self.symbol != REGIME_SYMBOL
                    and not await self._regime_is_bullish()):
                log.info("trend_entry_blocked_regime", bot_id=self.bot_id,
                         regime=REGIME_SYMBOL)
                return
            await self._enter(live_price, sig)
        elif sig.action == "sell" and have_pos:
            await self._exit(live_price, pos, sig)
        else:
            log.debug("trend_bot_hold", bot_id=self.bot_id,
                      action=sig.action, have_pos=have_pos)

    async def _regime_is_bullish(self) -> bool:
        """True si l'actif directeur (BTC) est haussier (prix > sa SMA daily). Filtre
        macro pour les alts. Fail-open : erreur / donnees insuffisantes -> True."""
        try:
            from strategies.trend_daily import fetch_daily_closes, TREND_SMA_PERIOD
            closes = await fetch_daily_closes(REGIME_SYMBOL)
            if len(closes) < TREND_SMA_PERIOD:
                return True
            sma   = sum(closes[-TREND_SMA_PERIOD:]) / TREND_SMA_PERIOD
            price = await self._coinbase.get_price(REGIME_SYMBOL)
            return price >= sma
        except Exception as exc:
            log.debug("regime_check_failed", error=str(exc))
            return True

    # ── Entree / sortie ──────────────────────────────────────────────────────
    async def _enter(self, price: float, sig) -> None:
        snap         = await self._coinbase.get_portfolio_snapshot()
        total_usdc   = snap.get("total_usdc", 0.0)
        free_usdc    = snap.get("usdc_balance", 0.0)
        spend        = min(total_usdc * TREND_POSITION_PCT, free_usdc * 0.95)

        # Cap d'exposition combinee : deploye = total - cash (positions de TOUS les
        # bots + dust). On ne laisse pas l'exposition depasser le cap -> on rogne la
        # mise sur la marge restante (room). room <= 0 -> mise nulle -> skip propre.
        if RISK_MAX_COMBINED_EXPOSURE_PCT < 1.0 and total_usdc > 0:
            deployed = max(0.0, total_usdc - free_usdc)
            room     = RISK_MAX_COMBINED_EXPOSURE_PCT * total_usdc - deployed
            if room < spend:
                spend = max(0.0, room)
                if spend < TREND_MIN_USDC:
                    log.info("trend_bot_entry_blocked_exposure_cap", bot_id=self.bot_id,
                             deployed_pct=round(deployed / total_usdc, 3),
                             cap=RISK_MAX_COMBINED_EXPOSURE_PCT)

        if spend < TREND_MIN_USDC:
            log.info("trend_bot_entry_skipped", bot_id=self.bot_id,
                     reason="mise trop faible", spend=round(spend, 2),
                     free_usdc=round(free_usdc, 2))
            return

        qty = spend / price
        try:
            order = await self._coinbase.place_order(self.symbol, "buy", qty)  # maker si active globalement
        except Exception as exc:
            log.error("trend_bot_buy_failed", bot_id=self.bot_id, error=str(exc))
            await notifier.notify(f"❌ *Trend {self.symbol}* — achat échoué\n`{exc}`")
            return

        self._last_trade_ts = time.time()
        self._peak_price    = price   # amorce le trailing-stop au prix d'entree
        self._memory.record_decision(
            role="trend_bot", task_type="order", symbol=self.symbol, action="buy",
            confidence=sig.confidence, reasoning=f"TREND ENTRY : {sig.reasoning}",
            metadata=f'{{"order_id":"{order.order_id}","price":{round(price,4)},"qty":{round(qty,8)}}}',
        )
        # Marqueur explicite de la 1re transaction live (idempotent) : borne robuste de
        # l'ere live pour le track record, sans dependre du seuil PAPER_LIVE_SPLIT (audit [27]).
        if getattr(self._coinbase, "mode", "paper") == "live":
            try:
                self._memory.mark_first_live_trade()
            except Exception as exc:
                log.debug("mark_first_live_failed", error=str(exc))
        self._memory.record_snapshot(await self._coinbase.get_portfolio_snapshot())
        await notifier.notify(
            f"📈 *TREND — Entrée* `{self.symbol}`\n"
            f"Achat `{qty:.6f}` @ `{price:,.4f}` (`{spend:.2f}` USDC)\n"
            f"_{sig.reasoning}_"
        )
        log.info("trend_bot_entered", bot_id=self.bot_id, qty=round(qty, 8), price=price)

    async def _exit(self, price: float, pos: dict, sig) -> None:
        qty       = pos["qty"]
        avg_price = pos.get("avg_price", price)
        try:
            order = await self._coinbase.place_order(self.symbol, "sell", qty, force=True)
        except UnsellableDustError as exc:
            # Residu sous le pas de cotation : economiquement clos, mais Coinbase
            # refusera toujours l'ordre. On le solde localement, sinon la sortie
            # est retentee (et loguee en erreur) a chaque cycle, pour toujours.
            self._coinbase.forget_position(self.symbol)
            log.info("trend_bot_dust_dropped", bot_id=self.bot_id,
                     qty=round(qty, 8), reason=str(exc))
            return
        except Exception as exc:
            log.error("trend_bot_sell_failed", bot_id=self.bot_id, error=str(exc))
            await notifier.notify(f"❌ *Trend {self.symbol}* — sortie échouée\n`{exc}`")
            return

        pnl_pct = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0
        # P&L NET en USDC : brut - frais taker aller-retour (achat + vente)
        gross_usdc = qty * (price - avg_price)
        fee_usdc   = TAKER_FEE_PCT * qty * (avg_price + price)
        net_usdc   = gross_usdc - fee_usdc
        self._gain_alerted  = False   # re-arme l'alerte gain
        self._last_trade_ts = time.time()
        self._memory.record_decision(
            role="trend_bot", task_type="order", symbol=self.symbol, action="sell",
            confidence=sig.confidence, reasoning=f"TREND EXIT : {sig.reasoning}",
            metadata=f'{{"order_id":"{order.order_id}","price":{round(price,4)},"qty":{round(qty,8)}}}',
        )
        self._memory.record_snapshot(await self._coinbase.get_portfolio_snapshot())
        emoji = "🟢" if net_usdc >= 0 else "🔴"
        await notifier.notify(
            f"📉 *TREND — Sortie* `{self.symbol}`\n"
            f"Vente `{qty:.6f}` @ `{price:,.4f}`\n"
            f"P&L {emoji} `{pnl_pct:+.1f}%` = `{net_usdc:+.2f}` USDC net _(frais `{fee_usdc:.2f}`)_\n"
            f"_{sig.reasoning}_"
        )
        log.info("trend_bot_exited", bot_id=self.bot_id, qty=round(qty, 8),
                 price=price, pnl_pct=round(pnl_pct, 2))
