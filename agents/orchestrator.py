"""
Orchestrator - chef d'orchestre du bot de trading.

Boucle principale :
  1. Warm-up instantane au demarrage (prefetch historique)
  2. Verifie stop-loss / take-profit sur position ouverte
  3. Envoie control -> MarketAgent
  4. Recoit artifact (signal + prix + RSI)
  5. Passe le signal au RiskAgent
  6. Si approuve -> place_order via CoinbaseClient
  7. Persiste tout via MemoryAgent
  8. Notifie via Telegram si configure
  9. Attend LOOP_INTERVAL_S secondes
"""
from __future__ import annotations

import asyncio
import os
import time

import structlog
from dotenv import load_dotenv

from agents import trading_state
from agents.market_agent  import MarketAgent
from agents.memory_agent  import MemoryAgent
from agents.risk_agent    import RiskAgent
from interfaces.coinbase_client import CoinbaseClient
from interfaces import notifier

load_dotenv()
log = structlog.get_logger()

SYMBOL              = os.getenv("TRADING_SYMBOL",          "BTC-USDC")
LOOP_INTERVAL_S     = int(os.getenv("LOOP_INTERVAL_S",      "60"))
STOP_LOSS_PCT       = float(os.getenv("RISK_STOP_LOSS_PCT",      "0.03"))  # 3%
TAKE_PROFIT_PCT     = float(os.getenv("RISK_TAKE_PROFIT_PCT",    "0.05"))  # 5%
SIGNAL_CONFIRM      = int(os.getenv("SIGNAL_CONFIRM_TICKS",      "2"))     # ticks de confirmation
TRADE_COOLDOWN_S    = int(os.getenv("TRADE_COOLDOWN_S",          "300"))   # 5 min entre trades

# Jalons de chauffe (uniquement si warm-up progressif, pas prefetch)
_WARMUP_MILESTONES: frozenset[int] = frozenset({5, 10, 15, 20, 22})


