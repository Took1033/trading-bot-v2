"""
Etat global partage entre orchestrateurs et bot Telegram.

Supporte la pause individuelle par bot_id + une pause globale (kill switch).
Pas de dependances externes - module pur Python.
"""
from __future__ import annotations

import time

_paused: dict[str, bool] = {}
_kill_switch: bool       = False
_kill_reason: str | None = None
_kill_since:  float | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Pause individuelle (par bot_id)
# ─────────────────────────────────────────────────────────────────────────────

def is_paused(bot_id: str = "main") -> bool:
    """True si le bot specifie est en pause OU si le kill switch est actif."""
    return _kill_switch or _paused.get(bot_id, False)


def is_bot_paused(bot_id: str = "main") -> bool:
    """True si le bot est en pause INDIVIDUELLE (/pause), hors kill switch.

    Distinction necessaire : le kill switch global ne doit bloquer que les
    nouvelles ENTREES — les boucles continuent d'evaluer (SL/TP, signaux, DB,
    dashboard) pour que le systeme reste protege et observable pendant les
    pauses Fear & Greed prolongees.
    """
    return _paused.get(bot_id, False)


def pause(bot_id: str = "main") -> None:
    """Met un bot specifique en pause."""
    _paused[bot_id] = True


def resume(bot_id: str = "main") -> None:
    """Reprend un bot specifique."""
    _paused[bot_id] = False


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch global (Director Agent)
# ─────────────────────────────────────────────────────────────────────────────

def kill_switch(reason: str) -> None:
    """Active le kill switch global : plus aucune NOUVELLE entree autorisee."""
    global _kill_switch, _kill_reason, _kill_since
    if not _kill_switch:
        _kill_since = time.time()
    _kill_switch = True
    _kill_reason = reason


def release_kill_switch() -> None:
    """Desactive le kill switch global."""
    global _kill_switch, _kill_reason, _kill_since
    _kill_switch = False
    _kill_reason = None
    _kill_since  = None


def get_kill_since() -> float | None:
    """Timestamp (epoch) du debut de l'episode kill switch en cours, sinon None."""
    return _kill_since


def is_kill_switch_active() -> bool:
    return _kill_switch


def get_kill_reason() -> str | None:
    return _kill_reason


def get_all_paused() -> dict[str, bool]:
    """Retourne l'etat de pause de tous les bots."""
    return dict(_paused)


# ─────────────────────────────────────────────────────────────────────────────
# Grace d'entree au demarrage (ferme le trou F&G du boot)
# ─────────────────────────────────────────────────────────────────────────────
# Au boot, le Director ne fait sa 1re evaluation Fear & Greed qu'apres ~45s
# (sleep initial 15s + un 1er _check qui ne fait qu'amorcer la valeur). Comme un
# TrendBot ne lit PAS le F&G lui-meme (sa seule protection peur = le kill switch),
# une entree pouvait passer pendant cette fenetre en plein Extreme Fear (vecu le
# 02/07 sur SOL/AAVE). Le preflight pose une courte grace quand il n'a pas pu
# confirmer que le marche est sur : elle bloque UNIQUEMENT les nouvelles entrees
# (comme le kill switch) — les sorties/SL/TP continuent — jusqu'a ce que le
# Director statue. Nulle si le preflight a pu lire un F&G non-extreme.
_entry_grace_until: float = 0.0


def set_entry_grace(until_ts: float) -> None:
    """Bloque les nouvelles entrees jusqu'a `until_ts` (epoch). Ne recule jamais
    une grace deja posee (on garde la plus tardive)."""
    global _entry_grace_until
    _entry_grace_until = max(_entry_grace_until, until_ts)


def clear_entry_grace() -> None:
    global _entry_grace_until
    _entry_grace_until = 0.0


def entry_grace_remaining() -> float:
    """Secondes restantes de grace d'entree (0 si aucune)."""
    if _entry_grace_until <= 0:
        return 0.0
    return max(0.0, _entry_grace_until - time.time())


def entries_allowed() -> bool:
    """True si de NOUVELLES entrees sont permises : ni kill switch, ni grace de boot."""
    return not _kill_switch and entry_grace_remaining() <= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Fear & Greed Index (publie par DirectorAgent, lu par RiskAgent)
# ─────────────────────────────────────────────────────────────────────────────

_fg_value: int | None = None
_fg_label: str        = "—"


def set_fear_greed(value: int | None, label: str = "—") -> None:
    """Publie la derniere valeur F&G (appele par DirectorAgent)."""
    global _fg_value, _fg_label
    _fg_value = value
    _fg_label = label


def get_fear_greed() -> tuple[int | None, str]:
    """Retourne (value, label) de la derniere mesure F&G. (None, '—') si jamais fetch."""
    return _fg_value, _fg_label


# ─────────────────────────────────────────────────────────────────────────────
# News sentiment (publie par NewsSentiment, lu par RiskAgent)
# ─────────────────────────────────────────────────────────────────────────────

_news_score:      float | None = None
_news_commentary: str          = "—"
_news_headlines:  list[str]    = []
_news_ts:         str | None   = None
_news_by_symbol_store: dict[str, list[str]] = {}   # news par-crypto (Google News FR par requete)


