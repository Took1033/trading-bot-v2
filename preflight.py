"""
preflight.py — controles ACTIFS de demarrage (Axe 2 fiabilite).

Complement de config_validator (qui, lui, ne fait que du STATIQUE : verifie que
les valeurs .env sont presentes et dans les bonnes plages). Le preflight, lui,
teste que le systeme MARCHE vraiment avant de lancer le trading :

  1. DB      — DB_PATH s'ouvre, integrite OK, ecriture possible (pas verrouillee
               par OneDrive/une autre instance), tables attendues presentes.
  2. Coinbase — en live, la cle API authentifie VRAIMENT (get_accounts read-only).
               config_validator voit juste "cle presente" ; une cle revoquee /
               expiree / mal copiee passe son controle mais fait tourner le bot
               AVEUGLE (tous les appels Coinbase echouent en silence). Ici on
               l'attrape au boot et on alerte.

Politique :
  - FATAL + PREFLIGHT_STRICT (defaut) -> notify puis sys.exit non-zero. Le wrapper
    run_with_restart re-alerte et relance avec backoff : bruyant et correct pour
    une config cassee (mieux qu'un trading aveugle silencieux).
  - WARN -> on continue (ex. erreur reseau transitoire : le bot a sa propre
    resilience/retry ; bloquer serait pire pendant une panne Coinbase).

Le trou F&G du boot est ferme separement par DirectorAgent.preflight_fg_gate().
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

EXIT_PREFLIGHT_FATAL = 3   # code de sortie dedie (run_with_restart traite tout !=0 comme crash)

# Marqueurs d'une erreur d'AUTHENTIFICATION/permission Coinbase (-> FATAL).
# Tout le reste (timeout, connexion, 5xx, inconnu) est traite comme transitoire
# (-> WARN) : on prefere laisser tourner un bot resilient qu'entrer en boot-loop
# pendant une panne reseau. Une vraie cle revoquee remonte de facon fiable l'un
# de ces marqueurs.
_AUTH_MARKERS = (
    "401", "403", "unauthorized", "forbidden", "authentication", "not authenticated",
    "invalid api", "invalid_api_key", "invalid signature", "invalid token",
    "permission", "expired",
)


class CheckResult:
    """Resultat d'un controle : level in {ok, warn, fatal}."""

    __slots__ = ("name", "level", "detail")

    def __init__(self, name: str, level: str, detail: str) -> None:
        self.name   = name
        self.level  = level
        self.detail = detail

    @property
    def is_fatal(self) -> bool:
        return self.level == "fatal"


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "ok", detail)


def _warn(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "warn", detail)


def _fatal(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "fatal", detail)


def _strict() -> bool:
    return os.getenv("PREFLIGHT_STRICT", "true").lower() in ("true", "1", "yes")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Base de donnees
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_TABLES = {"decisions", "portfolio_snapshots", "mcp_messages"}


def check_db() -> CheckResult:
    """Verifie que la DB s'ouvre, est saine, ecrivable, et a ses tables.

    Tables manquantes = base neuve/vierge -> WARN seulement (init_db les creera au
    demarrage du MemoryAgent) : c'est surtout un signal qu'un DB_PATH pointe peut-etre
    sur une base vide au lieu de l'historique attendu (cf. bug du 02/07)."""
    db_path = os.getenv("DB_PATH", "memory/trading.db")
    db_dir  = os.path.dirname(db_path) or "."

    if not os.path.isdir(db_dir):
        # config_validator promet une creation auto -> on cree le dossier ici plutot
        # qu'un FATAL (qui boot-looperait). init_db creera la DB au demarrage.
        try:
            os.makedirs(db_dir, exist_ok=True)
            log.info("preflight_db_dir_created", dir=db_dir)
        except Exception as exc:
            return _fatal("db", f"dossier '{db_dir}' absent et non creable : {exc}")

    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except Exception as exc:
        return _fatal("db", f"ouverture impossible : {exc} (DB_PATH={db_path})")

    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            return _fatal("db", f"integrite SQLite KO : {row} (DB_PATH={db_path})")

        # Ecriture / verrou : BEGIN IMMEDIATE prend un lock write tout de suite
        # (detecte base read-only ou verrouillee par OneDrive/une autre instance)
        # sans rien modifier.
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()

        tables  = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.OperationalError as exc:
        return _fatal("db", f"inaccessible/verrouillee : {exc} (DB_PATH={db_path})")
    except Exception as exc:
        return _fatal("db", f"erreur : {exc} (DB_PATH={db_path})")
    finally:
        conn.close()

    missing = REQUIRED_TABLES - tables
    if missing:
        return _warn("db", f"tables manquantes {sorted(missing)} — base neuve ? "
                           f"(DB_PATH={db_path})")
    return _ok("db", f"ouverte, saine, ecrivable ({db_path})")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Authentification Coinbase (live uniquement)
# ─────────────────────────────────────────────────────────────────────────────

