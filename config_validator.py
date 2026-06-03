"""
Config validator — verifie la coherence de .env au demarrage.

Lance par main.py AVANT toute initialisation lourde.
Affiche un rapport clair et leve si la config est dangereusement invalide.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()


# Verifications par niveau de criticite
# (key, default, validator, level)
#   level = "error" : empêche le démarrage
#   level = "warn"  : affiche un warning mais continue


def _f(key: str, default: str = "") -> float:
    try:
        return float(os.getenv(key, default))
    except (ValueError, TypeError):
        return float("nan")


def _i(key: str, default: str = "") -> int:
    try:
        return int(os.getenv(key, default))
    except (ValueError, TypeError):
        return -1


def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def validate() -> tuple[list[str], list[str]]:
    """Retourne (errors, warnings). Si errors non-vide, on doit s'arreter."""
    errors:   list[str] = []
    warnings: list[str] = []

    mode = _s("COINBASE_MODE", "paper")
    if mode not in ("paper", "live"):
        errors.append(f"COINBASE_MODE='{mode}' invalide (doit être 'paper' ou 'live')")

    # ── Mode live : verifs strictes ─────────────────────────────────────────
    if mode == "live":
        if not _s("COINBASE_API_KEY"):
            errors.append("COINBASE_API_KEY manquant pour mode live")
        if not _s("COINBASE_API_SECRET"):
            errors.append("COINBASE_API_SECRET manquant pour mode live")

        initial = _s("LIVE_INITIAL_USDC")
        if not initial:
            warnings.append("LIVE_INITIAL_USDC vide — P&L base sur le solde actuel (peu fiable)")
        else:
            try:
                v = float(initial)
                if v <= 0:
                    errors.append(f"LIVE_INITIAL_USDC={initial} invalide (doit être > 0)")
            except ValueError:
                errors.append(f"LIVE_INITIAL_USDC={initial} n'est pas un nombre")

    # ── Risk params ─────────────────────────────────────────────────────────
    max_pct = _f("RISK_MAX_POSITION_PCT", "0.02")
    if max_pct != max_pct or max_pct <= 0 or max_pct > 0.50:
        errors.append(f"RISK_MAX_POSITION_PCT={max_pct} dangereux (doit être entre 0.001 et 0.5)")
    elif max_pct > 0.10:
        warnings.append(f"RISK_MAX_POSITION_PCT={max_pct*100:.1f}% est agressif (>10% par trade)")

    min_conf = _f("RISK_MIN_CONFIDENCE", "0.55")
    if min_conf < 0.50 or min_conf > 1.0:
        warnings.append(f"RISK_MIN_CONFIDENCE={min_conf} hors plage classique [0.50, 1.0]")

    sl = _f("RISK_STOP_LOSS_PCT", "0.03")
    tp = _f("RISK_TAKE_PROFIT_PCT", "0.05")
    if sl >= tp:
        warnings.append(f"SL ({sl*100:.1f}%) >= TP ({tp*100:.1f}%) — R/R degenere")

    # ── Telegram (warning si absent) ────────────────────────────────────────
    if not _s("TELEGRAM_BOT_TOKEN"):
        warnings.append("TELEGRAM_BOT_TOKEN absent — notifications désactivées")

    # ── Anthropic (warning si absent) ───────────────────────────────────────
    if not _s("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY absent — validation AI + narrations désactivées")

    # ── Loop interval ───────────────────────────────────────────────────────
    interval = _i("LOOP_INTERVAL_S", "60")
    if interval < 10:
        warnings.append(f"LOOP_INTERVAL_S={interval}s très court (risque rate limit Coinbase)")

    # ── DB path ─────────────────────────────────────────────────────────────
    db_path = _s("DB_PATH", "memory/trading.db")
    db_dir  = os.path.dirname(db_path) or "."
    if not os.path.exists(db_dir):
        warnings.append(f"DB_PATH dir '{db_dir}' n'existe pas — sera cree au demarrage")

    return errors, warnings


def report_and_exit_on_error() -> None:
    """Affiche le rapport et sort si erreur fatale."""
    # Console Windows en cp1252 : on force UTF-8 pour ne pas crasher sur les emojis.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    errors, warnings = validate()

    if warnings:
        print()
        print("  ⚠️   WARNINGS .env :")
        for w in warnings:
            print(f"      - {w}")

    if errors:
        print()
        print("  ❌  ERREURS .env (demarrage annule) :")
        for e in errors:
            print(f"      - {e}")
        print()
        print("  Corrige ton .env et relance.")
        sys.exit(1)


if __name__ == "__main__":
    report_and_exit_on_error()
    print("  ✅  Configuration .env valide")
