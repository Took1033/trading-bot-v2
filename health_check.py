"""
health_check.py - Verifie que tous les composants sont operationnels.

Usage :
    python health_check.py

Retourne 0 si tout est OK, 1 si au moins un check echoue.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _ok(label: str) -> None:
    print(f"  [OK] {label}")


def _warn(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  [WA] {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  [KO] {label}{suffix}")


async def run_checks() -> int:
    """Retourne le nombre de checks en echec."""
    failures = 0

    print("\n=== Kairos Alpha — Health Check ===\n")

    # -- 1. Imports -------------------------------------------------------
    print("[1] Imports des modules")
    required = [
        ("agents.orchestrator",        "Orchestrator"),
        ("agents.market_agent",        "MarketAgent"),
        ("agents.risk_agent",          "RiskAgent"),
        ("agents.memory_agent",        "MemoryAgent"),
        ("agents.trading_state",       "is_paused"),
        ("agents.signal_validator",    "validate_signal"),
        ("agents.bot_swarm",           "BotSwarm"),
        ("agents.director_agent",      "DirectorAgent"),
        ("agents.dynamic_bot",         "DynamicBot"),
        ("interfaces.coinbase_client", "CoinbaseClient"),
        ("interfaces.telegram_bot",    "build_app"),
        ("interfaces.notifier",        "notify"),
        ("strategies.simple_ma",       "analyze"),
        ("strategies.backtester",      "Backtester"),
    ]
    for module, attr in required:
        try:
            mod = __import__(module, fromlist=[attr])
            getattr(mod, attr)
            _ok(f"{module}.{attr}")
        except Exception as exc:
            _fail(f"{module}.{attr}", str(exc))
            failures += 1

    # -- 2. Base de donnees -----------------------------------------------
    print("\n[2] Base de donnees SQLite")
    db_path = os.getenv("DB_PATH", "memory/trading.db")
    try:
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        required_tables = {"decisions", "portfolio_snapshots", "mcp_messages"}
        missing = required_tables - set(tables)
        if missing:
            _fail(f"Tables manquantes : {missing}")
            failures += 1
        else:
            n_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            _ok(f"DB ok - {len(tables)} tables, {n_decisions} decisions enregistrees")
        conn.close()
    except Exception as exc:
        _fail(f"DB inaccessible : {exc}")
        failures += 1

    # -- 3. Variables d'environnement -------------------------------------
    print("\n[3] Variables d'environnement")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if token:
        _ok("TELEGRAM_BOT_TOKEN configure")
    else:
        _warn("TELEGRAM_BOT_TOKEN absent - bot Telegram desactive")

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    config_file = Path(db_path).parent / "telegram_config.json"
    if chat_id:
        _ok(f"TELEGRAM_CHAT_ID configure ({chat_id})")
    elif config_file.exists():
        _ok("Chat ID trouve dans telegram_config.json")
    else:
        _warn("TELEGRAM_CHAT_ID absent - utilise /register dans le bot")

    mode = os.getenv("COINBASE_MODE", "paper")
    if mode == "paper":
        _ok(f"COINBASE_MODE={mode}")
    else:
        _warn(f"COINBASE_MODE={mode} - ATTENTION : mode live !")

    # -- 4. API Coinbase (prix public) ------------------------------------
    print("\n[4] API Coinbase (prix public)")
    try:
        import aiohttp
        url = "https://api.coinbase.com/v2/prices/BTC-USDC/spot"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    price = float(data["data"]["amount"])
                    _ok(f"BTC-USDC : {price:,.2f} USDC")
                else:
                    _fail(f"API Coinbase HTTP {r.status}")
                    failures += 1
    except Exception as exc:
        _fail(f"API Coinbase inaccessible : {exc}")
        failures += 1

    # -- 5. Orchestrateur (1 tick) ----------------------------------------
    print("\n[5] Orchestrateur (1 tick de test)")
    try:
        import structlog
        # Silencer les logs pour ce test
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(40)  # ERROR only
        )
        from agents.orchestrator import Orchestrator
        orc = Orchestrator()
        await orc._tick()
        _ok("Tick OK")
    except Exception as exc:
        _fail(f"Tick echoue : {exc}")
        failures += 1

    # -- 6. Telegram (validation token) -----------------------------------
    print("\n[6] Token Telegram")
    if not token:
        _warn("Skipped - TOKEN absent")
    else:
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{token}/getMe"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    data = await r.json()
                    if data.get("ok"):
                        bot = data["result"]
                        _ok(f"Token valide - @{bot['username']} ({bot['first_name']})")
                    else:
                        _fail(f"Token invalide : {data.get('description', '?')}")
                        failures += 1
        except Exception as exc:
            _fail(f"Verification token : {exc}")
            failures += 1

    # -- Resume -----------------------------------------------------------
    print(f"\n{'=' * 38}")
    if failures == 0:
        print("  Tous les checks passent - bot pret !")
    else:
        print(f"  {failures} check(s) en echec - voir details ci-dessus")
    print(f"{'=' * 38}\n")

    return failures


if __name__ == "__main__":
    n_failures = asyncio.run(run_checks())
    sys.exit(0 if n_failures == 0 else 1)