def classify_coinbase_error(exc: Exception) -> str:
    """'auth' si l'erreur trahit une cle refusee/sans permission, sinon 'transient'.

    Fonction pure (testable sans reseau). Biais volontaire : seules les erreurs
    clairement d'auth sont fatales ; l'inconnu est traite comme transitoire pour
    ne pas boot-looper le bot pendant une simple panne reseau."""
    msg = str(exc).lower()
    if any(m in msg for m in _AUTH_MARKERS):
        return "auth"
    return "transient"


async def check_coinbase_auth(mode: str) -> CheckResult:
    """En live : prouve que la cle API authentifie, via le MEME chemin que le bot
    (CoinbaseClient.check_auth -> get_accounts read-only, aucun ordre)."""
    if mode != "live":
        return _ok("coinbase", "mode paper — auth non requise")

    if not os.getenv("COINBASE_API_KEY") or not os.getenv("COINBASE_API_SECRET"):
        return _fatal("coinbase", "COINBASE_API_KEY/SECRET manquants (mode live)")

    # Construction du client = _init_live() : cree le RESTClient (aucun reseau).
    # Une cle mal formee / le SDK absent leve ici -> config cassee = fatal.
    try:
        from interfaces.coinbase_client import CoinbaseClient
        client = CoinbaseClient()
    except Exception as exc:
        return _fatal("coinbase", f"init client live impossible : {exc}")

    try:
        res = await asyncio.wait_for(client.check_auth(), timeout=20)
    except asyncio.TimeoutError:
        return _warn("coinbase", "auth : timeout 20s (reseau ?) — le bot reessaiera en boucle")
    except Exception as exc:
        if classify_coinbase_error(exc) == "auth":
            return _fatal("coinbase", f"AUTH REFUSEE — cle revoquee/expiree/permissions ? : {exc}")
        return _warn("coinbase", f"auth : erreur transitoire ({str(exc)[:120]}) — le bot reessaiera")
    finally:
        try:
            await client.close()
        except Exception:
            pass

    n = res.get("n_accounts", "?")
    return _ok("coinbase", f"auth OK — {n} compte(s) lisibles (View)")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def _print_report(mode: str, results: list[CheckResult]) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    glyph = {"ok": "✅", "warn": "⚠️ ", "fatal": "❌"}
    print()
    print(f"  ── Preflight Kairos (mode {mode.upper()}) ──")
    for r in results:
        print(f"   {glyph.get(r.level, '?')} {r.name:<9} {r.detail}")
    print()


def _persist_state(mode: str, results: list[CheckResult]) -> None:
    """Ecrit le resultat du preflight dans <dossier DB>/preflight_state.json pour que
    l'appli (endpoint /api/health) affiche l'etat du dernier demarrage. Best-effort."""
    try:
        db_path = os.getenv("DB_PATH", "memory/trading.db")
        out = os.path.join(os.path.dirname(db_path) or ".", "preflight_state.json")
        payload = {
            "ts":     datetime.now(timezone.utc).isoformat(),
            "mode":   mode,
            "ok":     not any(r.is_fatal for r in results),
            "checks": [{"name": r.name, "level": r.level, "detail": r.detail} for r in results],
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as exc:
        log.warning("preflight_persist_failed", error=str(exc))


async def _notify_report(mode: str, results: list[CheckResult]) -> None:
    """Push/Telegram UNIQUEMENT s'il y a un warn/fatal (silencieux si tout OK)."""
    fatals = [r for r in results if r.level == "fatal"]
    warns  = [r for r in results if r.level == "warn"]
    if not fatals and not warns:
        return
    try:
        from interfaces import notifier
        head = ("🔴 *Kairos — preflight ECHEC (démarrage bloqué)*"
                if fatals else "🟠 *Kairos — preflight : avertissement*")
        lines = [f"• {r.name} : {r.detail}" for r in (fatals + warns)]
        tail  = ("\n\n_Le bot ne démarre pas tant que ce n'est pas corrigé._"
                 if fatals else "")
        await notifier.notify(f"{head}\n" + "\n".join(lines) + tail)
    except Exception as exc:
        log.warning("preflight_notify_failed", error=str(exc))


async def run(mode: str) -> list[CheckResult]:
    """Lance tous les controles, affiche/alerte, et sort (strict) si FATAL.

    Ne fait PAS le gate F&G (c'est DirectorAgent.preflight_fg_gate, appele apres
    la construction du Director). A appeler tot dans main(), avant de construire
    le swarm et de lancer les taches."""
    results = [check_db(), await check_coinbase_auth(mode)]
    _print_report(mode, results)
    _persist_state(mode, results)

    for r in results:
        lvl = log.error if r.is_fatal else (log.warning if r.level == "warn" else log.info)
        lvl("preflight_check", name=r.name, level=r.level, detail=r.detail)

    await _notify_report(mode, results)

    if any(r.is_fatal for r in results) and _strict():
        log.error("preflight_fatal_exit", code=EXIT_PREFLIGHT_FATAL)
        sys.exit(EXIT_PREFLIGHT_FATAL)

    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    _mode = os.getenv("COINBASE_MODE", "paper")
    asyncio.run(run(_mode))
    print("  Preflight termine.")
