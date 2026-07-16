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
        self._peak_price:    float = 0.0   # plus haut depuis l'entree (trailing-stop)

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
            self._peak_price = 0.0

        if sig.action == "buy" and not have_pos:
            if trading_state.is_kill_switch_active():
                log.info("trend_entry_blocked_kill_switch", bot_id=self.bot_id,
                         reason=trading_state.get_kill_reason())
                return
            await self._enter(live_price, sig)
        elif sig.action == "sell" and have_pos:
            await self._exit(live_price, pos, sig)
        else:
            log.debug("trend_bot_hold", bot_id=self.bot_id,
                      action=sig.action, have_pos=have_pos)

    # ── Entree / sortie ──────────────────────────────────────────────────────
    async def _enter(self, price: float, sig) -> None:
        snap         = await self._coinbase.get_portfolio_snapshot()
        total_usdc   = snap.get("total_usdc", 0.0)
        free_usdc    = snap.get("usdc_balance", 0.0)
        spend        = min(total_usdc * TREND_POSITION_PCT, free_usdc * 0.95)

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
        self._last_trade_ts = time.time()
        self._memory.record_decision(
            role="trend_bot", task_type="order", symbol=self.symbol, action="sell",
            confidence=sig.confidence, reasoning=f"TREND EXIT : {sig.reasoning}",
            metadata=f'{{"order_id":"{order.order_id}","price":{round(price,4)},"qty":{round(qty,8)}}}',
        )
        self._memory.record_snapshot(await self._coinbase.get_portfolio_snapshot())
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        await notifier.notify(
            f"📉 *TREND — Sortie* `{self.symbol}`\n"
            f"Vente `{qty:.6f}` @ `{price:,.4f}` | P&L {emoji} `{pnl_pct:+.1f}%`\n"
            f"_{sig.reasoning}_"
        )
        log.info("trend_bot_exited", bot_id=self.bot_id, qty=round(qty, 8),
                 price=price, pnl_pct=round(pnl_pct, 2))
