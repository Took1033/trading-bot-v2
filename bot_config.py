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

# Problemes rencontres au dernier load_bots_config() (config corrompue, doublons
# ignores...). Vide = RAS. Le swarm le lit au boot pour alerter Telegram (Axe 2).
LAST_LOAD_ISSUES: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Chargement / sauvegarde
# ─────────────────────────────────────────────────────────────────────────────

def _filter_and_dedup(raw: list) -> tuple[list[dict], list[str]]:
    """Garde les bots bien formes, SANS bot_id ni symbole duplique (= collision de
    suivi de position, bug connu). Renvoie (gardes, problemes). 1re occurrence gagne."""
    kept: list[dict] = []
    issues: list[str] = []
    seen_ids: set[str] = set()
    seen_syms: set[str] = set()
    for b in raw:
        if not _is_well_formed(b):
            issues.append(f"entree malformee ignoree : {str(b)[:70]}")
            continue
        bid, sym = b["bot_id"], b["symbol"].upper()
        if bid in seen_ids:
            issues.append(f"bot_id duplique '{bid}' ignore (collision de suivi)")
        elif sym in seen_syms:
            issues.append(f"symbole duplique '{sym}' (bot '{bid}') ignore (collision de position)")
        else:
            seen_ids.add(bid)
            seen_syms.add(sym)
            kept.append(b)
    return kept, issues


def validate_bots_config(bots: list[dict]) -> list[str]:
    """Liste les problemes d'une config bots (vide = OK). Reutilisable (dashboard...)."""
    return _filter_and_dedup(bots)[1]


def load_bots_config() -> list[dict]:
    """Charge config/bots.json. SECURITE (Axe 2) : si le fichier est PRESENT mais
    corrompu / sans bot valide, on renvoie [] (le bot ne trade RIEN) plutot que de
    retomber SILENCIEUSEMENT sur les vieux defaults scalpers = mauvais trades. Les
    problemes sont exposes dans LAST_LOAD_ISSUES (le swarm alerte au boot)."""
    global LAST_LOAD_ISSUES
    LAST_LOAD_ISSUES = []

    if not CONFIG_PATH.exists():
        log.info("bots_config_missing_using_defaults", path=str(CONFIG_PATH))
        save_bots_config(DEFAULT_BOTS)   # 1er lancement : materialise le fichier
        return [dict(b) for b in DEFAULT_BOTS]

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        LAST_LOAD_ISSUES = [f"config/bots.json CORROMPU (JSON invalide : {str(exc)[:70]}) "
                            f"-> aucun bot charge, le bot ne tradera rien."]
        log.critical("bots_config_corrupt_trading_nothing", error=str(exc), path=str(CONFIG_PATH))
        return []

    kept, issues = _filter_and_dedup(data.get("bots", []))
    if not kept:
        LAST_LOAD_ISSUES = issues + ["aucun bot valide dans config/bots.json -> le bot ne tradera rien."]
        log.critical("bots_config_no_valid_bots", path=str(CONFIG_PATH), issues=issues)
        return []
    LAST_LOAD_ISSUES = issues
    if issues:
        log.warning("bots_config_loaded_with_issues", n=len(kept), issues=issues)
    else:
        log.info("bots_config_loaded", n=len(kept), path=str(CONFIG_PATH))
    return kept


def save_bots_config(bots: list[dict]) -> None:
    """Écrit la config des bots dans config/bots.json (écriture atomique)."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        for b in bots:
            e = {
                "bot_id": b["bot_id"],
                "symbol": b["symbol"],
                "weight": round(float(b.get("weight", 0.0)), 4),
                "name":   b.get("name", b["bot_id"].upper()),
            }
            if b.get("type"):          # preserve le type (ex: "trend") sinon le
                e["type"] = b["type"]  # bot redeviendrait un scalpeur au reload
            entries.append(e)
        payload = {"bots": entries}
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