def set_news_by_symbol(by_symbol: dict[str, list[str]] | None) -> None:
    """Publie les titres FR par crypto (une requete Google News par actif)."""
    global _news_by_symbol_store
    _news_by_symbol_store = dict(by_symbol or {})


def set_news_sentiment(score: float | None, commentary: str = "—",
                       headlines: list[str] | None = None) -> None:
    """Publie le sentiment global news (-1 = bearish, +1 = bullish) + les titres frais."""
    global _news_score, _news_commentary, _news_headlines, _news_ts
    _news_score = score
    _news_commentary = commentary
    if headlines is not None:
        _news_headlines = list(headlines[:12])
    from datetime import datetime, timezone
    _news_ts = datetime.now(timezone.utc).isoformat()


def get_news_sentiment() -> tuple[float | None, str]:
    return _news_score, _news_commentary


def news_sentiment_label(score: float | None) -> str:
    """Traduit le score en langage clair (appli + notifs)."""
    if score is None:  return "indisponible"
    if score >= 0.5:   return "nettement haussier"
    if score >= 0.2:   return "légèrement haussier"
    if score > -0.2:   return "neutre"
    if score > -0.5:   return "légèrement baissier"
    return "nettement baissier"


# Mots-cles par crypto pour rattacher un titre a un actif (news par-crypto).
# Noms complets surs + tickers avec frontiere de mot ; tickers ambigus (link/dot/near/uni/atom
# = mots anglais courants) rattaches par leur NOM COMPLET seulement, pour eviter les faux positifs.
NEWS_KEYWORDS = {
    "BTC": [r"bitcoin", r"\bbtc\b"],   "ETH":  [r"ethereum", r"\beth\b"],
    "SOL": [r"solana", r"\bsol\b"],    "XRP":  [r"\bxrp\b", r"ripple"],
    "DOGE": [r"dogecoin", r"\bdoge\b"], "ADA": [r"cardano", r"\bada\b"],
    "AVAX": [r"avalanche", r"\bavax\b"], "LINK": [r"chainlink"],
    "DOT": [r"polkadot"],              "LTC":  [r"litecoin", r"\bltc\b"],
    "BCH": [r"bitcoin cash", r"\bbch\b"], "ATOM": [r"\bcosmos\b"],
    "XLM": [r"stellar", r"\bxlm\b"],   "UNI":  [r"uniswap"],
    "AAVE": [r"\baave\b"],             "NEAR": [r"near protocol"],
}


def news_by_symbol(headlines: list[str] | None = None) -> dict[str, list[str]]:
    """Regroupe les titres par crypto mentionnee (matching a frontieres de mot)."""
    import re
    hs = headlines if headlines is not None else _news_headlines
    out: dict[str, list[str]] = {}
    for h in hs:
        low = h.lower()
        for sym, pats in NEWS_KEYWORDS.items():
            if any(re.search(p, low) for p in pats):
                out.setdefault(sym, []).append(h)
    return out


def get_news() -> dict:
    """Vue riche pour l'appli : score, libellé clair, effet concret, titres frais + par-crypto."""
    mult = news_sentiment_multiplier(_news_score)
    return {
        "score": _news_score,
        "label": news_sentiment_label(_news_score),
        "commentary": _news_commentary,
        "multiplier": round(mult, 2),
        "effect_pct": round((mult - 1) * 100, 1),   # effet sur la taille des NOUVELLES entrees
        "headlines": _news_headlines,
        # par-crypto : requetes Google News FR dediees si dispo, sinon matching mots-cles des titres globaux
        "by_symbol": _news_by_symbol_store if _news_by_symbol_store else news_by_symbol(),
        "updated_at": _news_ts,
    }


def news_sentiment_multiplier(score: float | None) -> float:
    """
    Multiplicateur de position basé sur le sentiment news.

    Score < -0.5 : 0.7x (defensif, news bearish)
    Score < -0.2 : 0.85x
    Score -0.2 a 0.2 : 1.0x (neutre)
    Score > 0.2 : 1.05x
    Score > 0.5 : 1.15x (legere agressivite, news bullish)
    """
    if score is None:
        return 1.0
    if score < -0.5:  return 0.70
    if score < -0.2:  return 0.85
    if score < 0.2:   return 1.00
    if score < 0.5:   return 1.05
    return 1.15


def fear_greed_position_multiplier(fg_value: int | None) -> float:
    """
    Retourne un multiplicateur de taille de position basé sur F&G.

    Logique : on achete plus quand le marche panique (acheter la peur),
    on achete moins quand le marche est euphorique (eviter les tops).

    F&G  0-20  : Extreme Fear   → 1.30x  (opportunite historique)
    F&G 20-40  : Fear           → 1.15x  (legere agressivite)
    F&G 40-60  : Neutral        → 1.00x  (par defaut)
    F&G 60-80  : Greed          → 0.85x  (prudence)
    F&G 80-100 : Extreme Greed  → 0.60x  (forte reduction, risque de top)
    """
    if fg_value is None:
        return 1.0
    if fg_value < 20:  return 1.30
    if fg_value < 40:  return 1.15
    if fg_value < 60:  return 1.00
    if fg_value < 80:  return 0.85
    return 0.60
