"""
watchdog.py — surveillance EXTERNE de Kairos Alpha.

A lancer par le planificateur Windows (toutes les heures). SILENCIEUX si tout
va bien ; envoie UN message Telegram seulement en cas d'anomalie. Anti-spam :
ne re-alerte pas pour la meme anomalie avant COOLDOWN_H heures.

Pourquoi un watchdog EXTERNE alors que le bot a deja des alertes internes ?
Parce que si le process entier meurt ou se fige, les alertes internes ne peuvent
plus partir. Ce script tourne EN DEHORS du bot et detecte precisement ce cas.

Self-locating : il se base sur l'emplacement du fichier (pas sur le cwd), donc
il marche quel que soit le lanceur (planificateur, double-clic, terminal).

Checks :
  1. Dashboard joignable      (http://localhost:PORT/api/portfolio)
  2. Log frais                (mtime de logs/trading.log < MAX_LOG_AGE_MIN)
  3. Drawdown                 (drawdown_pct du dashboard >= MAX_DD_PCT)
  4. Erreurs recentes         (lignes level=error sur la derniere heure)

Usage :
  python watchdog.py            # envoie Telegram si anomalie
  python watchdog.py --dry-run  # affiche seulement, n'envoie rien
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Se positionne dans le dossier du bot quel que soit le lanceur (le notifier et
# les chemins relatifs logs/ memory/ .env dependent du cwd).
BASE = Path(__file__).resolve().parent
os.chdir(BASE)

from dotenv import load_dotenv

load_dotenv()

DASHBOARD_PORT  = int(os.getenv("DASHBOARD_PORT", "8080"))
LOG_FILE        = Path(os.getenv("DB_PATH", "memory/trading.db")).parent / "logs" / "trading.log"
STATE_FILE      = Path(os.getenv("DB_PATH", "memory/trading.db")).parent / ".watchdog_state.json"
MAX_LOG_AGE_MIN = float(os.getenv("WATCHDOG_MAX_LOG_AGE_MIN", "15"))
MAX_DD_PCT      = float(os.getenv("WATCHDOG_MAX_DD_PCT", "6.0"))    # % sous le pic
MAX_ERRORS_1H   = int(os.getenv("WATCHDOG_MAX_ERRORS_1H", "20"))
COOLDOWN_H      = float(os.getenv("WATCHDOG_COOLDOWN_H", "6"))

DRY_RUN = "--dry-run" in sys.argv

# La console Windows (cp1252) ne sait pas encoder les emojis -> stdout en UTF-8
# best-effort pour que les prints (dry-run, debug) ne fassent jamais planter le script.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _dashboard() -> dict | None:
    url = f"http://localhost:{DASHBOARD_PORT}/api/portfolio"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _log_age_min() -> float | None:
    if not LOG_FILE.exists():
        return None
    return (time.time() - LOG_FILE.stat().st_mtime) / 60.0


def _recent_errors(window_min: float = 60.0) -> int:
    """Compte les lignes level=error des `window_min` dernieres minutes (tail du log)."""
    if not LOG_FILE.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - window_min * 60
    n = 0
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2_000_000))   # ~2 Mo de fin suffisent
            chunk = f.read().decode("utf-8", errors="ignore")
        for line in chunk.splitlines():
            if '"level": "error"' not in line:
                continue
            try:
                ts = json.loads(line).get("timestamp", "")
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t >= cutoff:
                n += 1
    except Exception:
        return 0
    return n


def _collect_problems() -> list[str]:
    problems: list[str] = []

    snap = _dashboard()
    if snap is None:
        problems.append(
            f"Dashboard injoignable sur localhost:{DASHBOARD_PORT} — bot MORT ou fige ?"
        )
    else:
        dd = snap.get("drawdown_pct")
        if isinstance(dd, (int, float)) and dd >= MAX_DD_PCT:
            problems.append(
                f"Drawdown {dd:.2f}% sous le pic (seuil {MAX_DD_PCT:.0f}%) — "
                f"capital {snap.get('total', 0):.2f} USDC."
            )

    age = _log_age_min()
    if age is None:
        problems.append("logs/trading.log introuvable.")
    elif age > MAX_LOG_AGE_MIN:
        problems.append(
            f"Log fige depuis {age:.0f} min (seuil {MAX_LOG_AGE_MIN:.0f}) — boucle bloquee ?"
        )

    errs = _recent_errors()
    if errs >= MAX_ERRORS_1H:
        problems.append(f"{errs} erreurs (level=error) sur la derniere heure (seuil {MAX_ERRORS_1H}).")

    return problems


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _send(text: str) -> None:
    if DRY_RUN:
        print("[DRY-RUN] Telegram qui SERAIT envoye :\n" + text)
        return
    try:
        import asyncio

        from interfaces import notifier
        asyncio.run(notifier.notify(text))
    except Exception as exc:
        print(f"Notif Telegram echouee : {exc}")


def main() -> int:
    problems = _collect_problems()
    now = time.time()

    if not problems:
        if _load_state().get("active") and not DRY_RUN:
            _save_state({"active": False, "ts": now})
        print(f"[{datetime.now().isoformat(timespec='seconds')}] OK — aucune anomalie.")
        return 0

    signature = " | ".join(problems)
    state = _load_state()
    if (state.get("signature") == signature
            and (now - state.get("ts", 0)) < COOLDOWN_H * 3600
            and not DRY_RUN):
        print("Anomalie identique encore en cooldown — pas de re-alerte.")
        return 1

    msg = ("🔴 *Watchdog Kairos — anomalie*\n"
           + "\n".join(f"• {p}" for p in problems)
           + f"\n\n_Verifie le bot._  ({datetime.now().strftime('%H:%M')})")
    _send(msg)
    if not DRY_RUN:
        _save_state({"active": True, "signature": signature, "ts": now})
    print("Alerte envoyee:", signature)
    return 1


if __name__ == "__main__":
    sys.exit(main())
