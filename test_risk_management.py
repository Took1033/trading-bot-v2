"""
test_risk_management.py — regression tests des correctifs risk-management (audit).

Couvre :
  [7] fenetre horaire : maxlen=240 (>1h) + _compute_hourly_loss trouve une reference
  [8] arret journalier : hold 24h via _maybe_release, et /release manuel relance la
      surveillance (plus de Director aveugle 24h)
  [6] cap d'exposition combinee applique a l'entree TrendBot

Lancer : python test_risk_management.py   (exit 0 = tout passe)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["DB_PATH"]            = os.path.join(tempfile.mkdtemp(prefix="kairos_risk_test_"), "trading.db")
os.environ["COINBASE_MODE"]      = "paper"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""

import asyncio
from collections import deque

from agents import trading_state
from agents.director_agent import DirectorAgent, RESUME_AFTER_MIN

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


class _Swarm:
    def __init__(self, value=200.0): self.value = value
    async def get_portfolio_total(self): return self.value


def test_hourly_window() -> None:
    print("\n[7] fenetre horaire du kill switch")
    d = DirectorAgent(_Swarm())
    check("deque maxlen=240 (>1h)", d._hourly_window.maxlen == 240)
    now = time.time()
    # une reference > 1h existe -> la perte horaire se calcule
    d._hourly_window = deque([(now - 4000, 100.0), (now - 100, 90.0)], maxlen=240)
    loss = d._compute_hourly_loss(now)
    check("perte horaire calculee (10%)", abs(loss - 0.10) < 1e-9)
    # sans reference > 1h -> 0 (comportement attendu, pas de faux positif)
    d._hourly_window = deque([(now - 100, 90.0)], maxlen=240)
    check("pas de reference >1h -> 0", d._compute_hourly_loss(now) == 0.0)


def test_daily_hold_and_resume() -> None:
    print("\n[8] arret journalier : hold 24h + reprise sur /release")
    trading_state.release_kill_switch()
    trading_state.clear_entry_grace()
    d = DirectorAgent(_Swarm(180.0))

    # simule un kill journalier arme il y a > RESUME_AFTER_MIN, hold encore actif
    now = time.time()
    d._kill_switch_at   = now - (RESUME_AFTER_MIN + 5) * 60
    d._daily_hold_until = now + 3600          # encore dans les 24h
    trading_state.kill_switch("Perte journaliere test - arret 24h")

    asyncio.run(d._maybe_release_kill_switch(now))
    check("hold 24h : kill switch maintenu malgre >60min", trading_state.is_kill_switch_active() is True)
    check("hold 24h : _daily_hold_until conserve", d._daily_hold_until > 0)

    # /release manuel (comme handle_release) : le Director ne doit PAS rester aveugle
    trading_state.release_kill_switch()
    d._initial_value = 180.0
    d._peak_value    = 180.0
    async def _fg(): return 50            # F&G neutre, pas de kill
    d._fetch_fear_greed = _fg             # type: ignore
    asyncio.run(d._check())
    check("release manuel -> _daily_hold_until leve", d._daily_hold_until == 0.0)
    check("release manuel -> bookkeeping kill reset", d._kill_switch_at == 0.0)
    check("release manuel -> surveillance active (kill toujours off)",
          trading_state.is_kill_switch_active() is False)

    trading_state.release_kill_switch()


def test_exposure_cap() -> None:
    print("\n[6] cap d'exposition combinee a l'entree")
    import agents.trend_bot as tb
    from strategies.simple_ma import Signal

    class _FakeMarket:
        def __init__(self, *a, **k):
            self._prices = []; self.price_history = []; self.is_warmed_up = True
        async def warmup_from_history(self): ...

    class _Coinbase:
        def __init__(self, total, free):
            self.snap = {"total_usdc": total, "usdc_balance": free}; self.orders = []
        async def get_portfolio_snapshot(self): return self.snap
        async def place_order(self, symbol, side, qty, force=False):
            self.orders.append((symbol, side, qty))
            class _O: order_id="t"; price=100.0
            return _O()

    class _Mem:
        def record_decision(self, **k): return "id"
        def record_snapshot(self, s): ...
        def last_entry_price(self, s): return None

    orig_market = tb.MarketAgent
    tb.MarketAgent = _FakeMarket
    orig_cap = tb.RISK_MAX_COMBINED_EXPOSURE_PCT
    try:
        sig = Signal("buy", 0.9, "test", "BTC-USDC", {})
        # cap 50%, deja 90% deploye (free faible) -> entree bloquee
        tb.RISK_MAX_COMBINED_EXPOSURE_PCT = 0.5
        cb = _Coinbase(total=1000.0, free=100.0)
        bot = tb.TrendBot(symbol="BTC-USDC", coinbase=cb, memory=_Mem(), weight=0.1)
        asyncio.run(bot._enter(100.0, sig))
        check("exposition au-dessus du cap -> aucun ordre", len(cb.orders) == 0)

        # cap 50%, 0% deploye (tout cash) -> entree autorisee
        cb2 = _Coinbase(total=1000.0, free=1000.0)
        bot2 = tb.TrendBot(symbol="BTC-USDC", coinbase=cb2, memory=_Mem(), weight=0.1)
        asyncio.run(bot2._enter(100.0, sig))
        check("sous le cap -> entree placee", len(cb2.orders) == 1)

        # cap desactive (1.0) -> comportement historique (entree placee malgre deploiement)
        tb.RISK_MAX_COMBINED_EXPOSURE_PCT = 1.0
        cb3 = _Coinbase(total=1000.0, free=200.0)
        bot3 = tb.TrendBot(symbol="BTC-USDC", coinbase=cb3, memory=_Mem(), weight=0.1)
        asyncio.run(bot3._enter(100.0, sig))
        check("cap=1.0 -> pas de plafond (entree placee)", len(cb3.orders) == 1)
    finally:
        tb.MarketAgent = orig_market
        tb.RISK_MAX_COMBINED_EXPOSURE_PCT = orig_cap


if __name__ == "__main__":
    print("=== Risk-management — regression tests ===")
    test_hourly_window()
    test_daily_hold_and_resume()
    test_exposure_cap()
    print(f"\n{'=' * 46}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
