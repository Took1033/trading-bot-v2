"""
SignalValidator - validation AI des signaux avant execution.

Utilise Claude Haiku (ultra-rapide, ~0.002$/appel) pour :
  - Valider la coherence du signal avec le contexte de marche
  - Ajuster la confiance selon l'analyse contextuelle
  - Fournir une raison lisible dans les logs

Active uniquement si ANTHROPIC_API_KEY est configure.
Desactive silencieusement si la cle est absente (pas de bloquer le trading).

Calibrage CLAUDE.md : Haiku 4.5 | effort=low | taches repetitives
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from functools import lru_cache

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ENABLED           = bool(ANTHROPIC_API_KEY)
MODEL             = "claude-haiku-4-5-20251001"    # le plus rapide et moins cher
TIMEOUT_S         = 8.0                            # timeout strict pour ne pas bloquer

# Cache des validations : meme signal dans la meme minute = meme reponse
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 60.0   # 1 minute


def _cache_key(symbol: str, action: str, rsi: float, trend: str) -> str:
    return f"{symbol}:{action}:{int(rsi)}:{trend}"


async def validate_signal(
    symbol:     str,
    action:     str,         # "buy" | "sell"
    confidence: float,
    price:      float,
    rsi:        float,
    ema_fast:   float,
    ema_slow:   float,
    ema_trend:  float | None,
    vol_pct:    float,
    reasoning:  str,
) -> dict:
    """
    Valide le signal avec Claude Haiku.

    Returns:
        {
            "approved":    bool,   # True si le signal est coherent
            "confidence":  float,  # confiance ajustee (peut monter ou descendre)
            "commentary":  str,    # explication courte pour les logs
            "used_ai":     bool,   # False si la validation AI n'etait pas disponible
        }
    """
    default = {
        "approved":   True,
        "confidence": confidence,
        "commentary": "Validation AI non disponible",
        "used_ai":    False,
    }

    if not ENABLED:
        return default

    trend = "haussiere" if (ema_trend and price > ema_trend) else "baissiere" if ema_trend else "inconnue"
    key   = _cache_key(symbol, action, rsi, trend)

    # Verifier le cache
    if key in _cache:
        ts, cached = _cache[key]
        if time.time() - ts < CACHE_TTL:
            log.debug("signal_validator_cache_hit", symbol=symbol, action=action)
            return cached

    prompt = (
        f"Tu es un analyste trading crypto. Evalue ce signal en 1 phrase max.\n\n"
        f"Signal : {action.upper()} {symbol}\n"
        f"Prix : ${price:,.2f} | RSI : {rsi:.1f} | "
        f"EMA9 : {ema_fast:.2f} | EMA21 : {ema_slow:.2f}"
        + (f" | EMA50 : {ema_trend:.2f}" if ema_trend else "")
        + f"\nVolatilite : {vol_pct:.3f}% | Tendance : {trend}\n"
        f"Confiance algo : {confidence:.0%}\n"
        f"Raison algo : {reasoning}\n\n"
        f"Reponds en JSON uniquement, sans markdown :\n"
        f'{{ "approved": true/false, "confidence_adjust": -0.05 a +0.05, "commentary": "..." }}\n'
        f"approved=false uniquement si le signal est manifestement contre-tendance ou RSI extreme."
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=TIMEOUT_S,
        )

        raw = response.content[0].text.strip() if response.content else ""
        # Le modele entoure parfois le JSON de fences markdown ou de texte.
        # On extrait le premier objet {...} pour etre robuste.
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            log.warning("signal_validator_empty_response", raw=raw[:120])
            return default
        data = json.loads(match.group(0))

        approved    = bool(data.get("approved", True))
        adj         = float(data.get("confidence_adjust", 0.0))
        commentary  = str(data.get("commentary", ""))
        new_conf    = round(min(max(confidence + adj, 0.0), 1.0), 4)

        result = {
            "approved":   approved,
            "confidence": new_conf,
            "commentary": commentary,
            "used_ai":    True,
        }

        _cache[key] = (time.time(), result)

        log.info("signal_validated_by_ai",
                 symbol=symbol, action=action,
                 approved=approved,
                 confidence_before=round(confidence, 3),
                 confidence_after=round(new_conf, 3),
                 adj=round(adj, 3),
                 commentary=commentary[:80])

        return result

    except asyncio.TimeoutError:
        log.warning("signal_validator_timeout", symbol=symbol, timeout_s=TIMEOUT_S)
        return default
    except json.JSONDecodeError as exc:
        log.warning("signal_validator_json_error", error=str(exc))
        return default
    except Exception as exc:
        log.warning("signal_validator_error", error=str(exc))
        return default
