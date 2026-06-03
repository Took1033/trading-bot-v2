"""
bot_config.py — chargement / sauvegarde de la configuration des bots du swarm.

La config vit dans config/bots.json (versionnable, éditable à la main ou via
les commandes Telegram / dashboard). Permet de personnaliser les paires, poids
et noms sans toucher au code.

Format :
    {"bots": [
        {"bot_id": "btc", "symbol": "BTC-USDC", "weight": 0.35, "name": "Bitcoin"},
        ...
    ]}

Validation des symboles :
  - format    : ^[A-Z0-9]+-[A-Z0-9]+$  (ex: BTC-USDC)  — synchrone, toujours
  - existence : interroge les produits Coinbase           — asynchrone, best-effort
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

log = structlog.get_logger()

CONFIG_PATH = Path(__file__).parent / "config" / "bots.json"

# Fallback si le fichier est absent / corrompu (= ancien DEFAULT_BOTS)
DEFAULT_BOTS: list[dict] = [
    {"bot_id": "btc",       "symbol": "BTC-USDC", "weight": 0.35, "name": "Bitcoin"},
    {"bot_id": "eth",       "symbol": "ETH-USDC", "weight": 0.30, "name": "Ethereum"},
    {"bot_id": "sol",       "symbol": "SOL-USDC", "weight": 0.20, "name": "Solana"},
    {"bot_id": "dynamique", "symbol": "BTC-USDC", "weight": 0.15, "name": "Dynamique"},
]

_SYMBOL_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


# ─────────────────────────────────────────────────────────────────────────────
# Chargement / sauvegarde
# ─────────────────────────────────────────────────────────────────────────────

def load_bots_config() -> list[dict]:
    """Charge la config des bots depuis config/bots.json (fallback DEFAULT_BOTS)."""
    if not CONFIG_PATH.exists():
        log.info("bots_config_missing_using_defaults", path=str(CONFIG_PATH))
        save_bots_config(DEFAULT_BOTS)   # matérialise le fichier au premier lancement
        return [dict(b) for b in DEFAULT_BOTS]

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        bots = data.get("bots", [])
        valid = [b for b in bots if _is_well_formed(b)]
        if not valid:
            log.warning("bots_config_empty_using_defaults", path=str(CONFIG_PATH))
            return [dict(b) for b in DEFAULT_BOTS]
        log.info("bots_config_loaded", n=len(valid), path=str(CONFIG_PATH))
        return valid
    except Exception as exc:
        log.error("bots_config_load_failed", error=str(exc), path=str(CONFIG_PATH))
        return [dict(b) for b in DEFAULT_BOTS]


def save_bots_config(bots: list[dict]) -> None:
    """Écrit la config des bots dans config/bots.json (écriture atomique)."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bots": [
            {
                "bot_id": b["bot_id"],
                "symbol": b["symbol"],
                "weight": round(float(b.get("weight", 0.0)), 4),
                "name":   b.get("name", b["bot_id"].upper()),
            }
            for b in bots
        ]}
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        log.info("bots_config_saved", n=len(bots), path=str(CONFIG_PATH))
    except Exception as exc:
        log.error("bots_config_save_failed", error=str(exc))


def _is_well_formed(b: dict) -> bool:
    return (
        isinstance(b, dict)
        and isinstance(b.get("bot_id"), str) and b["bot_id"]
        and isinstance(b.get("symbol"), str) and validate_symbol_format(b["symbol"])
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validation des symboles
# ─────────────────────────────────────────────────────────────────────────────

def validate_symbol_format(symbol: str) -> bool:
    """Validation synchrone du format (ex: BTC-USDC)."""
    return bool(symbol and _SYMBOL_RE.match(symbol.upper()))


async def symbol_exists(symbol: str) -> bool:
    """
    Vérifie que la paire est tradable sur Coinbase Advanced Trade (best-effort).

    On interroge le MÊME endpoint que le bot utilise pour les prix
    (api.coinbase.com/v2/prices/{symbol}/spot) afin que la validation reflète
    exactement le catalogue de trading, et pas celui — différent — de l'API
    Coinbase Exchange. Si l'API est injoignable, on retourne True (best-effort,
    le format est déjà validé).
    """
    symbol = symbol.upper()
    if not validate_symbol_format(symbol):
        return False

    try:
        import aiohttp
        url = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    return bool(data.get("data", {}).get("amount"))
                if r.status == 404:
                    return False   # paire inconnue côté Coinbase
                return True        # autre code : on ne bloque pas
    except Exception as exc:
        log.warning("symbol_exists_check_failed", symbol=symbol, error=str(exc))
        return True
