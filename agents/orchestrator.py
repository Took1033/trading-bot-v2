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

DEFAULT_SYMBOL      = os.getenv("TRADING_SYMBOL",          "BTC-USDC")
LOOP_INTERVAL_S     = int(os.getenv("LOOP_INTERVAL_S",      "60"))
STOP_LOSS_PCT       = float(os.getenv("RISK_STOP_LOSS_PCT",      "0.03"))  # -3%
TAKE_PROFIT_PCT     = float(os.getenv("RISK_TAKE_PROFIT_PCT",    "0.05"))  # +5%
TRAILING_STOP_PCT   = float(os.getenv("RISK_TRAILING_STOP_PCT",  "0.02"))  # -2% depuis peak
TRAILING_ACTIVATE   = float(os.getenv("RISK_TRAILING_ACTIVATE",  "0.02"))  # active a +2% au-dessus entree
SIGNAL_CONFIRM      = int(os.getenv("SIGNAL_CONFIRM_TICKS",      "2"))     # ticks de confirmation
TRADE_COOLDOWN_S    = int(os.getenv("TRADE_COOLDOWN_S",          "300"))   # 5 min entre trades

# Frais Coinbase Advanced Trade : 0.60% taker (market orders IOC)
# Round-trip = 2 × taker = 1.20%. P&L net = P&L brut - 1.20%
COINBASE_TAKER_FEE_PCT = float(os.getenv("COINBASE_TAKER_FEE_PCT", "0.006"))
ROUND_TRIP_FEE_PCT     = COINBASE_TAKER_FEE_PCT * 2

# ATR-based SL/TP : si actif, on remplace les valeurs fixes par des multiples d'ATR
ATR_BASED_SLTP   = os.getenv("ATR_BASED_SLTP", "true").lower() in ("true", "1", "yes")
ATR_SL_MULT      = float(os.getenv("ATR_SL_MULT", "1.5"))   # SL = 1.5 * ATR
ATR_TP_MULT      = float(os.getenv("ATR_TP_MULT", "2.5"))   # TP = 2.5 * ATR (R/R 1:1.67)
ATR_SL_MIN_PCT   = float(os.getenv("ATR_SL_MIN_PCT", "0.015"))  # plancher SL = 1.5%
ATR_SL_MAX_PCT   = float(os.getenv("ATR_SL_MAX_PCT", "0.05"))   # plafond SL = 5%
# Plancher TP : en faible volatilite le TP ATR peut tomber sous les frais aller-retour
# (1.2%), rendant le take-profit perdant net. On garantit un TP net positif.
ATR_TP_MIN_PCT   = float(os.getenv("ATR_TP_MIN_PCT", "0.025"))  # plancher TP = 2.5% (net ~+1.3%)

# Garde-fou anti-churn : une vente SUR SIGNAL n'est autorisee que si le gain brut
# couvre les frais aller-retour + cette marge nette. Sinon on garde la position
# (le stop-loss protege le bas). Empeche les aller-retours perdants sur micro-mouvements.
SELL_MIN_NET_PROFIT_PCT = float(os.getenv("SELL_MIN_NET_PROFIT_PCT", "0.003"))  # +0.3% net mini

# Filtre d'entree fee-aware : on n'ouvre un BUY que si la volatilite (ATR) du marche
# peut produire une sortie rentable. Le TP ATR vaut ATR_TP_MULT * atr ; pour qu'il
# couvre l'aller-retour de frais + une marge, il faut :
#     ATR_TP_MULT * atr_frac >= ROUND_TRIP_FEE_PCT + ENTRY_MIN_NET_MARGIN_PCT
# Sinon le marche est trop plat : meme en touchant le TP on ne bat pas les frais,
# donc on n'entre pas (evite les trades condamnes a churner a perte).
ENTRY_MIN_NET_MARGIN_PCT = float(os.getenv("ENTRY_MIN_NET_MARGIN_PCT", "0.003"))  # +0.3% net vise

# Protection : ferme une position ouverte depuis > X heures (zombie protection)
POSITION_MAX_AGE_H   = float(os.getenv("POSITION_MAX_AGE_H", "24"))

