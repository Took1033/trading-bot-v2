"""
Claude API client centralise pour Kairos Alpha.

Wrapper minimal autour de anthropic.AsyncAnthropic avec :
  - Cache LRU sur les prompts identiques (60s)
  - Timeout strict (jamais bloquer le trading)
  - Selection automatique du modele selon la tache (par CLAUDE.md)
  - Toutes les erreurs renvoient None (silencieux, non-bloquant)

Calibrage selon CLAUDE.md :
  - Haiku 4.5  | low    | logs, daily reports, validation signaux
  - Sonnet 4.6 | medium | analyse, summaries, Q&A user
  - Opus 4.7   | xhigh  | conception alpha, debug cross-agent
"""
from __future__ import annotations

import asyncio
import os
import time

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ENABLED           = bool(ANTHROPIC_API_KEY)

# Modeles : noms exacts (cf CLAUDE.md)
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-7"

# Cache des completions (cle = hash(model + prompt + max_tokens))
_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL_S = 60.0


async def complete(
    prompt:      str,
    model:       str   = MODEL_HAIKU,
    max_tokens:  int   = 200,
    timeout_s:   float = 10.0,
    system:      str   = "",
    use_cache:   bool  = True,
) -> str | None:
    """
    Appel Claude API generique. Retourne le texte de la reponse ou None si echec.

    Ne leve JAMAIS d'exception (defensif : ne doit pas bloquer le trading).
    """
    if not ENABLED:
        return None

    key = f"{model}:{hash(prompt + system)}:{max_tokens}"
    if use_cache and key in _cache:
        ts, cached = _cache[key]
        if time.time() - ts < CACHE_TTL_S:
            return cached

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        kwargs = {
            "model":       model,
            "max_tokens":  max_tokens,
            "messages":    [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        response = await asyncio.wait_for(
            client.messages.create(**kwargs),
            timeout=timeout_s,
        )

        text = response.content[0].text.strip()
        if use_cache:
            _cache[key] = (time.time(), text)

        log.debug("claude_completion",
                  model=model.split("-")[1],
                  prompt_chars=len(prompt),
                  response_chars=len(text))
        return text

    except asyncio.TimeoutError:
        log.warning("claude_timeout", model=model, timeout_s=timeout_s)
        return None
    except Exception as exc:
        log.warning("claude_error", error=str(exc), model=model)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers par cas d'usage (avec model selection + prompt template)
# ─────────────────────────────────────────────────────────────────────────────

async def narrate_daily(metrics: dict) -> str | None:
    """Genere une narration courte (Haiku) du resume quotidien."""
    if not ENABLED or not metrics:
        return None
    prompt = (
        "Tu es un analyste trading. En 2 phrases max et en français, "
        "raconte ce qui s'est passe ces 24h pour un bot crypto.\n\n"
        f"Donnees :\n"
        f"- Signaux generes : {metrics.get('signals', 0)}\n"
        f"- Ordres executes : {metrics.get('orders_executed', 0)}\n"
        f"- Ordres rejetes  : {metrics.get('orders_rejected', 0)}\n"
        f"- P&L 24h         : {metrics.get('pnl_24h_pct', 'N/A')}%\n"
        f"- Valeur courante : {metrics.get('current_value', 'N/A')} USDC\n\n"
        "Reponds direct, sans introduction."
    )
    return await complete(prompt, model=MODEL_HAIKU, max_tokens=150)


async def analyze_closed_trade(
    symbol:    str,
    side:      str,     # "buy" closed by "sell" SL/TP
    entry:     float,
    exit:      float,
    pnl_pct:   float,
    reason:    str,     # "STOP-LOSS", "TAKE-PROFIT", "TRAILING-STOP"
    duration_min: float,
) -> str | None:
    """Post-mortem rapide (Haiku) d'un trade ferme."""
    if not ENABLED:
        return None
    prompt = (
        f"Analyse rapide en 1 phrase (français) d'un trade crypto ferme :\n"
        f"- Symbole  : {symbol}\n"
        f"- Entree   : ${entry:,.2f}\n"
        f"- Sortie   : ${exit:,.2f} ({reason})\n"
        f"- P&L      : {pnl_pct:+.2f}%\n"
        f"- Duree    : {duration_min:.0f} min\n\n"
        "Qu'est-ce que ce trade nous apprend ? Reponds direct."
    )
    return await complete(prompt, model=MODEL_HAIKU, max_tokens=120)


async def answer_user_question(
    question:    str,
    context:     dict,
) -> str | None:
    """Repond a une question utilisateur sur le bot (Sonnet, plus intelligent)."""
    if not ENABLED:
        return None
    ctx_lines = "\n".join(f"- {k} : {v}" for k, v in context.items() if v is not None)
    prompt = (
        f"Question utilisateur sur son bot de trading crypto Kairos Alpha :\n"
        f"\n{question}\n\n"
        f"Contexte actuel :\n{ctx_lines}\n\n"
        f"Reponds en français, concis (3-5 phrases max), factuel. "
        f"Ne fais PAS de recommandations d'investissement."
    )
    return await complete(prompt, model=MODEL_SONNET, max_tokens=400, timeout_s=15)


async def weekly_market_analysis(metrics: dict) -> str | None:
    """Analyse hebdomadaire (Sonnet) pour le rapport du lundi."""
    if not ENABLED:
        return None
    prompt = (
        "Tu es un analyste crypto. Resume en 3 phrases (français) la "
        "performance hebdomadaire de ce bot de trading.\n\n"
        f"Metriques :\n"
        f"- Trades fermes : {metrics.get('n_trades', 0)}\n"
        f"- Win rate      : {metrics.get('win_rate', 'N/A')}%\n"
        f"- Best trade    : {metrics.get('best_trade', 'N/A')}%\n"
        f"- Worst trade   : {metrics.get('worst_trade', 'N/A')}%\n"
        f"- Sharpe        : {metrics.get('sharpe', 'N/A')}\n"
        f"- Max drawdown  : {metrics.get('max_drawdown', 0):.2f}%\n"
        f"- P&L 7j        : {metrics.get('pnl_7d_pct', 'N/A')}%\n\n"
        "Identifie 1 force et 1 axe d'amelioration. Direct, pas d'intro."
    )
    return await complete(prompt, model=MODEL_SONNET, max_tokens=350)
