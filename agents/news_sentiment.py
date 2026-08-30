"""
NewsSentiment - fetch news crypto + scoring sentiment par Claude.

Sources RSS gratuites (pas d'API key requise) :
  - CoinDesk : https://feeds.feedburner.com/CoinDesk
  - CoinTelegraph : https://cointelegraph.com/rss

Workflow :
  1. Fetch les 10-15 titres les plus recents
  2. Claude Haiku score le sentiment global : -1 (très baissier) à +1 (très haussier)
  3. Publie le score dans trading_state pour usage par RiskAgent
  4. Cache 1h (pas d'over-trading sur news)

Le RiskAgent applique un MULTIPLICATEUR a la position si sentiment fort :
  - Score < -0.5 : multiplicateur 0.7 (defensif sur news bearish)
  - Score > +0.5 : multiplicateur 1.15 (legere agressivite sur news bullish)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from xml.etree import ElementTree as ET

import structlog
from dotenv import load_dotenv

from agents import trading_state
from interfaces import notifier
from interfaces.claude_client import complete, ENABLED, MODEL_HAIKU

load_dotenv()
log = structlog.get_logger()

CHECK_INTERVAL_S  = int(os.getenv("NEWS_CHECK_INTERVAL_S", "3600"))   # 1h
NOTIFY_ON_CHANGE  = float(os.getenv("NEWS_NOTIFY_THRESHOLD", "0.3"))  # notif si |Δscore| > 0.3

RSS_SOURCES = [
    ("CoinDesk",      "https://feeds.feedburner.com/CoinDesk"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
]


async def _fetch_rss(url: str) -> list[str]:
    """Recupere les titres d'un flux RSS. Retourne max 10 titres."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return []
                xml_text = await r.text()
        root  = ET.fromstring(xml_text)
        # RSS 2.0 : channel/item/title
        titles = []
        for item in root.iter("item"):
            t = item.find("title")
            if t is not None and t.text:
                titles.append(t.text.strip())
            if len(titles) >= 10:
                break
        return titles
    except Exception as exc:
        log.warning("rss_fetch_failed", url=url, error=str(exc))
        return []


async def fetch_all_headlines() -> list[str]:
    """Fetch tous les flux en parallele."""
    results = await asyncio.gather(*[_fetch_rss(url) for _, url in RSS_SOURCES])
    headlines = []
    for src, titles in zip([s for s, _ in RSS_SOURCES], results):
        for t in titles:
            # Nettoyage HTML basique
            t = re.sub(r"<[^>]+>", "", t)
            headlines.append(f"[{src}] {t}")
    return headlines[:20]   # max 20 titres


async def score_sentiment(headlines: list[str]) -> tuple[float, str] | None:
    """
    Demande a Claude Haiku de scorer le sentiment global.
    Retourne (score, commentary) ou None si echec.
    """
    if not ENABLED or not headlines:
        return None

    headlines_txt = "\n".join(f"- {h}" for h in headlines[:15])
    prompt = (
        "Tu es analyste crypto. Analyse ces titres recents et donne un score "
        "de sentiment global pour le marche crypto.\n\n"
        f"{headlines_txt}\n\n"
        "Reponds UNIQUEMENT en JSON :\n"
        '{"score": -1.0 à +1.0, "commentary": "1 phrase max"}\n\n'
        "Score : -1 = très baissier (FUD, hack, reglementation negative), "
        "0 = neutre, +1 = très haussier (ETF, adoption, breakthrough)."
    )

    response = await complete(prompt, model=MODEL_HAIKU, max_tokens=200, timeout_s=10)
    if not response:
        return None

    # Parse JSON (peut etre wrappee dans ```json)
    import json
    raw = response.strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.split("\n") if not l.strip().startswith("```"))
    try:
        data = json.loads(raw)
        score = float(data.get("score", 0.0))
        score = max(-1.0, min(1.0, score))
        commentary = str(data.get("commentary", "")).strip()
        return score, commentary
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("news_sentiment_parse_error", error=str(exc), raw=response[:200])
        return None


async def update_sentiment_once() -> dict | None:
    """Fetch + score + publish. Retourne le resultat ou None."""
    headlines = await fetch_all_headlines()
    if not headlines:
        log.warning("news_sentiment_no_headlines")
        return None

    scored = await score_sentiment(headlines)
    if not scored:
        return None

    score, commentary = scored
    trading_state.set_news_sentiment(score, commentary, headlines)

    log.info("news_sentiment_updated",
             score=round(score, 2),
             n_headlines=len(headlines),
             commentary=commentary[:80])

    return {"score": score, "commentary": commentary,
            "n_headlines": len(headlines), "headlines": headlines}


async def news_sentiment_loop() -> None:
    """Boucle infinie : update toutes les CHECK_INTERVAL_S secondes."""
    log.info("news_sentiment_started",
             interval_s=CHECK_INTERVAL_S,
             notify_threshold=NOTIFY_ON_CHANGE)

    last_score: float | None = None

    while True:
        try:
            result = await update_sentiment_once()

            if result:
                new_score = result["score"]
                # Notif si changement significatif (et pas premier passage) — PEDAGOGIQUE :
                # le score seul ("0.15 -> 0.65") est illisible ; on traduit en clair + effet concret.
                if last_score is not None and abs(new_score - last_score) >= NOTIFY_ON_CHANGE:
                    lbl_old = trading_state.news_sentiment_label(last_score)
                    lbl_new = trading_state.news_sentiment_label(new_score)
                    mult = trading_state.news_sentiment_multiplier(new_score)
                    eff = round((mult - 1) * 100)
                    if eff > 0:
                        effect = f"le bot prendra des positions **{eff}% plus grosses** (news porteuses)"
                    elif eff < 0:
                        effect = f"le bot prendra des positions **{abs(eff)}% plus petites** (prudence)"
                    else:
                        effect = "**taille des positions inchangée** (sentiment neutre)"
                    emoji = "📈" if new_score > last_score else "📉"
                    heads = "\n".join(f"• {h}" for h in (result.get("headlines") or [])[:3])
                    await notifier.notify(
                        f"{emoji} *Ambiance du marché crypto : {lbl_new}*\n"
                        f"Le sentiment des news est passé de _{lbl_old}_ à _{lbl_new}_.\n"
                        f"(score {last_score:+.2f} → {new_score:+.2f} · échelle −1 très baissier … +1 très haussier)\n"
                        f"➡️ Concrètement : {effect}.\n"
                        f"_{result['commentary']}_"
                        + (f"\n\nÀ la une :\n{heads}" if heads else "")
                    )
                last_score = new_score

            await asyncio.sleep(CHECK_INTERVAL_S)

        except asyncio.CancelledError:
            log.info("news_sentiment_stopped")
            raise
        except Exception as exc:
            log.warning("news_sentiment_error", error=str(exc))
            await asyncio.sleep(300)
