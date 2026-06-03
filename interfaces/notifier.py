"""
Notifier — envoie des messages Telegram + Discord (webhook) en parallele.

Telegram : token + chat_id (auto-resolu via /register ou .env)
Discord  : URL de webhook dans .env (DISCORD_WEBHOOK_URL), optionnel

Jamais d'exception levee — le trading ne doit pas crasher si une notif foire.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
_CONFIG          = Path(os.getenv("DB_PATH", "memory/trading.db")).parent / "telegram_config.json"


def _chat_id() -> str:
    """Lit le chat_id depuis l'env ou le fichier de config."""
    env_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env_id:
        return env_id
    if _CONFIG.exists():
        try:
            return str(json.loads(_CONFIG.read_text(encoding="utf-8")).get("chat_id", ""))
        except Exception:
            pass
    return ""


def _markdown_to_discord(text: str) -> str:
    """Convertit le Markdown Telegram en Markdown Discord (similaire mais pas identique)."""
    # Telegram utilise *bold*, Discord utilise **bold**
    # Telegram utilise `code`, Discord aussi
    # Telegram utilise _italic_, Discord aussi
    return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", text)


async def _notify_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Envoie a Telegram (sans lever d'exception)."""
    if not TOKEN:
        return False
    chat_id = _chat_id()
    if not chat_id:
        return False

    if len(text) > 4096:
        text = text[:4090] + "\n…"

    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                log.warning("telegram_http_error", status=resp.status, body=body[:150])
                return False
    except Exception as exc:
        log.warning("telegram_exception", error=str(exc))
        return False


async def _notify_discord(text: str) -> bool:
    """Envoie a Discord via webhook (sans lever d'exception)."""
    if not DISCORD_WEBHOOK:
        return False

    discord_text = _markdown_to_discord(text)
    # Discord limit = 2000 chars
    if len(discord_text) > 2000:
        discord_text = discord_text[:1995] + "\n…"

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DISCORD_WEBHOOK,
                json={"content": discord_text},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                body = await resp.text()
                log.warning("discord_http_error", status=resp.status, body=body[:150])
                return False
    except Exception as exc:
        log.warning("discord_exception", error=str(exc))
        return False


async def notify(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Envoie a Telegram ET Discord en parallele (si tous deux configures).
    Retourne True si AU MOINS UN canal a reussi.
    """
    results = await asyncio.gather(
        _notify_telegram(text, parse_mode),
        _notify_discord(text),
        return_exceptions=False,
    )
    success = any(results)
    if success:
        log.debug("notify_sent",
                  telegram=results[0], discord=results[1], chars=len(text))
    else:
        log.debug("notify_no_channel",
                  telegram_cfg=bool(TOKEN), discord_cfg=bool(DISCORD_WEBHOOK))
    return success
