"""
RiskAgent - controle du risque avant tout ordre.

Verifie :
  - Confiance minimale du signal
  - Absence de position dupliquee (deja long / deja flat)
  - Taille de position dynamique (% du portefeuille * confidence)
  - Montant minimum de trade
"""
from __future__ import annotations

import os

import structlog
from dotenv import load_dotenv

from agents import trading_state

load_dotenv()
log = structlog.get_logger()

# Adaptive sizing selon Fear & Greed (peut etre desactive)
FG_ADAPTIVE_ENABLED = os.getenv("RISK_FG_ADAPTIVE", "true").lower() in ("true", "1", "yes")

# Correlation cap : BTC/ETH/SOL bougent ensemble ~80% du temps.
# Si plusieurs bots sont long en meme temps, on est sur-expose.
# Cap = max X% du portefeuille en exposition combinee (toutes positions).
RISK_MAX_COMBINED_EXPOSURE_PCT = float(os.getenv("RISK_MAX_COMBINED_EXPOSURE_PCT", "0.06"))   # 6% max total

# ─────────────────────────────────────────────────────────────────────────────
# Parametres de risque (surchargeables via .env)
# ─────────────────────────────────────────────────────────────────────────────

_MODE = os.getenv("COINBASE_MODE", "paper")

# En live on commence TRES conservateur (0.5% par trade = ~$50 sur $10k)
# En paper on reste a 2% pour des backtests representatifs
_DEFAULT_MAX_PCT = "0.005" if _MODE == "live" else "0.02"
_DEFAULT_MIN_CONF = "0.65" if _MODE == "live" else "0.55"   # plus strict en live

MAX_POSITION_PCT = float(os.getenv("RISK_MAX_POSITION_PCT", _DEFAULT_MAX_PCT))
MIN_CONFIDENCE   = float(os.getenv("RISK_MIN_CONFIDENCE",   _DEFAULT_MIN_CONF))
MIN_USDC_TRADE   = float(os.getenv("RISK_MIN_USDC_TRADE",   "2.0"))   # min Coinbase

# Scaling dynamique : position = MAX_POSITION_PCT * (confidence / base), plafonne a MAX_SCALE
# Ex live : confidence=0.85, base=0.70 -> scale=1.21 -> position = 0.5% * 1.21 = 0.6%
CONFIDENCE_BASE = float(os.getenv("RISK_CONFIDENCE_BASE", "0.70"))
MAX_SCALE       = float(os.getenv("RISK_MAX_SCALE",        "1.50"))   # plafond x1.5


