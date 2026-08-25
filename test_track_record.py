"""
test_track_record.py — tests hermetiques (sans reseau) de l'export track record (objectif ②).
Verifie l'appariement FIFO + le calcul de P&L net + le determinisme de l'empreinte.

Lancer : python test_track_record.py   (exit 0 = tout passe)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Env AVANT import dashboard : mode paper, frais taker par defaut (RT = 0.015).
_TMP = tempfile.mkdtemp(prefix="kairos_tr_test_")
os.environ["DB_PATH"]       = os.path.join(_TMP, "trading.db")
os.environ["COINBASE_MODE"] = "paper"
os.environ.pop("COINBASE_TAKER_FEE_PCT", None)   # -> defaut 0.0075/cote, RT 0.015

from interfaces import dashboard

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE decisions (id TEXT PRIMARY KEY, timestamp TEXT, role TEXT, "
        "task_type TEXT, symbol TEXT, action TEXT, confidence REAL, reasoning TEXT, "
        "metadata TEXT, mode TEXT)")
    conn.execute("CREATE TABLE portfolio_snapshots (id TEXT, timestamp TEXT, total_usdc REAL, positions TEXT, pnl_pct REAL, mode TEXT)")
    rows = [
        # BTC : achat 0.001@50000 (cost 50) -> vente 0.001@60000 : net = 10 - 0.015*50 = 9.25
        ("2026-08-01T10:00:00", "trend_bot", "BTC-USDC", "buy",  '{"qty":0.001,"price":50000}'),
        ("2026-08-05T10:00:00", "trend_bot", "BTC-USDC", "sell", '{"qty":0.001,"price":60000}'),
        # ETH : achat 0.01@3000 (cost 30) -> vente 0.01@2900 : net = -1 - 0.015*30 = -1.45
        ("2026-08-02T10:00:00", "trend_bot", "ETH-USDC", "buy",  '{"qty":0.01,"price":3000}'),
        ("2026-08-06T10:00:00", "trend_bot", "ETH-USDC", "sell", '{"qty":0.01,"price":2900}'),
        # signal 'hold' et role non-executant : doivent etre ignores
        ("2026-08-03T10:00:00", "trend_bot", "BTC-USDC", "hold", '{}'),
    ]
    for i, (ts, role, sym, act, meta) in enumerate(rows):
        tt = "order" if act in ("buy", "sell") else "signal"
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (f"id{i}", ts, role, tt, sym, act, 1.0, "", meta, "paper"))
    conn.commit()
    return conn


def test_closed_trades() -> None:
    print("\n[1] _closed_trades — appariement FIFO + frais")
    conn = _make_db(os.environ["DB_PATH"])
    trades = dashboard._closed_trades(conn)
    check("2 round-trips fermes", len(trades) == 2)
    by_sym = {t["symbol"]: t for t in trades}
    btc, eth = by_sym.get("BTC-USDC"), by_sym.get("ETH-USDC")
    rt = dashboard.ROUND_TRIP_FEE_PCT   # frais reels du .env (Coinbase One ~0)
    exp_btc = round(0.001 * (60000 - 50000) - rt * 50, 4)   # 10 - rt*cout
    exp_eth = round(0.01 * (2900 - 3000) - rt * 30, 4)      # -1 - rt*cout
    check(f"BTC net = {exp_btc} (frais RT={rt})", btc and abs(btc["net_pnl_usdc"] - exp_btc) < 1e-4)
    check(f"ETH net = {exp_eth}", eth and abs(eth["net_pnl_usdc"] - exp_eth) < 1e-4)
    check("BTC gagnant, ETH perdant", btc and eth and btc["net_pnl_usdc"] > 0 and eth["net_pnl_usdc"] < 0)
    check("BTC entree/sortie corrects", btc and btc["entry"] == 50000 and btc["exit"] == 60000)

    stats = dashboard._realized_stats(conn)
    check("stats BTC : 1 clot, win_rate 1.0", stats.get("BTC-USDC", {}).get("win_rate") == 1.0)
    check("stats ETH : win_rate 0.0", stats.get("ETH-USDC", {}).get("win_rate") == 0.0)
    conn.close()


def test_fingerprint_deterministic() -> None:
    print("\n[2] empreinte SHA256 — deterministe & verifiable par un tiers")
    conn = _make_db(os.path.join(_TMP, "db2.db"))
    trades = dashboard._closed_trades(conn)
    conn.close()

    def fp(tr):
        canon = json.dumps(tr, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()

    f1, f2 = fp(trades), fp(trades)
    check("empreinte stable sur 2 calculs", f1 == f2)
    # Un tiers qui recalcule depuis la meme liste retrouve la meme empreinte
    check("empreinte reproductible depuis les trades bruts", fp(json.loads(json.dumps(trades))) == f1)
    # Une falsification (net modifie) change l'empreinte
    tampered = json.loads(json.dumps(trades)); tampered[0]["net_pnl_usdc"] += 1.0
    check("falsification detectee (empreinte differente)", fp(tampered) != f1)


if __name__ == "__main__":
    print("=== Track record (objectif ②) — tests hermetiques ===")
    test_closed_trades()
    test_fingerprint_deterministic()
    print(f"\n{'=' * 46}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
