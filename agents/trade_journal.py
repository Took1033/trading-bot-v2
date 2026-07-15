"""
TradeJournal - exporte toutes les decisions order en CSV pour audit/compta.

Format : timestamp, bot_id, symbol, action, qty, price, value_usdc, fee_estimate,
         pnl_pct, reason, confidence, mode

Lance comme tache asyncio : regenere journal.csv toutes les heures + apres chaque trade.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

DB_PATH     = os.getenv("DB_PATH", "memory/trading.db")
CSV_PATH    = Path(DB_PATH).parent / "trade_journal.csv"
INTERVAL_S  = int(os.getenv("JOURNAL_INTERVAL_S", "3600"))    # 1h par defaut


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def export_journal() -> int:
    """Genere le CSV. Retourne le nombre de lignes ecrites."""
    with _db() as conn:
        rows = conn.execute(
            # role IN (...) : orchestrator (scalpeur historique) + trend_bot
            # (bots actuels) + user (cloture manuelle). Le filtre orchestrator/
            # trend_bot seul faisait disparaitre les clotures manuelles du CSV.
            "SELECT timestamp, role, symbol, action, confidence, reasoning, metadata "
            "FROM decisions "
            "WHERE task_type='order' AND role IN ('orchestrator','trend_bot','user') "
            "AND action IN ('buy','sell') "
            "ORDER BY timestamp ASC"
        ).fetchall()

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    fieldnames = [
        "timestamp", "symbol", "action", "qty", "price",
        "value_usdc", "fee_estimate_usdc", "reason",
        "confidence", "order_id",
    ]

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
                qty   = float(meta.get("qty",   0))
                price = float(meta.get("price", 0))
                value = qty * price
                fee   = value * 0.012   # round-trip estimate

                writer.writerow({
                    "timestamp":         row["timestamp"],
                    "symbol":            row["symbol"],
                    "action":            row["action"],
                    "qty":               round(qty, 8),
                    "price":             round(price, 2),
                    "value_usdc":        round(value, 2),
                    "fee_estimate_usdc": round(fee, 4),
                    "reason":            (row["reasoning"] or "")[:200],
                    "confidence":        round(float(row["confidence"] or 0), 3),
                    "order_id":          meta.get("order_id", ""),
                })
                n_written += 1
            except Exception as exc:
                log.debug("journal_row_skip", error=str(exc))
                continue

    return n_written


async def journal_loop() -> None:
    """Boucle infinie : export toutes les INTERVAL_S secondes."""
    log.info("trade_journal_started", path=str(CSV_PATH), interval_s=INTERVAL_S)

    while True:
        try:
            await asyncio.sleep(INTERVAL_S)
            n = export_journal()
            log.info("trade_journal_exported", path=str(CSV_PATH), rows=n)
        except asyncio.CancelledError:
            log.info("trade_journal_stopped")
            # Dernier export propre avant arret
            try:
                export_journal()
            except Exception:
                pass
            raise
        except Exception as exc:
            log.warning("trade_journal_error", error=str(exc))
            await asyncio.sleep(60)
