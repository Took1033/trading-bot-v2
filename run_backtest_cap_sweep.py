"""
run_backtest_cap_sweep.py — le cap est-il au bon niveau ? (la vraie variable de risque)

La reflexion strategique l'a identifie : le rapport optimise le SIGNAL, mais le book
est domine par le CAP d'exposition combinee (6% aujourd'hui), jamais backteste. Trop
bas, il etouffe l'edge prouve de BTC ; trop haut, il expose aux drawdowns correles.

Ce script balaie le cap (sizing par bot fixe a 3%) et montre, pour chaque niveau, le
SWARM (etaler) et BTC concentre (cap entier sur BTC). Fetch UNE fois, rejoue N caps.

NB : lancer sur la machine de Brice (truststore). Aucune donnee live.
Usage : python run_backtest_cap_sweep.py
"""
from __future__ import annotations

import asyncio
import sys

from edge_report import (
    GRAN_TO_PPY, GRANULARITY, LIVE_FLEET, REFERENCE_FEE, fetch_daily,
)
from run_backtest_portfolio import _precompute_signals, metrics, simulate_book

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CAPS = [0.02, 0.04, 0.06, 0.10, 0.15, 0.30]   # 6% = actuel
POS  = 0.03                                    # sizing par bot (fixe, comme prod)


async def main() -> int:
    ppy = GRAN_TO_PPY[GRANULARITY]
    order = LIVE_FLEET[:]
    per_sym = {}
    all_ts_set = set()
    for sym in order:
        candles = await fetch_daily(sym.replace("USDC", "USD"), 1825)
        ts = [c[0] for c in candles]; closes = [c[1] for c in candles]
        sigs = _precompute_signals(closes, 0.0)
        per_sym[sym] = {ts[i]: (closes[i], sigs[i]) for i in range(len(ts))}
        all_ts_set.update(ts)
        await asyncio.sleep(0.35)
    all_ts = sorted(all_ts_set)
    span = (all_ts[-1] - all_ts[0]) / 86400.0
    btc = order[0]; btc_ts = sorted(per_sym[btc].keys())
    btc_span = (btc_ts[-1] - btc_ts[0]) / 86400.0

    print("=" * 82)
    print(f"  SWEEP DU CAP — sizing par bot fixe {POS*100:.0f}% · flip strict · frais {REFERENCE_FEE*100:.1f}% · 5 ans")
    print("  Question : 6% est-il le bon niveau ? (trop bas etouffe BTC, trop haut expose aux DD correles)")
    print("=" * 82)
    print(f"  {'CAP':>5} | {'SWARM-5 (etaler)':^30} | {'BTC concentre':^30}")
    print(f"  {'':>5} | {'rendt':>8}{'Sharpe':>8}{'MaxDD':>8}{'expo':>6} | {'rendt':>8}{'Sharpe':>8}{'MaxDD':>8}")
    print("-" * 82)
    for cap in CAPS:
        pos = min(POS, cap)
        sw = simulate_book(per_sym, all_ts, order, pos, cap, REFERENCE_FEE)
        cc = simulate_book({btc: per_sym[btc]}, btc_ts, [btc], cap, cap, REFERENCE_FEE)
        ms = metrics(sw["equity"], span, ppy)
        mc = metrics(cc["equity"], btc_span, ppy)
        mark = "  <= actuel" if abs(cap - 0.06) < 1e-9 else ""
        print(f"  {cap*100:>4.0f}% | {ms['ret']:>+7.1f}%{ms['sharpe']:>8.2f}{ms['maxdd']:>7.1f}%"
              f"{sw['max_expo_ratio']*100:>5.0f}% | {mc['ret']:>+7.1f}%{mc['sharpe']:>8.2f}{mc['maxdd']:>7.1f}%{mark}")
    print("=" * 82)
    print("  Lecture : le Sharpe se degrade-t-il quand le cap monte (DD correles) ? Le rendement")
    print("  augmente-t-il assez pour le justifier ? = l'arbitrage rendement/risque du dimensionnement.")
    print("  (aide a la decision — la decision live et tout acte restent a Brice)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
