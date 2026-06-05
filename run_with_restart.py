"""
run_with_restart.py - Lance main.py et le relance automatiquement en cas de crash.

Usage :
    python run_with_restart.py

Comportement :
  - Sortie 0 (Ctrl+C) -> stop propre, pas de relance
  - Sortie != 0 (crash) -> relance apres BACKOFF secondes
  - Backoff exponentiel borne (max 5 min entre 2 tentatives)
  - Log chaque relance dans logs/restart.log
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_FILE       = Path("logs") / "restart.log"
INITIAL_BACKOFF = 5      # secondes
MAX_BACKOFF     = 300    # 5 minutes
SCRIPT          = "main.py"


def log(msg: str) -> None:
    """Log dans la console + fichier."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify_telegram(msg: str) -> None:
    """Best-effort Telegram — ne doit JAMAIS empecher le restart du bot."""
    try:
        import asyncio

        from interfaces import notifier
        asyncio.run(notifier.notify(msg))
    except Exception as exc:
        log(f"Notif Telegram echouee (ignore): {exc}")


def main() -> int:
    log("=" * 60)
    log(f"Wrapper auto-restart demarre - cible: {SCRIPT}")
    log("=" * 60)

    backoff   = INITIAL_BACKOFF
    n_crashes = 0

    while True:
        log(f"Lancement {SCRIPT}...")
        start_ts = time.time()

        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT],
                check=False,
            )
        except KeyboardInterrupt:
            log("Ctrl+C recu - arret du wrapper.")
            return 0

        runtime = time.time() - start_ts
        log(f"{SCRIPT} termine (exit={proc.returncode}, runtime={runtime:.0f}s)")

        # Sortie propre (Ctrl+C transmis ou exit 0) -> stop
        if proc.returncode == 0:
            log("Sortie propre - wrapper termine.")
            return 0

        n_crashes += 1
        log(f"Crash #{n_crashes} detecte (exit={proc.returncode})")

        # Si le bot a tenu > 10 min, reset le backoff
        if runtime > 600:
            backoff = INITIAL_BACKOFF
            log(f"Le bot a tenu {runtime:.0f}s - backoff reset a {backoff}s")
        else:
            backoff = min(backoff * 2, MAX_BACKOFF)

        notify_telegram(
            f"💥 *Kairos a crashé* (#{n_crashes})\n"
            f"Code de sortie : `{proc.returncode}` — uptime `{runtime:.0f}s`.\n"
            f"Relance automatique dans `{backoff}s`."
        )
        log(f"Relance dans {backoff}s...")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            log("Ctrl+C pendant le backoff - arret du wrapper.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
