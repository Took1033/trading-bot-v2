"""
Initialisation de la base SQLite avec UUID déterministes (SHA256).
UUID = SHA256(role + task_type + timestamp_iso) → reproductible et auditable.
"""
import hashlib
import sqlite3
import os
from datetime import datetime, timezone


DB_PATH = os.getenv("DB_PATH", "memory/trading.db")


def deterministic_uuid(role: str, task_type: str, timestamp: str) -> str:
    payload = f"{role}:{task_type}:{timestamp}"
    return hashlib.sha256(payload.encode()).hexdigest()


SCHEMA = """
-- Décisions de trading (immuables, jamais de UPDATE)
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,          -- SHA256 déterministe
    timestamp   TEXT NOT NULL,             -- ISO 8601 UTC
    role        TEXT NOT NULL,             -- 'orchestrator' | 'market_agent' | 'risk_agent'
    task_type   TEXT NOT NULL,             -- 'signal' | 'order' | 'summary' | 'alert'
    symbol      TEXT,                      -- ex: 'BTC-USD'
    action      TEXT,                      -- 'buy' | 'sell' | 'hold' | NULL
    confidence  REAL,                      -- 0.0 - 1.0
    reasoning   TEXT,                      -- résumé structurel (jamais de prix brut)
    metadata    TEXT,                      -- JSON structurel additionnel
    mode        TEXT DEFAULT 'paper'       -- 'paper' | 'live'
);

-- État du portefeuille paper trading (snapshots horodatés)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    total_usdc  REAL NOT NULL,
    positions   TEXT NOT NULL,             -- JSON: {symbol: {qty, avg_price}}
    pnl_pct     REAL,                      -- % depuis snapshot précédent
    mode        TEXT DEFAULT 'paper'
);

-- Journal des messages MCP entre agents
CREATE TABLE IF NOT EXISTS mcp_messages (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    sender      TEXT NOT NULL,
    receiver    TEXT NOT NULL,
    node_type   TEXT NOT NULL,             -- 'control' | 'artifact' | 'error'
    payload     TEXT NOT NULL,             -- JSON
    timeout_ms  INTEGER,
    retry_count INTEGER DEFAULT 0
);

-- Index temporels pour les requêtes courantes
CREATE INDEX IF NOT EXISTS idx_decisions_ts     ON decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_mcp_sender       ON mcp_messages(sender, timestamp);
"""


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def new_decision(
    role: str,
    task_type: str,
    symbol: str | None = None,
    action: str | None = None,
    confidence: float | None = None,
    reasoning: str = "",
    metadata: str = "{}",
    mode: str = "paper",
) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    uid = deterministic_uuid(role, task_type, ts)
    return {
        "id": uid,
        "timestamp": ts,
        "role": role,
        "task_type": task_type,
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "reasoning": reasoning,
        "metadata": metadata,
        "mode": mode,
    }


if __name__ == "__main__":
    conn = init_db()
    print(f"Base initialisée : {DB_PATH}")
    print(f"Tables : {[r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type=?', ('table',))]}")
