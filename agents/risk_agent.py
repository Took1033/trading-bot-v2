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

load_dotenv()
log = structlog.get_logger()

# ─────────────────────────────────────────────────────────────────────────────
# Parametres de risque (surchargeables via .env)
# ─────────────────────────────────────────────────────────────────────────────

MAX_POSITION_PCT = float(os.getenv("RISK_MAX_POSITION_PCT", "0.02"))  # 2%
MIN_CONFIDENCE   = float(os.getenv("RISK_MIN_CONFIDENCE",   "0.55"))  # 55%
MIN_USDC_TRADE   = float(os.getenv("RISK_MIN_USDC_TRADE",   "10.0"))  # 10 USDC

# Scaling dynamique : la taille de position est multipliee par le ratio
# confidence / CONFIDENCE_BASE, puis plafonnee a MAX_SCALE.
# Exemple : confidence=0.85, base=0.70 -> scale=1.21 -> position = 2% * 1.21 = 2.42%
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
                # Scaling : plus la confiance est haute, plus on investit
                scale         = min(signal_confidence / CONFIDENCE_BASE, MAX_SCALE)
                dynamic_pct   = MAX_POSITION_PCT * scale
                usdc_to_spend = portfolio_usdc * dynamic_pct

                if usdc_to_spend < MIN_USDC_TRADE:
                    reasons.append(
                        f"Montant trop faible ({usdc_to_spend:.2f} USDC < min {MIN_USDC_TRADE} USDC)"
                    )
                else:
                    qty = usdc_to_spend / price
                    log.info(
                        "position_sized",
                        confidence=round(signal_confidence, 2),
                        scale=round(scale, 2),
                        pct=round(dynamic_pct * 100, 2),
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
