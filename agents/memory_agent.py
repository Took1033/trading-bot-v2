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
from pathlib import Path

import structlog
from dotenv import load_dotenv

from memory.schema import deterministic_uuid, init_db, new_decision

load_dotenv()
log = structlog.get_logger()

DB_PATH = os.getenv("DB_PATH", "memory/trading.db")
FIRST_LIVE_MARKER = Path(DB_PATH).parent / ".first_live_trade.txt"


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
        mode: str | None = None,
    ) -> str:
        """Insert une décision et retourne son ID SHA256.

        `mode=None` (défaut) ⇒ on lit COINBASE_MODE à l'exécution, pour que les
        ordres et signaux passés en live ne soient plus enregistrés à tort comme
        'paper' (audit fiable). Un appelant peut toujours forcer un mode explicite.
        """
        if mode is None:
            mode = os.getenv("COINBASE_MODE", "paper")
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

    def last_entry_price(self, symbol: str) -> float | None:
        """Prix du dernier ACHAT enregistré pour un symbole (depuis la DB).

        Sert à restaurer l'avg_price après un redémarrage : sans ça, une position
        ré-adoptée depuis le solde Coinbase est marquée au prix courant et le P&L
        affiché devient faux. Retourne None si aucun achat connu (ex: dépôt
        hors-bot) — l'appelant garde alors le prix courant.
        """
        row = self._conn.execute(
            "SELECT metadata FROM decisions "
            "WHERE symbol=? AND task_type='order' AND action='buy' "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if not row or not row["metadata"]:
            return None
        try:
            price = json.loads(row["metadata"]).get("price")
            return float(price) if price is not None and float(price) > 0 else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Marqueur "premier trade live" (fichier, hors DB)
    # ──────────────────────────────────────────────────────────────────────────

    def get_first_live_trade_ts(self) -> str | None:
        """Retourne le timestamp ISO du premier trade live, ou None si jamais."""
        if FIRST_LIVE_MARKER.exists():
            try:
                return FIRST_LIVE_MARKER.read_text(encoding="utf-8").strip()
            except Exception:
                return None
        return None

    def mark_first_live_trade(self) -> None:
        """Enregistre le timestamp du premier trade live (idempotent)."""
        if FIRST_LIVE_MARKER.exists():
            return
        try:
            FIRST_LIVE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            FIRST_LIVE_MARKER.write_text(
                datetime.now(timezone.utc).isoformat(),
                encoding="utf-8",
            )
            log.info("first_live_trade_marked", file=str(FIRST_LIVE_MARKER))
        except Exception as exc:
            log.warning("first_live_trade_mark_failed", error=str(exc))

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
