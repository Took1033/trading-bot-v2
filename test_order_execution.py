"""
test_order_execution.py — regression tests des correctifs d'execution d'ordres (audit).

  [4] _live_price ne blackliste JAMAIS un symbole detenu (reste pricable via fallback)
  [5] _sell_base_size ne fige pas un echec transitoire (re-tente jusqu'a un increment valide)
  [10] add_bot refuse un symbole deja trade par un autre bot
  [11] force_close reprend le bot si la vente echoue (sorties auto restent actives)

Lancer : python test_order_execution.py
"""
from __future__ import annotations

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["DB_PATH"]            = os.path.join(tempfile.mkdtemp(prefix="kairos_oe_test_"), "trading.db")
os.environ["COINBASE_MODE"]      = "paper"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""

import asyncio

from interfaces.coinbase_client import CoinbaseClient

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def _live_client() -> CoinbaseClient:
    c = CoinbaseClient()
    c.mode = "live"          # force le chemin live pour le test (aucun reseau reel)
    return c


def test_blacklist_protects_held() -> None:
    print("\n[4] blacklist : jamais sur un actif detenu")

    class FakeBidAskRaises:
        def get_best_bid_ask(self, product_ids=None):
            raise Exception("400 Bad Request: invalid product_id")

    c = _live_client()
    c._real_client = FakeBidAskRaises()
    async def _fallback(sym): return 100.0
    c._paper_price = _fallback          # type: ignore  (evite le reseau)

    # symbole NON detenu -> blacklist + raise
    async def _untracked():
        try:
            await c._live_price("ACX-USDC")
            return False
        except Exception:
            return "ACX-USDC" in c._invalid_products
    check("symbole non detenu -> blackliste", asyncio.run(_untracked()) is True)

    # symbole DETENU -> jamais blackliste, fallback prix public
    c._live_port.positions["BTC-USDC"] = {"qty": 0.001, "avg_price": 100.0}
    price = asyncio.run(c._live_price("BTC-USDC"))
    check("actif detenu -> non blackliste", "BTC-USDC" not in c._invalid_products)
    check("actif detenu -> prix via fallback", price == 100.0)


def test_base_increment_cache() -> None:
    print("\n[5] base_increment : pas de cache d'echec")

    class FakeProduct:
        def __init__(self, inc): self.base_increment = inc

    class FakeClient:
        def __init__(self): self.calls = 0
        def get_product(self, symbol):
            self.calls += 1
            if self.calls == 1:
                raise Exception("produit indisponible")   # non-retryable -> 1 echec net
            return FakeProduct("0.001")

    c = _live_client()
    c._real_client = FakeClient()

    # 1er appel : echec get_product -> fallback 8 decimales, RIEN mis en cache
    s1 = asyncio.run(c._sell_base_size("HYPE-USDC", 0.00376521))
    check("echec -> fallback 8 decimales", s1 == "0.00376521")
    check("echec -> increment NON mis en cache", "HYPE-USDC" not in c._base_incr_cache)

    # 2e appel : get_product OK (0.001) -> increment valide, floor correct
    s2 = asyncio.run(c._sell_base_size("HYPE-USDC", 0.00376521))
    check("succes -> increment cache", c._base_incr_cache.get("HYPE-USDC") == "0.001")
    check("succes -> taille floor au pas (0.003)", s2 == "0.003")


def test_duplicate_symbol_rejected() -> None:
    print("\n[10] add_bot refuse un symbole deja trade")
    from agents.bot_swarm import BotSwarm
    sw = BotSwarm()
    existing = sw.bots[0].symbol          # ex: BTC-USDC (trend_btc)
    res = asyncio.run(sw.add_bot("dup1", existing))
    check(f"symbole {existing} deja pris -> refuse", res.get("ok") is False and "déjà" in (res.get("error") or ""))


def test_force_close_resumes_on_failure() -> None:
    print("\n[11] force_close reprend le bot si la vente echoue")
    from agents.bot_swarm import BotSwarm
    from agents import trading_state
    sw = BotSwarm()
    bot_id = sw.bots[0].bot_id

    class FakeCoinbase:
        def get_position(self, symbol): return {"qty": 0.01, "avg_price": 100.0}
        async def place_order(self, symbol, side, qty, force=False):
            raise Exception("500 erreur reseau persistante")
        async def get_portfolio_snapshot(self): return {"total_usdc": 200.0, "usdc_balance": 100.0}
    sw._coinbase = FakeCoinbase()

    trading_state.resume(bot_id)
    res = asyncio.run(sw.force_close(bot_id))
    check("vente echouee -> ok False", res.get("ok") is False)
    check("vente echouee -> paused False (bot repris)", res.get("paused") is False)
    check("vente echouee -> bot PAS en pause", trading_state.is_bot_paused(bot_id) is False)
    trading_state.resume(bot_id)


if __name__ == "__main__":
    print("=== Order execution — regression tests ===")
    test_blacklist_protects_held()
    test_base_increment_cache()
    test_duplicate_symbol_rejected()
    test_force_close_resumes_on_failure()
    print(f"\n{'=' * 46}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
