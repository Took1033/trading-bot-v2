"""
Notifier — envoie des messages Telegram depuis n'importe quel composant.

Priorité de résolution du chat_id :
  1. Variable d'env   TELEGRAM_CHAT_ID
  2. Fichier          memory/telegram_config.json  (créé par /register dans le bot)

Jamais d'exception levée — le trading ne doit pas crasher si Telegram est down.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CONFIG = Path(os.getenv("DB_PATH", "memory/trading.db")).parent / "telegram_config.json"


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


async def notify(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Envoie un message Telegram de façon silencieuse.
    Retourne True si envoyé avec succès, False sinon (sans lever d'exception).
    Tronque automatiquement à 4096 caractères (limite Telegram).
    """
    if not TOKEN:
        log.debug("notify_skip", reason="TOKEN absent")
        return False

    chat_id = _chat_id()
    if not chat_id:
        log.debug("notify_skip", reason="CHAT_ID non configuré — envoie /register au bot")
        return False

    # Telegram limite les messages à 4096 caractères
    if len(text) > 4096:
        text = text[:4090] + "\n…"

    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    log.debug("notify_sent", chars=len(text))
                    return True
                body = await resp.text()
                log.warning("notify_http_error", status=resp.status, body=body[:150])
                return False
    except Exception as exc:
        log.error("notify_exception", error=str(exc))
        return False
