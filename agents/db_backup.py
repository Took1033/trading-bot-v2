"""
DBBackup - copie atomique de trading.db toutes les N heures.

Garde les 10 derniers backups (rotation automatique).
Utilise sqlite3.Connection.backup() pour une copie thread-safe et atomique
(meme si le bot ecrit en parallele).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

DB_PATH         = os.getenv("DB_PATH", "memory/trading.db")
# DB_BACKUP_DIR decouple du dossier de la DB : permet une DB vive hors OneDrive
# (pas de locks/sync sur SQLite) tout en gardant les copies froides DANS
# OneDrive comme sauvegarde cloud.
BACKUP_DIR      = Path(os.getenv("DB_BACKUP_DIR", "") or (Path(DB_PATH).parent / "backups"))
BACKUP_INTERVAL = int(os.getenv("DB_BACKUP_INTERVAL_H", "6")) * 3600
MAX_BACKUPS     = int(os.getenv("DB_BACKUP_KEEP",       "10"))


def _backup_once() -> Path:
    """Backup atomique. Retourne le path du fichier cree."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts        = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest      = BACKUP_DIR / f"trading_{ts}.db"

    src_conn  = sqlite3.connect(DB_PATH)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return dest


def _rotate() -> int:
    """Supprime les vieux backups, garde les MAX_BACKUPS plus recents."""
    if not BACKUP_DIR.exists():
        return 0
    files = sorted(BACKUP_DIR.glob("trading_*.db"), key=os.path.getmtime, reverse=True)
    removed = 0
    for old in files[MAX_BACKUPS:]:
        try:
            old.unlink()
            removed += 1
        except Exception:
            pass
    return removed


async def backup_loop() -> None:
    """Boucle infinie : backup toutes les BACKUP_INTERVAL secondes."""
    log.info("db_backup_started",
             interval_h=BACKUP_INTERVAL / 3600,
             keep=MAX_BACKUPS, dir=str(BACKUP_DIR))

    while True:
        try:
            await asyncio.sleep(BACKUP_INTERVAL)
            dest = _backup_once()
            removed = _rotate()
            log.info("db_backup_done",
                     file=str(dest.name),
                     size_kb=round(dest.stat().st_size / 1024, 1),
                     rotated=removed)
        except asyncio.CancelledError:
            log.info("db_backup_stopped")
            raise
        except Exception as exc:
            log.warning("db_backup_error", error=str(exc))
            await asyncio.sleep(600)
