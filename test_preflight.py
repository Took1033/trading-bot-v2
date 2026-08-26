"""
test_preflight.py — tests hermetiques (sans reseau) du preflight de demarrage.

Couvre :
  - classify_coinbase_error : auth vs transitoire
  - check_db : ok / tables manquantes (warn) / dossier absent (fatal) / verrou (fatal)
  - check_coinbase_auth('paper') : no-op OK
  - trading_state : grace d'entree (entries_allowed / remaining)
  - DirectorAgent.preflight_fg_gate : arme le kill switch en Extreme Fear,
    ne fait rien si sain, pose une grace si F&G illisible
  - TrendBot : une entree BUY est BLOQUEE pendant la grace de boot, PASSE une fois levee
    (teste le vrai chemin d'ordre — la fenetre de boot est fermee)

Lancer : python test_preflight.py   (exit 0 = tout passe)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

# Env hermetique AVANT tout import projet (les modules font load_dotenv sans override,
# donc nos valeurs pre-posees gagnent sur le .env reel : pas de mode live, pas de notif).
_TMP = tempfile.mkdtemp(prefix="kairos_preflight_test_")
os.environ["DB_PATH"]          = os.path.join(_TMP, "trading.db")
os.environ["COINBASE_MODE"]    = "paper"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""
os.environ["PREFLIGHT_STRICT"] = "false"

import asyncio

import preflight
from agents import trading_state

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def _make_db(path: str, tables: list[str]) -> None:
    conn = sqlite3.connect(path)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id TEXT)")
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────

def test_classify() -> None:
    print("\n[1] classify_coinbase_error")
    auth_cases = [
        Exception("401 Unauthorized"),
        Exception("HTTP 403 Forbidden"),
        Exception("Invalid API key"),
        Exception("authentication failed"),
        Exception("API key expired"),
        Exception("missing required permission"),
    ]
    for e in auth_cases:
        check(f"auth: {e}", preflight.classify_coinbase_error(e) == "auth")

    transient_cases = [
        Exception("Connection timeout"),
        Exception("503 Service Unavailable"),
        Exception("rate limit exceeded (429)"),
        Exception("some weird unknown error"),   # inconnu -> transitoire (biais uptime)
    ]
    for e in transient_cases:
        check(f"transient: {e}", preflight.classify_coinbase_error(e) == "transient")


def test_check_db() -> None:
    print("\n[2] check_db")
    # OK : DB avec les 3 tables requises
    ok_db = os.path.join(_TMP, "ok.db")
    _make_db(ok_db, sorted(preflight.REQUIRED_TABLES))
    os.environ["DB_PATH"] = ok_db
    r = preflight.check_db()
    check(f"3 tables -> ok ({r.detail})", r.level == "ok")

    # WARN : base neuve (tables manquantes)
    empty_db = os.path.join(_TMP, "empty.db")
    _make_db(empty_db, [])
    os.environ["DB_PATH"] = empty_db
    r = preflight.check_db()
    check(f"tables manquantes -> warn ({r.level})", r.level == "warn")

    # dossier inexistant -> cree (aligne avec config_validator), non-fatal
    os.environ["DB_PATH"] = os.path.join(_TMP, "nope", "x.db")
    r = preflight.check_db()
    check(f"dossier absent -> cree, non-fatal ({r.level})", r.level != "fatal")
    check("dossier effectivement cree", os.path.isdir(os.path.join(_TMP, "nope")))

    # FATAL : base verrouillee (write lock tenu par une autre connexion)
    locked_db = os.path.join(_TMP, "locked.db")
    _make_db(locked_db, sorted(preflight.REQUIRED_TABLES))
    holder = sqlite3.connect(locked_db, timeout=1)
    holder.execute("BEGIN IMMEDIATE")   # garde le lock write
    os.environ["DB_PATH"] = locked_db
    try:
        r = preflight.check_db()
        check(f"DB verrouillee -> fatal ({r.level})", r.level == "fatal")
    finally:
        holder.rollback()
        holder.close()

    os.environ["DB_PATH"] = os.path.join(_TMP, "trading.db")   # reset


def test_auth_paper() -> None:
    print("\n[3] check_coinbase_auth('paper')")
    r = asyncio.run(preflight.check_coinbase_auth("paper"))
    check(f"paper -> ok sans reseau ({r.detail})", r.level == "ok")


def test_grace() -> None:
    print("\n[4] trading_state — grace d'entree")
    import time
    trading_state.clear_entry_grace()
    trading_state.release_kill_switch()
    check("entrees permises par defaut", trading_state.entries_allowed() is True)

    trading_state.set_entry_grace(time.time() + 30)
    check("grace future -> remaining > 0", trading_state.entry_grace_remaining() > 0)
    check("grace future -> entrees bloquees", trading_state.entries_allowed() is False)

    trading_state.set_entry_grace(time.time() - 5)   # passe -> ne recule pas la grace future
    check("set passe ne raccourcit pas une grace future", trading_state.entry_grace_remaining() > 0)

    trading_state.clear_entry_grace()
    check("grace levee -> entrees permises", trading_state.entries_allowed() is True)


def test_fg_gate() -> None:
    print("\n[5] DirectorAgent.preflight_fg_gate")
    from agents.director_agent import DirectorAgent

    class _DummySwarm:
        async def get_portfolio_total(self) -> float:
            return 200.0

    async def _run(fg_value):
        trading_state.release_kill_switch()
        trading_state.clear_entry_grace()
        d = DirectorAgent(_DummySwarm())

        async def _fake_fetch():
            if fg_value is not None:
                trading_state.set_fear_greed(fg_value, "Test")
                d._fg_label = "Test"
            return fg_value
        d._fetch_fear_greed = _fake_fetch   # type: ignore
        await d.preflight_fg_gate()
        return d

    # Extreme Fear -> kill switch arme, marque comme cause F&G (reprise correcte)
    d = asyncio.run(_run(10))
    check("F&G=10 -> kill switch actif", trading_state.is_kill_switch_active() is True)
    check("F&G=10 -> marque cause_fg", d._kill_is_fg is True)

    # Sain -> rien
    trading_state.release_kill_switch(); trading_state.clear_entry_grace()
    asyncio.run(_run(55))
    check("F&G=55 -> pas de kill switch", trading_state.is_kill_switch_active() is False)
    check("F&G=55 -> pas de grace", trading_state.entry_grace_remaining() == 0)

    # Illisible -> grace d'entree posee
    trading_state.release_kill_switch(); trading_state.clear_entry_grace()
    asyncio.run(_run(None))
    check("F&G illisible -> pas de kill switch", trading_state.is_kill_switch_active() is False)
    check("F&G illisible -> grace posee", trading_state.entry_grace_remaining() > 0)

    trading_state.release_kill_switch(); trading_state.clear_entry_grace()


def test_trend_entry_gated_during_grace() -> None:
    print("\n[6] TrendBot — entree BUY bloquee pendant la grace de boot")
    import time
    import agents.trend_bot as tb
    from strategies.simple_ma import Signal

    # Fakes hermetiques (aucun reseau)
    class _FakeMarket:
        def __init__(self, *a, **k):
            self._prices = []
            self.price_history = []
            self.is_warmed_up = True
        async def warmup_from_history(self): ...

    class _FakeCoinbase:
        def __init__(self): self.orders = []
        async def get_price(self, symbol): return 100.0
        def get_position(self, symbol): return None
        async def get_portfolio_snapshot(self):
            return {"total_usdc": 1000.0, "usdc_balance": 1000.0}
        async def place_order(self, symbol, side, qty, force=False):
            self.orders.append((symbol, side, qty))
            class _O:  # objet ordre minimal
                order_id = "test-order"
                price = 100.0
            return _O()

    class _FakeMemory:
        def record_decision(self, **k): return "id"
        def record_snapshot(self, snap): ...
        def last_entry_price(self, symbol): return None

    async def _fake_analyze(symbol, price):
        return Signal("buy", 0.9, "prix > SMA50 (test)", symbol, {"dist_pct": 5.0})

    orig_market, orig_analyze = tb.MarketAgent, tb.trend_analyze
    tb.MarketAgent, tb.trend_analyze = _FakeMarket, _fake_analyze
    try:
        cb = _FakeCoinbase()
        bot = tb.TrendBot(symbol="BTC-USDC", coinbase=cb, memory=_FakeMemory(), weight=0.1)
        trading_state.release_kill_switch()

        # Grace active -> aucune entree
        trading_state.set_entry_grace(time.time() + 60)
        asyncio.run(bot._tick())
        check("grace active -> aucun ordre place", len(cb.orders) == 0)

        # Grace levee + pas de kill switch -> l'entree passe
        trading_state.clear_entry_grace()
        asyncio.run(bot._tick())
        check("grace levee -> 1 ordre BUY place",
              len(cb.orders) == 1 and cb.orders[0][1] == "buy")

        # Kill switch actif -> bloque meme sans grace
        cb.orders.clear()
        trading_state.kill_switch("test")
        asyncio.run(bot._tick())
        check("kill switch actif -> aucun ordre", len(cb.orders) == 0)
    finally:
        tb.MarketAgent, tb.trend_analyze = orig_market, orig_analyze
        trading_state.release_kill_switch(); trading_state.clear_entry_grace()


if __name__ == "__main__":
    print("=== Preflight — tests hermetiques ===")
    test_classify()
    test_check_db()
    test_auth_paper()
    test_grace()
    test_fg_gate()
    test_trend_entry_gated_during_grace()
    print(f"\n{'=' * 42}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