# Protection : pause si mouvement brutal (> X% en 1 tick = flash crash / pump)
FLASH_MOVE_THRESHOLD = float(os.getenv("FLASH_MOVE_THRESHOLD", "0.05"))   # 5%
FLASH_PAUSE_S        = int(os.getenv("FLASH_PAUSE_S", "180"))             # 3 min

# Daily loss limit PAR BOT : un bot qui perd > X% sur 24h est mis en pause auto
BOT_MAX_DAILY_LOSS_PCT = float(os.getenv("BOT_MAX_DAILY_LOSS_PCT", "0.05"))   # 5%

# Jalons de chauffe (uniquement si warm-up progressif, pas prefetch)
_WARMUP_MILESTONES: frozenset[int] = frozenset({5, 10, 15, 20, 22})


class Orchestrator:
    """Coordonne les agents via messages MCP et execute les ordres paper."""

    def __init__(
        self,
        symbol:        str = DEFAULT_SYMBOL,
        bot_id:        str = "main",
        weight:        float = 1.0,
        coinbase:      CoinbaseClient | None = None,
        memory:        MemoryAgent | None    = None,
    ) -> None:
        self.symbol = symbol
        self.bot_id = bot_id
        self.weight = weight

        self._market   = MarketAgent(symbol=symbol)
        self._risk     = RiskAgent()
        # Partage CoinbaseClient et MemoryAgent entre bots (meme portefeuille + meme DB)
        self._coinbase = coinbase or CoinbaseClient()
        self._memory   = memory   or MemoryAgent()

        # Etat interne : confirmation de signal + cooldown + peaks
        self._signal_streak: dict             = {"action": None, "count": 0}
        self._last_trade_ts: float            = 0.0
        self._position_peak: dict[str, float] = {}   # symbol -> prix max depuis l'entree
        # SL/TP par position (calcules dynamiquement via ATR au moment du BUY)
        self._position_sltp: dict[str, dict]  = {}   # symbol -> {sl_pct, tp_pct, atr_pct}
        # Timestamp d'ouverture des positions (pour position max age)
        self._position_opened_ts: dict[str, float] = {}
        # Dernier prix vu (pour detecter mouvements brutaux)
        self._last_price: float | None        = None
        self._flash_pause_until: float        = 0.0
        # P&L cumule sur 24h glissantes (somme des pnl realises par bot)
        self._daily_pnl_usdc:  float          = 0.0
        self._daily_pnl_reset: float          = time.time()
        self._daily_initial:   float | None   = None

        log.info("orchestrator_ready", symbol=self.symbol, bot_id=self.bot_id,
                 weight=self.weight, interval_s=LOOP_INTERVAL_S,
                 stop_loss=f"{STOP_LOSS_PCT:.0%}", take_profit=f"{TAKE_PROFIT_PCT:.0%}",
                 trailing=f"{TRAILING_STOP_PCT:.0%} (active +{TRAILING_ACTIVATE:.0%})",
                 confirm_ticks=SIGNAL_CONFIRM, cooldown_s=TRADE_COOLDOWN_S)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers notification : auto-prefixe avec [BOT_ID]
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def _tag(self) -> str:
        """Tag court pour identifier le bot dans les notifs Telegram."""
        return f"`[{self.bot_id.upper()[:3]}]`"

    async def _notify(self, text: str) -> bool:
        """Envoie une notif Telegram prefixe avec [BTC]/[ETH]/[SOL]/[DYN]."""
        return await notifier.notify(f"{self._tag} {text}")

    # ─────────────────────────────────────────────────────────────────────────
    # Changement de paire à chaud (P1)
    # ─────────────────────────────────────────────────────────────────────────

    async def switch_symbol(self, new_symbol: str) -> None:
        """
        Change la paire tradée par ce bot. Recrée le MarketAgent, réinitialise
        l'état de position et relance un warm-up. La boucle run_forever() en cours
        utilisera la nouvelle paire dès le prochain tick.
        """
        old = self.symbol
        self.symbol = new_symbol
        self._market = MarketAgent(symbol=new_symbol)
        # Reset des états liés à l'ancienne position
        self._signal_streak     = {"action": None, "count": 0}
        self._last_price        = None
        self._flash_pause_until = 0.0
        self._position_peak.pop(old, None)
        self._position_sltp.pop(old, None)
        self._position_opened_ts.pop(old, None)
        await self._market.warmup_from_history()
        log.info("orchestrator_symbol_switched", bot_id=self.bot_id,
                 old=old, new=new_symbol, warmed=self._market.is_warmed_up)

    # ─────────────────────────────────────────────────────────────────────────
    # Stop-loss / Take-profit
    # ─────────────────────────────────────────────────────────────────────────

    async def _check_sl_tp(self, price: float) -> bool:
        """
        Verifie SL fixe / TP fixe / trailing stop sur la position ouverte.
        Si l'un declenche, execute la vente forcee et retourne True.
        """
        pos = self._coinbase.get_position(self.symbol)
        if not pos:
            # Plus de position - on nettoie tous les etats
            self._position_peak.pop(self.symbol, None)
            self._position_sltp.pop(self.symbol, None)
            self._position_opened_ts.pop(self.symbol, None)
            return False

        entry = pos["avg_price"]
        qty   = pos["qty"]
        pct   = (price - entry) / entry

        # ── 0. Protection MAX AGE (zombie position > X heures) ──────────────
        opened_ts = self._position_opened_ts.get(self.symbol)
        if opened_ts:
            age_h = (time.time() - opened_ts) / 3600
            if age_h > POSITION_MAX_AGE_H:
                reason = f"MAX-AGE position ouverte depuis {age_h:.1f}h > {POSITION_MAX_AGE_H}h"
                log.warning("position_max_age", age_h=round(age_h, 2),
                            symbol=self.symbol, pct=round(pct, 4))
                await self._notify(
                    f"⏰ *POSITION AGE* — `{self.symbol}`\n"
                    f"Ouverte depuis `{age_h:.1f}h` > max `{POSITION_MAX_AGE_H}h`\n"
                    f"P&L actuel : `{pct:+.2%}` — fermeture automatique"
                )
                await self._force_sell(qty, price, reason)
                return True

        # Track du peak
        peak = self._position_peak.get(self.symbol, entry)
        if price > peak:
            peak = price
            self._position_peak[self.symbol] = peak

        base = self.symbol.split("-")[0]   # "BTC" depuis "BTC-USDC"
        net  = lambda p: p - ROUND_TRIP_FEE_PCT   # P&L net de frais

        # ── Resolution SL/TP : per-position (ATR) si dispo, sinon globaux ────
        sltp     = self._position_sltp.get(self.symbol, {})
        sl_pct   = sltp.get("sl_pct", STOP_LOSS_PCT)
        tp_pct   = sltp.get("tp_pct", TAKE_PROFIT_PCT)

        # ── 1. Stop-loss fixe ────────────────────────────────────────────────
        if pct <= -sl_pct:
            reason = f"STOP-LOSS ({pct:+.2%} depuis {entry:,.2f})"
            log.warning("stop_loss_triggered", pct=round(pct, 4), entry=entry, price=price)
            await self._notify(
                f"🛑 *STOP-LOSS* — `{self.symbol}`\n"
                f"`{pct:+.2%}` brut | `{net(pct):+.2%}` net depuis entrée `{entry:,.2f}`\n"
                f"Vente : `{qty:.6f}` {base} @ `{price:,.2f}` USDC"
            )
            await self._force_sell(qty, price, reason)
            return True

        # ── 2. Take-profit fixe (cap haut) ───────────────────────────────────
        if pct >= tp_pct:
            reason = f"TAKE-PROFIT ({pct:+.2%} depuis {entry:,.2f})"
            log.info("take_profit_triggered", pct=round(pct, 4), entry=entry, price=price)
            await self._notify(
                f"💰 *TAKE-PROFIT* — `{self.symbol}`\n"
                f"`{pct:+.2%}` brut | `{net(pct):+.2%}` net depuis entrée `{entry:,.2f}`\n"
                f"Vente : `{qty:.6f}` {base} @ `{price:,.2f}` USDC"
            )
            await self._force_sell(qty, price, reason)
            return True

        # ── 3. Trailing stop (actif uniquement au-dessus du seuil) ───────────
        if peak >= entry * (1 + TRAILING_ACTIVATE):
            trailing_stop = peak * (1 - TRAILING_STOP_PCT)
            if price <= trailing_stop:
                drop_from_peak = (price - peak) / peak
                reason = (
                    f"TRAILING-STOP (peak={peak:,.2f}, drop={drop_from_peak:+.2%}, "
                    f"P&L={pct:+.2%})"
                )
                log.info("trailing_stop_triggered",
                         peak=peak, price=price, pct=round(pct, 4))
                await self._notify(
                    f"📍 *TRAILING-STOP* — `{self.symbol}`\n"
                    f"Peak  : `{peak:,.2f}` | Prix : `{price:,.2f}` (`{drop_from_peak:+.2%}` depuis peak)\n"
                    f"P&L   : `{pct:+.2%}` brut | `{net(pct):+.2%}` net depuis `{entry:,.2f}`\n"
                    f"Vente : `{qty:.6f}` {base}"
                )
                await self._force_sell(qty, price, reason)
                return True

        return False

    async def _force_sell(self, qty: float, price: float, reason: str) -> None:
        """Execute une vente forcee (SL ou TP) et persiste la decision."""
        try:
            # Lire avg_price AVANT place_order (qui supprime la position en live)
            pos_before = self._coinbase.get_position(self.symbol)
            avg_price  = pos_before["avg_price"] if pos_before else price
            order = await self._coinbase.place_order(self.symbol, "sell", qty, force=True)
            pnl   = qty * (price - avg_price)
            pnl_pct = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0

            # Track P&L cumule sur 24h (pour daily loss limit par bot)
            self._daily_pnl_usdc += pnl

            # Post-mortem Claude Haiku (asynchrone, ne bloque pas)
            try:
                from interfaces.claude_client import analyze_closed_trade, ENABLED
                if ENABLED:
                    # Duree en min depuis le buy (approx via last_trade_ts)
                    dur_min = (time.time() - self._last_trade_ts) / 60 if self._last_trade_ts else 0
                    postmortem = await analyze_closed_trade(
                        symbol=self.symbol, side="sell",
                        entry=avg_price, exit=price,
                        pnl_pct=pnl_pct,
                        reason=reason.split(" ")[0],
                        duration_min=dur_min,
                    )
                    if postmortem:
                        await self._notify(f"🧠 _Post-mortem: {postmortem}_")
            except Exception as exc:
                log.debug("postmortem_skip", error=str(exc))

            self._memory.record_decision(
                role="orchestrator",
                task_type="order",
                symbol=self.symbol,
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
            await self._notify(f"❌ *Erreur vente forcee*\n`{exc}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Cycle principal
    # ─────────────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Un cycle complet : SL/TP -> signal -> risque -> ordre -> snapshot."""

        # ── 0. Verifier la pause ──────────────────────────────────────────────
        if trading_state.is_paused(self.bot_id):
            log.info("tick_paused", symbol=self.symbol)
            return

        # ── 0b. Daily loss limit PAR BOT (auto-pause si derive) ──────────────
        now = time.time()
        if now - self._daily_pnl_reset > 86400:   # reset 24h
            self._daily_pnl_usdc  = 0.0
            self._daily_pnl_reset = now
            self._daily_initial   = None

        if self._daily_initial and self._daily_initial > 0:
            daily_loss_pct = -self._daily_pnl_usdc / self._daily_initial
            if daily_loss_pct >= BOT_MAX_DAILY_LOSS_PCT:
                trading_state.pause(self.bot_id)
                log.warning("bot_daily_loss_pause",
                            bot_id=self.bot_id,
                            loss_pct=round(daily_loss_pct * 100, 2),
                            loss_usdc=round(self._daily_pnl_usdc, 2))
                await self._notify(
                    f"⏸️ *AUTO-PAUSE {self.bot_id.upper()}*\n"
                    f"Perte 24h : `{daily_loss_pct:+.2%}` "
                    f"(`{self._daily_pnl_usdc:+.2f}` USDC)\n"
                    f"Limite : `{BOT_MAX_DAILY_LOSS_PCT:.0%}` — bot en pause jusqu'au reset 24h ou /resume manuel"
                )
                return

        # ── 1. Control -> MarketAgent ─────────────────────────────────────────
        ctrl = {
            "node_type":    "control",
            "sender":       "orchestrator",
            "receiver":     "market_agent",
            "payload":      {"symbol": self.symbol},
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
            await self._notify(f"❌ *Erreur MarketAgent*\n`{err}`")
            return

        payload     = market_artifact["payload"]
        signal      = payload["signal"]
        price       = payload["price"]
        history_len = payload.get("history_len", 0)

        # ── 2b. Flash crash/pump detection ────────────────────────────────────
        if self._last_price and self._last_price > 0:
            move_pct = abs(price - self._last_price) / self._last_price
            if move_pct >= FLASH_MOVE_THRESHOLD:
                direction = "PUMP" if price > self._last_price else "CRASH"
                log.warning("flash_move_detected",
                            symbol=self.symbol, direction=direction,
                            move_pct=round(move_pct * 100, 2),
                            prev=self._last_price, curr=price)
                await self._notify(
                    f"⚡ *FLASH {direction}* — `{self.symbol}`\n"
                    f"Mouvement : `{move_pct*100:+.2f}%` en 1 tick\n"
                    f"Prix : `{self._last_price:.2f}` → `{price:.2f}` USDC\n"
                    f"Trading pausé `{FLASH_PAUSE_S}s` (anti-mauvaise execution)"
                )
                self._flash_pause_until = time.time() + FLASH_PAUSE_S
        self._last_price = price

        if time.time() < self._flash_pause_until:
            return   # encore en pause flash

        # ── 3. Verifier stop-loss / take-profit ───────────────────────────────
        sl_tp_triggered = await self._check_sl_tp(price)
        if sl_tp_triggered:
            return

        # ── 4. Notifications de chauffe (warm-up progressif seulement) ────────
        if history_len in _WARMUP_MILESTONES:
            if history_len < 22:
                await self._notify(
                    f"🔄 *Warm-up* : `{history_len}/22` prix collectes..."
                )
            else:
                await self._notify(
                    f"✅ *Warm-up termine* - trading actif !\n"
                    f"Symbole : `{self.symbol}` | Prix : `{price:,.2f} USDC`"
                )

        # ── 5. Enregistrer le signal ──────────────────────────────────────────
        self._memory.record_decision(
            role="market_agent",
            task_type="signal",
            symbol=self.symbol,
            action=signal["action"],
            confidence=signal["confidence"],
            reasoning=signal["reasoning"],
            metadata=str(signal.get("metadata", {})),
        )

        if signal["action"] == "hold":
            self._signal_streak = {"action": None, "count": 0}
            log.info("tick_hold", symbol=self.symbol, reasoning=signal["reasoning"][:60])
            return

        # ── 5b. Filtre d'entree fee-aware (BUY uniquement) ────────────────────
        # On rejette un achat si la volatilite du marche ne permet pas, meme en
        # touchant le TP ATR, de couvrir l'aller-retour de frais + la marge.
        # Empeche les entrees condamnees a churner a perte sur marche plat.
        if signal["action"] == "buy":
            atr_pct  = float(signal.get("metadata", {}).get("atr_pct", 0.0)) / 100.0
            tp_reach = ATR_TP_MULT * atr_pct
            min_move = ROUND_TRIP_FEE_PCT + ENTRY_MIN_NET_MARGIN_PCT
            if tp_reach < min_move:
                log.info("entry_blocked_low_volatility",
                         symbol=self.symbol,
                         atr_pct=round(atr_pct * 100, 3),
                         tp_reach_pct=round(tp_reach * 100, 3),
                         min_move_pct=round(min_move * 100, 3))
                self._signal_streak = {"action": None, "count": 0}
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

        # ── 7. Validation AI (Claude Haiku — optionnel, si cle configuree) ──────
        meta    = signal.get("metadata", {})
        rsi_val = meta.get("rsi", "?")
        ai_tag  = ""
        try:
            from agents.signal_validator import validate_signal, ENABLED as AI_ENABLED
            # On ne valide QUE les BUY. Un SELL est une sortie defensive (SL/TP,
            # retournement, sentiment) : on ne doit jamais le bloquer, sinon les
            # positions ne peuvent sortir qu'au stop-loss en cas de retournement.
            if AI_ENABLED and signal["action"] == "buy":
                ai_result = await validate_signal(
                    symbol=self.symbol,
                    action=signal["action"],
                    confidence=signal["confidence"],
                    price=price,
                    rsi=float(meta.get("rsi", 50.0)),
                    ema_fast=float(meta.get("ema_fast", price)),
                    ema_slow=float(meta.get("ema_slow", price)),
                    ema_trend=meta.get("ema_trend"),
                    vol_pct=float(meta.get("vol_pct", 0.0)),
                    reasoning=signal["reasoning"],
                )
                if not ai_result["approved"]:
                    log.info("signal_rejected_by_ai",
                             symbol=self.symbol, action=signal["action"],
                             commentary=ai_result["commentary"])
                    await self._notify(
                        f"🤖 *Signal {signal['action'].upper()} rejeté AI* — `{self.symbol}`\n"
                        f"_{ai_result['commentary']}_"
                    )
                    return
                signal = dict(signal)
                signal["confidence"] = ai_result["confidence"]
                ai_tag = " 🤖"
        except Exception:
            pass   # Validation AI facultative — ne bloque jamais le trading

        # ── 8. Evaluation du risque (avec correlation cap inter-bots) ─────────
        snapshot    = await self._coinbase.get_portfolio_snapshot()
        last_action = self._memory.get_last_action_for_symbol(self.symbol)

        # Init du capital de reference pour daily loss tracking
        if self._daily_initial is None:
            self._daily_initial = snapshot.get("total_usdc", snapshot["usdc_balance"])

        # ── 7c. Garde-fou anti-churn : une vente SUR SIGNAL ne doit jamais
        # realiser une perte nette. Le mouvement brut doit couvrir l'aller-retour
        # de frais + une marge. Les sorties SL/TP/trailing passent par
        # _check_sl_tp() en amont (return) et ne sont PAS concernees (protection bas).
        if signal["action"] == "sell":
            pos = snapshot.get("positions", {}).get(self.symbol)
            if pos and (pos.get("avg_price") or 0) > 0:
                entry      = pos["avg_price"]
                gross_gain = (price - entry) / entry
                min_gain   = ROUND_TRIP_FEE_PCT + SELL_MIN_NET_PROFIT_PCT
                if gross_gain < min_gain:
                    log.info("sell_blocked_unprofitable",
                             symbol=self.symbol,
                             gross_gain_pct=round(gross_gain * 100, 3),
                             min_gain_pct=round(min_gain * 100, 3),
                             entry=entry, price=price)
                    return   # on garde la position ; le stop-loss protege le bas

        # Calcul de l'exposition combinee actuelle (toutes positions valorisees)
        current_exposure = sum(
            p["qty"] * p["current_price"]
            for p in snapshot.get("positions", {}).values()
        )

        # ── 7b. Notification signal actionnable ───────────────────────────────
        emoji = "📈" if signal["action"] == "buy" else "📉"
        await self._notify(
            f"{emoji} *Signal {signal['action'].upper()}*{ai_tag} — `{self.symbol}`\n"
            f"Prix : `{price:,.2f} USDC` | RSI : `{rsi_val}`\n"
            f"Confiance : `{signal['confidence']:.0%}`\n"
            f"_{signal['reasoning'][:120]}_"
        )

        risk_artifact = self._risk.evaluate(
            signal_action=signal["action"],
            signal_confidence=signal["confidence"],
            portfolio_usdc=snapshot["usdc_balance"],
            price=price,
            last_action=last_action,
            total_portfolio=snapshot.get("total_usdc", snapshot["usdc_balance"]),
            current_exposure=current_exposure,
        )
        self._memory.record_mcp_message(risk_artifact)
        risk_payload = risk_artifact["payload"]

        # ── 8. Decision de risque ─────────────────────────────────────────────
        self._memory.record_decision(
            role="risk_agent",
            task_type="order",
            symbol=self.symbol,
            action=signal["action"] if risk_payload["approved"] else "rejected",
            confidence=signal["confidence"],
            reasoning="; ".join(risk_payload["reasons"]) or "Approuve",
        )

        if not risk_payload["approved"]:
            reasons     = risk_payload["reasons"]
            reasons_txt = "\n".join(f"- {r}" for r in reasons)
            log.info("order_rejected", reasons=reasons)
            # Rejets benins (etat normal du marche) : log seul, pas de spam Telegram.
            benign = ("Position deja longue", "Pas de position ouverte a vendre")
            is_benign = reasons and all(
                any(b in r for b in benign) for r in reasons
            )
            if not is_benign:
                await self._notify(f"⚠️ *Ordre rejete*\n{reasons_txt}")
            return

        # ── 9. Executer l'ordre paper ─────────────────────────────────────────
        try:
            qty = risk_payload["qty"]

            if signal["action"] == "sell":
                pos = snapshot["positions"].get(self.symbol)
                if not pos:
                    log.warning("no_position_to_sell", symbol=self.symbol)
                    await self._notify(f"⚠️ *Vente impossible* - aucune position `{self.symbol}`")
                    return
                qty = pos["qty"]

            order = await self._coinbase.place_order(self.symbol, signal["action"], qty)
            cost  = qty * price

            # ── Détection du PREMIER trade live (notif spéciale) ──────────────
            if self._coinbase.mode == "live":
                is_first = self._memory.get_first_live_trade_ts() is None
                if is_first:
                    await notifier.notify(
                        f"🎉 *PREMIER TRADE LIVE !* 🎉\n"
                        f"{self._tag} `{signal['action'].upper()}` `{qty:.6f}` "
                        f"{self.symbol.split('-')[0]} @ `{price:,.2f}` USDC\n"
                        f"Montant : `{cost:.2f}` USDC\n"
                        f"_Le bot Kairos Alpha est officiellement actif en argent réel._"
                    )
                    self._memory.mark_first_live_trade()

            self._memory.record_decision(
                role="orchestrator",
                task_type="order",
                symbol=self.symbol,
                action=signal["action"],
                confidence=signal["confidence"],
                reasoning=f"Ordre execute : {signal['action']} {qty:.6f} {self.symbol} @ {price:.2f} USDC",
                metadata=(
                    f'{{"order_id":"{order.order_id}",'
                    f'"price":{round(price, 2)},'
                    f'"qty":{round(qty, 6)}}}'
                ),
            )

            self._last_trade_ts = time.time()
            self._signal_streak = {"action": None, "count": 0}   # reset apres trade

            # Reset du peak apres une vente (position fermee)
            if signal["action"] == "sell":
                self._position_peak.pop(self.symbol, None)
                self._position_sltp.pop(self.symbol, None)
                self._position_opened_ts.pop(self.symbol, None)
            # Init du peak + SL/TP dynamique (ATR) au prix d'achat
            elif signal["action"] == "buy":
                self._position_peak[self.symbol] = price
                self._position_opened_ts[self.symbol] = time.time()

                # SL/TP dynamiques basés sur ATR si dispo dans les metadata
                if ATR_BASED_SLTP:
                    atr_pct = float(meta.get("atr_pct", 0)) / 100   # convert % en ratio
                    if atr_pct > 0:
                        sl_pct = min(max(ATR_SL_MULT * atr_pct, ATR_SL_MIN_PCT), ATR_SL_MAX_PCT)
                        tp_pct = max(ATR_TP_MULT * atr_pct, ATR_TP_MIN_PCT)
                        self._position_sltp[self.symbol] = {
                            "sl_pct":  sl_pct,
                            "tp_pct":  tp_pct,
                            "atr_pct": atr_pct,
                        }
                        log.info("atr_sltp_set",
                                 symbol=self.symbol,
                                 atr_pct=round(atr_pct * 100, 3),
                                 sl_pct=round(sl_pct * 100, 2),
                                 tp_pct=round(tp_pct * 100, 2))

            log.info("order_executed", side=signal["action"],
                     qty=round(qty, 6), price=round(price, 2), symbol=self.symbol)

            # SL/TP : ATR-based si BUY recent vient de les setter, sinon fixes
            sltp_use      = self._position_sltp.get(self.symbol, {})
            sl_pct_use    = sltp_use.get("sl_pct", STOP_LOSS_PCT)
            tp_pct_use    = sltp_use.get("tp_pct", TAKE_PROFIT_PCT)
            sl_price      = price * (1 - sl_pct_use)
            tp_price      = price * (1 + tp_pct_use)
            sl_net_pct    = -sl_pct_use - ROUND_TRIP_FEE_PCT
            tp_net_pct    =  tp_pct_use - ROUND_TRIP_FEE_PCT
            fee_cost_usdc = cost * ROUND_TRIP_FEE_PCT
            base = self.symbol.split("-")[0]
            sltp_tag      = " (ATR)" if sltp_use else ""

            await self._notify(
                f"✅ *Ordre executé* — `{self.symbol}`\n"
                f"`{signal['action'].upper()}` `{qty:.6f}` {base} @ `{price:,.2f}` USDC\n"
                f"Montant : `{cost:,.2f}` USDC | Frais ~`{fee_cost_usdc:.3f}` USDC (1.20%)\n"
                f"SL{sltp_tag} : `{sl_price:,.2f}` ({sl_net_pct:+.1%} net)\n"
                f"TP{sltp_tag} : `{tp_price:,.2f}` ({tp_net_pct:+.1%} net)"
            )

            # ── 10. Snapshot post-ordre ───────────────────────────────────────
            new_snapshot = await self._coinbase.get_portfolio_snapshot()
            self._memory.record_snapshot(new_snapshot)

        except Exception as exc:
            log.error("order_failed", error=str(exc), symbol=self.symbol)
            self._memory.record_decision(
                role="orchestrator",
                task_type="alert",
                symbol=self.symbol,
                action=None,
                confidence=0.0,
                reasoning=f"Erreur ordre : {exc}",
            )
            await self._notify(f"❌ *Erreur ordre*\n`{exc}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Boucle infinie
    # ─────────────────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Demarre la boucle principale. Warm-up prefetch au lancement."""

        # Démarrage (sync positions + warm-up + notif) : protégé pour qu'un
        # échec ne tue JAMAIS la tâche du bot avant sa boucle principale.
        try:
            if hasattr(self._coinbase, "sync_live_positions"):
                await self._coinbase.sync_live_positions()

            log.info("warmup_start", symbol=self.symbol)
            await self._market.warmup_from_history()

            if self._market.is_warmed_up:
                await self._notify(
                    f"✅ *Warm-up instantane* - `{len(self._market.price_history)}` prix charges\n"
                    f"Symbole : `{self.symbol}` | Trading actif des le 1er tick"
                )
            else:
                await self._notify(
                    f"🔄 *Warm-up progressif* - historique insuffisant\n"
                    f"Il faudra `{22 - len(self._market.price_history)}` ticks supplementaires"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("startup_error", symbol=self.symbol, error=str(exc),
                      hint="le bot entre quand meme dans sa boucle (warm-up progressif)")

        log.info("orchestrator_loop_start", symbol=self.symbol, interval_s=LOOP_INTERVAL_S)

        try:
            while True:
                try:
                    await self._tick()
                except Exception as exc:
                    log.error("tick_unhandled_error", error=str(exc))
                await asyncio.sleep(LOOP_INTERVAL_S)
        except asyncio.CancelledError:
            # Ne PAS fermer self._coinbase ici : le client est partagé entre tous
            # les bots du swarm. Le fermer sur l'annulation d'un seul bot
            # (add/remove/set_pair) casserait les autres. La fermeture est gérée
            # au niveau du swarm lors de l'arrêt global.
            log.info("orchestrator_stopped", symbol=self.symbol)
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entree standalone
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    orc = Orchestrator()
    await orc.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