class Orchestrator:
    """Coordonne les agents via messages MCP et execute les ordres paper."""

    def __init__(self) -> None:
        self._market   = MarketAgent(symbol=SYMBOL)
        self._risk     = RiskAgent()
        self._memory   = MemoryAgent()
        self._coinbase = CoinbaseClient()

        # Etat interne : confirmation de signal + cooldown
        self._signal_streak: dict = {"action": None, "count": 0}
        self._last_trade_ts: float = 0.0

        log.info("orchestrator_ready", symbol=SYMBOL, interval_s=LOOP_INTERVAL_S,
                 stop_loss=f"{STOP_LOSS_PCT:.0%}", take_profit=f"{TAKE_PROFIT_PCT:.0%}",
                 confirm_ticks=SIGNAL_CONFIRM, cooldown_s=TRADE_COOLDOWN_S)

    # ─────────────────────────────────────────────────────────────────────────
    # Stop-loss / Take-profit
    # ─────────────────────────────────────────────────────────────────────────

    async def _check_sl_tp(self, price: float) -> bool:
        """
        Verifie si une position ouverte a atteint le stop-loss ou take-profit.
        Si oui, execute la vente forcee et retourne True (tick termine).
        """
        pos = self._coinbase.get_position(SYMBOL)
        if not pos:
            return False

        entry  = pos["avg_price"]
        qty    = pos["qty"]
        pct    = (price - entry) / entry

        if pct <= -STOP_LOSS_PCT:
            reason = f"STOP-LOSS declenche ({pct:+.2%} depuis {entry:,.2f} USDC)"
            log.warning("stop_loss_triggered", pct=round(pct, 4), entry=entry, price=price)
            await notifier.notify(
                f"🛑 *STOP-LOSS*\n"
                f"`{pct:+.2%}` depuis entree `{entry:,.2f}` USDC\n"
                f"Vente forcee : `{qty:.6f}` BTC @ `{price:,.2f}` USDC"
            )
            await self._force_sell(qty, price, reason)
            return True

        if pct >= TAKE_PROFIT_PCT:
            reason = f"TAKE-PROFIT declenche ({pct:+.2%} depuis {entry:,.2f} USDC)"
            log.info("take_profit_triggered", pct=round(pct, 4), entry=entry, price=price)
            await notifier.notify(
                f"💰 *TAKE-PROFIT*\n"
                f"`{pct:+.2%}` depuis entree `{entry:,.2f}` USDC\n"
                f"Vente : `{qty:.6f}` BTC @ `{price:,.2f}` USDC"
            )
            await self._force_sell(qty, price, reason)
            return True

        return False

    async def _force_sell(self, qty: float, price: float, reason: str) -> None:
        """Execute une vente forcee (SL ou TP) et persiste la decision."""
        try:
            order = await self._coinbase.place_order(SYMBOL, "sell", qty)
            pnl   = qty * (price - self._coinbase._paper.positions.get(
                SYMBOL, {}).get("avg_price", price))

            self._memory.record_decision(
                role="orchestrator",
                task_type="order",
                symbol=SYMBOL,
                action="sell",
                confidence=1.0,
                reasoning=reason,
                metadata=f'{{"order_id":"{order.order_id}","price":{round(price,2)},"qty":{round(qty,6)}}}',
            )
            new_snap = await self._coinbase.get_portfolio_snapshot()
            self._memory.record_snapshot(new_snap)
            log.info("force_sell_executed", qty=round(qty, 6), price=round(price, 2))
        except Exception as exc:
            log.error("force_sell_failed", error=str(exc))
            await notifier.notify(f"❌ *Erreur vente forcee*\n`{exc}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Cycle principal
    # ─────────────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Un cycle complet : SL/TP -> signal -> risque -> ordre -> snapshot."""

        # ── 0. Verifier la pause ──────────────────────────────────────────────
        if trading_state.is_paused():
            log.info("tick_paused", symbol=SYMBOL)
            return

        # ── 1. Control -> MarketAgent ─────────────────────────────────────────
        ctrl = {
            "node_type":    "control",
            "sender":       "orchestrator",
            "receiver":     "market_agent",
            "payload":      {"symbol": SYMBOL},
            "timeout_ms":   5000,
            "retry_budget": 2,
        }
        self._memory.record_mcp_message(ctrl)

        # ── 2. Recuperer signal ───────────────────────────────────────────────
        market_artifact = await self._market.run()
        self._memory.record_mcp_message(market_artifact)

        if market_artifact["node_type"] == "error":
            err = market_artifact["payload"].get("error", "inconnu")
            log.warning("market_agent_failed", error=err)
            await notifier.notify(f"❌ *Erreur MarketAgent*\n`{err}`")
            return

        payload     = market_artifact["payload"]
        signal      = payload["signal"]
        price       = payload["price"]
        history_len = payload.get("history_len", 0)

        # ── 3. Verifier stop-loss / take-profit ───────────────────────────────
        sl_tp_triggered = await self._check_sl_tp(price)
        if sl_tp_triggered:
            return

        # ── 4. Notifications de chauffe (warm-up progressif seulement) ────────
        if history_len in _WARMUP_MILESTONES:
            if history_len < 22:
                await notifier.notify(
                    f"🔄 *Warm-up* : `{history_len}/22` prix collectes..."
                )
            else:
                await notifier.notify(
                    f"✅ *Warm-up termine* - trading actif !\n"
                    f"Symbole : `{SYMBOL}` | Prix : `{price:,.2f} USDC`"
                )

        # ── 5. Enregistrer le signal ──────────────────────────────────────────
        self._memory.record_decision(
            role="market_agent",
            task_type="signal",
            symbol=SYMBOL,
            action=signal["action"],
            confidence=signal["confidence"],
            reasoning=signal["reasoning"],
            metadata=str(signal.get("metadata", {})),
        )

        if signal["action"] == "hold":
            self._signal_streak = {"action": None, "count": 0}
            log.info("tick_hold", symbol=SYMBOL, reasoning=signal["reasoning"][:60])
            return

        # ── 6. Confirmation sur N ticks consecutifs ───────────────────────────
        if signal["action"] == self._signal_streak["action"]:
            self._signal_streak["count"] += 1
        else:
            self._signal_streak = {"action": signal["action"], "count": 1}

        if self._signal_streak["count"] < SIGNAL_CONFIRM:
            log.info(
                "signal_pending_confirmation",
                action=signal["action"],
                tick=self._signal_streak["count"],
                required=SIGNAL_CONFIRM,
            )
            return   # attend le prochain tick

        # ── 6b. Cooldown post-trade ────────────────────────────────────────────
        elapsed = time.time() - self._last_trade_ts
        if self._last_trade_ts > 0 and elapsed < TRADE_COOLDOWN_S:
            remaining = int(TRADE_COOLDOWN_S - elapsed)
            log.info("trade_cooldown_active", remaining_s=remaining)
            return

        # ── 7. Notification signal actionnable ────────────────────────────────
        rsi_val = signal.get("metadata", {}).get("rsi", "?")
        emoji   = "📈" if signal["action"] == "buy" else "📉"
        await notifier.notify(
            f"{emoji} *Signal {signal['action'].upper()}* - `{SYMBOL}`\n"
            f"Prix : `{price:,.2f} USDC` | RSI : `{rsi_val}`\n"
            f"Confiance : `{signal['confidence']:.0%}`\n"
            f"_{signal['reasoning'][:120]}_"
        )

        # ── 7. Evaluation du risque ───────────────────────────────────────────
        snapshot    = await self._coinbase.get_portfolio_snapshot()
        last_action = self._memory.get_last_action_for_symbol(SYMBOL)

        risk_artifact = self._risk.evaluate(
            signal_action=signal["action"],
            signal_confidence=signal["confidence"],
            portfolio_usdc=snapshot["usdc_balance"],
            price=price,
            last_action=last_action,
        )
        self._memory.record_mcp_message(risk_artifact)
        risk_payload = risk_artifact["payload"]

        # ── 8. Decision de risque ─────────────────────────────────────────────
        self._memory.record_decision(
            role="risk_agent",
            task_type="order",
            symbol=SYMBOL,
            action=signal["action"] if risk_payload["approved"] else "rejected",
            confidence=signal["confidence"],
            reasoning="; ".join(risk_payload["reasons"]) or "Approuve",
        )

        if not risk_payload["approved"]:
            reasons_txt = "\n".join(f"- {r}" for r in risk_payload["reasons"])
            log.info("order_rejected", reasons=risk_payload["reasons"])
            await notifier.notify(f"⚠️ *Ordre rejete*\n{reasons_txt}")
            return

        # ── 9. Executer l'ordre paper ─────────────────────────────────────────
        try:
            qty = risk_payload["qty"]

            if signal["action"] == "sell":
                pos = snapshot["positions"].get(SYMBOL)
                if not pos:
                    log.warning("no_position_to_sell", symbol=SYMBOL)
                    await notifier.notify(f"⚠️ *Vente impossible* - aucune position `{SYMBOL}`")
                    return
                qty = pos["qty"]

            order = await self._coinbase.place_order(SYMBOL, signal["action"], qty)
            cost  = qty * price

            self._memory.record_decision(
                role="orchestrator",
                task_type="order",
                symbol=SYMBOL,
                action=signal["action"],
                confidence=signal["confidence"],
                reasoning=f"Ordre execute : {signal['action']} {qty:.6f} {SYMBOL} @ {price:.2f} USDC",
                metadata=(
                    f'{{"order_id":"{order.order_id}",'
                    f'"price":{round(price, 2)},'
                    f'"qty":{round(qty, 6)}}}'
                ),
            )

            self._last_trade_ts = time.time()
            self._signal_streak = {"action": None, "count": 0}   # reset apres trade

            log.info("order_executed", side=signal["action"],
                     qty=round(qty, 6), price=round(price, 2), symbol=SYMBOL)

            await notifier.notify(
                f"✅ *Ordre execute*\n"
                f"`{signal['action'].upper()}` `{qty:.6f}` BTC @ `{price:,.2f}` USDC\n"
                f"Montant : `{cost:,.2f}` USDC\n"
                f"SL : `{price * (1 - STOP_LOSS_PCT):,.2f}` | "
                f"TP : `{price * (1 + TAKE_PROFIT_PCT):,.2f}`"
            )

            # ── 10. Snapshot post-ordre ───────────────────────────────────────
            new_snapshot = await self._coinbase.get_portfolio_snapshot()
            self._memory.record_snapshot(new_snapshot)

        except Exception as exc:
            log.error("order_failed", error=str(exc), symbol=SYMBOL)
            self._memory.record_decision(
                role="orchestrator",
                task_type="alert",
                symbol=SYMBOL,
                action=None,
                confidence=0.0,
                reasoning=f"Erreur ordre : {exc}",
            )
            await notifier.notify(f"❌ *Erreur ordre*\n`{exc}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Boucle infinie
    # ─────────────────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Demarre la boucle principale. Warm-up prefetch au lancement."""

        # Warm-up instantane : charge l'historique avant le premier tick
        log.info("warmup_start", symbol=SYMBOL)
        await self._market.warmup_from_history()

        if self._market.is_warmed_up:
            await notifier.notify(
                f"✅ *Warm-up instantane* - `{len(self._market.price_history)}` prix charges\n"
                f"Symbole : `{SYMBOL}` | Trading actif des le 1er tick"
            )
        else:
            await notifier.notify(
                f"🔄 *Warm-up progressif* - historique insuffisant\n"
                f"Il faudra `{22 - len(self._market.price_history)}` ticks supplementaires"
            )

        log.info("orchestrator_loop_start", symbol=SYMBOL, interval_s=LOOP_INTERVAL_S)

        try:
            while True:
                try:
                    await self._tick()
                except Exception as exc:
                    log.error("tick_unhandled_error", error=str(exc))
                await asyncio.sleep(LOOP_INTERVAL_S)
        except asyncio.CancelledError:
            log.info("orchestrator_stopped")
            await self._coinbase.close()


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entree standalone
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    orc = Orchestrator()
    await orc.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
