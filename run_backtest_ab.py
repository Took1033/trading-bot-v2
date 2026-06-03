"""
run_backtest_ab.py — A/B du gate de tendance sur donnees historiques.

Rejoue l'ENSEMBLE (multi_indicator + simple_ma + mean_reversion) sur des candles
Coinbase, avec et sans le gate de tendance (price > EMA50), et compare.
Les frais aller-retour reels (ROUND_TRIP_FEE_PCT) sont deduits pour un P&L net.

But : verifier si le gate ameliore vraiment les entrees (la "cure" du saignement),
plutot que de le deployer sur du capital reel a l'aveugle.

Usage :
    python run_backtest_ab.py                      # BTC/ETH/SOL, 90j, hourly
    python run_backtest_ab.py BTC-USDC 120 hourly
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

# Silence le bruit des strategies (un vote logge par tick) avant tout import.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

from strategies.backtester import Backtester
from strategies.ensemble import analyze as ensemble_analyze, Signal
from agents.orchestrator import ROUND_TRIP_FEE_PCT

DEFAULT_SYMBOLS = ["BTC-USDC", "ETH-USDC", "SOL-USDC"]


async def fetch_prices_coinbase(symbol: str, granularity: int = 3600, days: int = 90) -> list[float]:
    """Close prices depuis l'API publique Coinbase Exchange (boucle, 300 pts/req)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = f"https://api.exchange.coinbase.com/products/{symbol.replace('USDC', 'USD')}/candles"

    all_candles: list = []
    cursor_end = end
    async with aiohttp.ClientSession() as s:
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(seconds=granularity * 300))
            params = {"granularity": granularity,
                      "start": cursor_start.isoformat(), "end": cursor_end.isoformat()}
            async with s.get(url, params=params,
                             headers={"User-Agent": "TradingBot/2.0 (backtest)"}) as r:
                if r.status != 200:
                    break
                data = await r.json()
                if not data:
                    break
                all_candles.extend(data)
            cursor_end = cursor_start
            await asyncio.sleep(0.3)

    all_candles.sort(key=lambda c: c[0])
    return [float(c[4]) for c in all_candles]


def _gated_strategy(buffer: float = 0.0):
    """Wrapper : l'ensemble + gate de tendance (refuse BUY si price <= EMA50*(1+buffer))."""
    async def strat(symbol, prices, volumes=None):
        sig = await ensemble_analyze(symbol, prices, volumes)
        if sig.action == "buy":
            et = sig.metadata.get("ema_trend")
            if et and prices[-1] <= et * (1 + buffer):
                return Signal("hold", 0.5, "gate: contre-tendance", symbol, sig.metadata)
        return sig
    return strat


def _net_metrics(result) -> dict:
    """Recalcule return/win-rate NETS de frais en appariant buy->sell."""
    buys = [t for t in result.trades if t.side == "buy"]
    sells = [t for t in result.trades if t.side == "sell"]
    net_pnls = []
    for b, s in zip(buys, sells):
        notional = b.qty * b.price
        gross = s.qty * (s.price - b.price)
        fee = ROUND_TRIP_FEE_PCT * notional        # frais aller-retour sur la mise
        net_pnls.append(gross - fee)
    n = len(net_pnls)
    wins = sum(1 for p in net_pnls if p > 0)
    net_return = sum(net_pnls) / result.initial_usdc * 100
    return {
        "n_trades": n,
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "net_return_pct": round(net_return, 2),
        "gross_return_pct": result.total_return,
        "max_dd": result.max_drawdown,
    }


async def _run_symbol(symbol: str, days: int, granularity: int) -> dict | None:
    try:
        prices = await fetch_prices_coinbase(symbol, granularity, days)
    except Exception as exc:
        print(f"  {symbol}: fetch KO ({exc})")
        return None
    if len(prices) < 60:
        print(f"  {symbol}: pas assez de prix ({len(prices)})")
        return None

    bh = (prices[-1] / prices[0] - 1) * 100
    off = await Backtester(strategy=ensemble_analyze).run(symbol, prices)
    on  = await Backtester(strategy=_gated_strategy()).run(symbol, prices)
    return {
        "symbol": symbol, "n_prices": len(prices), "buy_hold": round(bh, 2),
        "off": _net_metrics(off), "on": _net_metrics(on),
    }


async def main() -> int:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    gran_name = sys.argv[3] if len(sys.argv) > 3 else "hourly"
    granularity = {"minute": 60, "5min": 300, "15min": 900,
                   "hourly": 3600, "6h": 21600, "daily": 86400}.get(gran_name, 3600)

    print("=" * 78)
    print(f"  A/B GATE DE TENDANCE — {days}j {gran_name} — frais round-trip {ROUND_TRIP_FEE_PCT:.2%}")
    print("=" * 78)
    print(f"  {'symbole':<10}{'pts':>6}{'buy&hold':>10} | "
          f"{'OFF net':>8}{'OFF win':>8}{'OFF n':>6} | {'ON net':>8}{'ON win':>8}{'ON n':>6}")
    print("  " + "-" * 74)

    rows = []
    for sym in symbols:
        r = await _run_symbol(sym, days, granularity)
        if not r:
            continue
        rows.append(r)
        o, n = r["off"], r["on"]
        print(f"  {sym:<10}{r['n_prices']:>6}{r['buy_hold']:>9.1f}% | "
              f"{o['net_return_pct']:>7.1f}%{o['win_rate']:>7.0f}%{o['n_trades']:>6} | "
              f"{n['net_return_pct']:>7.1f}%{n['win_rate']:>7.0f}%{n['n_trades']:>6}")

    if rows:
        print("  " + "-" * 74)
        off_tot = sum(r["off"]["net_return_pct"] for r in rows)
        on_tot = sum(r["on"]["net_return_pct"] for r in rows)
        print(f"  {'TOTAL net':<10}{'':>6}{'':>10} | {off_tot:>7.1f}%{'':>14} | {on_tot:>7.1f}%")
        print()
        verdict = "AMELIORE" if on_tot > off_tot else "N'AMELIORE PAS"
        print(f"  >>> Gate de tendance {verdict} le P&L net agrege : "
              f"{off_tot:+.1f}% -> {on_tot:+.1f}% ({on_tot - off_tot:+.1f} pts)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