class RiskAgent:
    """Evalue un signal et retourne approved + qty au format MCP artifact."""

    def evaluate(
        self,
        signal_action:      str,
        signal_confidence:  float,
        portfolio_usdc:     float,
        price:              float,
        last_action:        str | None,
        total_portfolio:    float | None = None,
        current_exposure:   float | None = None,
    ) -> dict:
        """
        Args:
            signal_action:     "buy" | "sell" | "hold"
            signal_confidence: 0.0 - 1.0
            portfolio_usdc:    balance USDC disponible
            price:             prix actuel du symbole
            last_action:       derniere action executee ("buy" | "sell" | None)

        Returns:
            MCP artifact { approved, action, qty, reasons, position_pct }
        """
        reasons: list[str] = []
        qty:     float     = 0.0

        # 1. Signal non actionnable
        if signal_action == "hold":
            return self._artifact(
                approved=False, action="hold", qty=0.0,
                reasons=["Signal HOLD - rien a faire"], position_pct=0.0,
            )

        # 2. Confiance insuffisante
        if signal_confidence < MIN_CONFIDENCE:
            reasons.append(
                f"Confiance insuffisante ({signal_confidence:.0%} < {MIN_CONFIDENCE:.0%})"
            )

        # 3. Doublons de position
        if signal_action == "buy" and last_action == "buy":
            reasons.append("Position deja longue - pas de doublon d'achat")
        elif signal_action == "sell" and last_action in (None, "sell", "rejected"):
            reasons.append("Pas de position ouverte a vendre")

        # 4. Calcul de la taille de position dynamique
        if not reasons:
            if signal_action == "buy":
                # Scaling confidence : plus la confiance est haute, plus on investit
                conf_scale = min(signal_confidence / CONFIDENCE_BASE, MAX_SCALE)

                # Scaling Fear & Greed : achete plus en panique, moins en euphorie
                fg_value, fg_label = trading_state.get_fear_greed()
                fg_mult = (
                    trading_state.fear_greed_position_multiplier(fg_value)
                    if FG_ADAPTIVE_ENABLED else 1.0
                )

                # Scaling News sentiment (Claude Haiku scoring sur RSS crypto)
                news_score, _ = trading_state.get_news_sentiment()
                news_mult     = trading_state.news_sentiment_multiplier(news_score)

                dynamic_pct   = MAX_POSITION_PCT * conf_scale * fg_mult * news_mult
                usdc_to_spend = portfolio_usdc * dynamic_pct

                # ── Correlation cap : limite l'exposition combinee inter-bots ──
                if total_portfolio and total_portfolio > 0:
                    exposure_pct_now    = (current_exposure or 0.0) / total_portfolio
                    cap_remaining       = max(0.0,
                        RISK_MAX_COMBINED_EXPOSURE_PCT - exposure_pct_now)
                    max_usdc_allowed    = total_portfolio * cap_remaining
                    if usdc_to_spend > max_usdc_allowed:
                        log.info("correlation_cap_applied",
                                 wanted=round(usdc_to_spend, 2),
                                 allowed=round(max_usdc_allowed, 2),
                                 current_exposure_pct=round(exposure_pct_now * 100, 2),
                                 cap_pct=round(RISK_MAX_COMBINED_EXPOSURE_PCT * 100, 2))
                        usdc_to_spend = max_usdc_allowed
                    if max_usdc_allowed < MIN_USDC_TRADE:
                        reasons.append(
                            f"Correlation cap atteint : exposition combinee "
                            f"{exposure_pct_now*100:.1f}% >= max {RISK_MAX_COMBINED_EXPOSURE_PCT*100:.1f}%"
                        )

                if not reasons and usdc_to_spend < MIN_USDC_TRADE:
                    reasons.append(
                        f"Montant trop faible ({usdc_to_spend:.2f} USDC < min {MIN_USDC_TRADE} USDC)"
                    )
                else:
                    qty = usdc_to_spend / price
                    log.info(
                        "position_sized",
                        confidence=round(signal_confidence, 2),
                        conf_scale=round(conf_scale, 2),
                        fg_value=fg_value, fg_mult=round(fg_mult, 2),
                        news_score=round(news_score, 2) if news_score is not None else None,
                        news_mult=round(news_mult, 2),
                        pct=round(dynamic_pct * 100, 3),
                        usdc=round(usdc_to_spend, 2),
                    )

            elif signal_action == "sell":
                qty = -1.0   # sentinel : "vendre toute la position"

        approved     = len(reasons) == 0
        position_pct = (qty * price / portfolio_usdc) if qty > 0 and portfolio_usdc > 0 else 0.0

        log.info(
            "risk_evaluation",
            action=signal_action,
            approved=approved,
            confidence=round(signal_confidence, 2),
            qty=round(qty, 6) if qty > 0 else qty,
            reasons=reasons if reasons else ["OK"],
        )

        return self._artifact(
            approved=approved, action=signal_action,
            qty=qty, reasons=reasons, position_pct=position_pct,
        )

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _artifact(
        approved: bool,
        action:   str,
        qty:      float,
        reasons:  list[str],
        position_pct: float = 0.0,
    ) -> dict:
        return {
            "node_type": "artifact",
            "sender":    "risk_agent",
            "receiver":  "orchestrator",
            "payload": {
                "approved":     approved,
                "action":       action,
                "qty":          qty,
                "reasons":      reasons,
                "position_pct": round(position_pct * 100, 2),
            },
        }
