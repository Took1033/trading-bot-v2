"""
MemoryAgent — seul agent autorisé à écrire en base SQLite.

Les autres agents passent par lui pour toute persistance.
Toutes les décisions sont immuables (INSERT OR IGNORE, jamais UPDATE).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import structlog
from dotenv import load_dotenv

from memory.schema import deterministic_uuid, init_db, new_decision

load_dotenv()
log = structlog.get_logger()

DB_PATH = os.getenv("DB_PATH", "memory/trading.db")


class MemoryAgent:
    """Passerelle unique pour la persistance SQLite."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._conn: sqlite3.Connection = init_db(db_path)
        log.info("memory_agent_ready", db=db_path)

    # ──────────────────────────────────────────────────────────────────────────
    # Décisions (immuables)
    # ──────────────────────────────────────────────────────────────────────────

    def record_decision(
        self,
        role: str,
        task_type: str,
        symbol: str | None = None,
        action: str | None = None,
        confidence: float | None = None,
        reasoning: str = "",
        metadata: str = "{}",
        mode: str = "paper",
    ) -> str:
        """Insert une décision et retourne son ID SHA256."""
        d = new_decision(
            role=role,
            task_type=task_type,
            symbol=symbol,
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            metadata=metadata,
            mode=mode,
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO decisions "
            "VALUES (:id,:timestamp,:role,:task_type,:symbol,:action,:confidence,:reasoning,:metadata,:mode)",
            d,
        )
        self._conn.commit()
        log.info(
            "decision_recorded",
            id=d["id"][:12],
            role=role,
            action=action,
            symbol=symbol,
        )
        return d["id"]

    # ──────────────────────────────────────────────────────────────────────────
    # Snapshots portefeuille
    # ──────────────────────────────────────────────────────────────────────────

    def record_snapshot(self, snapshot: dict) -> None:
        """Enregistre un snapshot de portefeuille."""
        ts = snapshot["timestamp"]
        uid = deterministic_uuid("portfolio", "snapshot", ts)
        self._conn.execute(
            "INSERT OR IGNORE INTO portfolio_snapshots VALUES (?,?,?,?,?,?)",
            (
                uid,
                ts,
                snapshot["total_usdc"],
                json.dumps(snapshot.get("positions", {})),
                snapshot.get("pnl_pct"),
                snapshot.get("mode", "paper"),
            ),
        )
        self._conn.commit()
        log.info(
            "snapshot_recorded",
            total_usdc=round(snapshot["total_usdc"], 2),
            pnl_pct=round(snapshot.get("pnl_pct") or 0.0, 4),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Messages MCP inter-agents
    # ──────────────────────────────────────────────────────────────────────────

    def record_mcp_message(self, msg: dict) -> None:
        """Enregistre un message MCP (control / artifact / error)."""
        ts = datetime.now(timezone.utc).isoformat()
        uid = deterministic_uuid(msg["sender"], msg["node_type"], ts)
        self._conn.execute(
            "INSERT OR IGNORE INTO mcp_messages VALUES (?,?,?,?,?,?,?,?)",
            (
                uid,
                ts,
                msg["sender"],
                msg["receiver"],
                msg["node_type"],
                json.dumps(msg.get("payload", {})),
                msg.get("timeout_ms"),
                msg.get("retry_count", 0),
            ),
        )
        self._conn.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # Lecture (pour les autres agents)
    # ──────────────────────────────────────────────────────────────────────────

    def get_recent_decisions(self, n: int = 10) -> list[dict]:
        """Retourne les N dernières décisions (plus récentes en premier)."""
        rows = self._conn.execute(
            "SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_action_for_symbol(self, symbol: str) -> str | None:
        """Retourne la dernière action exécutée sur un symbole."""
        row = self._conn.execute(
            "SELECT action FROM decisions "
            "WHERE symbol=? AND action IS NOT NULL AND task_type='order' "
            "AND role='orchestrator' "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return row["action"] if row else None

    def get_last_snapshot(self) -> dict | None:
        """Retourne le dernier snapshot de portefeuille."""
        row = self._conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["positions"] = json.loads(d["positions"])
        return d
