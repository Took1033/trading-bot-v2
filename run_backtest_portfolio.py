"""
run_backtest_portfolio.py — backtest du BOOK REELLEMENT DEPLOYE (le bon objet).

Les 11 autres harnais mesurent un actif isole, all-in. Or le produit live est un
SWARM : N bots, chacun ~POSITION_PCT du NAV par position, sous un CAP d'exposition
combinee, le reste en cash. "1 edge prouve sur 5" repond a la mauvaise question tant
qu'on ne simule pas le portefeuille tel qu'il tourne.

Ce script rejoue les N actifs EN LOCKSTEP (meme calendrier daily), coordonne cash +
cap + rationnement (ordre de flotte), execution barre-suivante, net de frais. Il
compare, a BUDGET DE RISQUE EGAL (le cap) :
  - SWARM      : les 5 bots, POSITION_PCT chacun, plafonnes au CAP (etaler)
  - BTC_CONC   : BTC seul, dimensionne au CAP entier (concentrer)
  - HOLD_BTC   : CAP en BTC achete au depart + cash (buy&hold a expo egale)
  - CASH       : 100% cash (le vrai diversifiant du book)

But : la diversification TEMPORELLE des queues (BTC evite 2022, SOL explose 2023-24,
DOGE 2024-25 : fenetres disjointes) recompense-t-elle le swarm face a BTC concentre ?

NB : lancer sur la machine de Brice (truststore -> SSL Coinbase). Aucune donnee live.
Usage : python run_backtest_portfolio.py [--exit-buffer 0] [--cap 0.06] [--pos 0.03]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from edge_report import (  # reuse : fetch + decision + metriques (deja testes)
    GRAN_TO_PPY, GRANULARITY, INITIAL, LIVE_FLEET, REFERENCE_FEE, SMA_PERIOD,
    cagr_pct, compute_sharpe, fetch_daily, max_drawdown_pct, sortino, trend_decision,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

POSITION_PCT = 0.03   # TREND_POSITION_PCT (defaut prod)
CAP          = 0.06   # RISK_MAX_COMBINED_EXPOSURE_PCT (defaut documente)
MIN_TRADE    = 5.0    # TREND_MIN_USDC


def _precompute_signals(closes: list[float], exit_buf: float) -> list[str]:
    """Signal par barre (look-ahead-safe : closes[:i+1]). Meme logique que la prod."""
    sig = []
    for i in range(len(closes)):
        sig.append(trend_decision(closes[i], closes[: i + 1], SMA_PERIOD, 0.0, exit_buf))
    return sig


def simulate_book(assets: dict[str, dict], all_ts: list[int], order: list[str],
                  position_pct: float, cap: float, fee_rt: float) -> dict:
    """Rejoue un book multi-actifs coordonne. `assets[sym] = {ts->(price, signal)}`.
    Execution barre-suivante par actif ; sizing = min(pos%*NAV, place sous cap, cash).
    Rationnement : ordre de flotte (le 1er signal servi consomme le cap)."""
    fee_side = fee_rt / 2.0
    cash = INITIAL
    pos = {s: {"units": 0.0, "entry": 0.0, "in": False} for s in order}
    last_px = {s: 0.0 for s in order}
    pending = {s: None for s in order}
    curve: list[float] = []
    n_buys = 0
    max_expo_ratio = 0.0   # invariant : doit rester <= cap

    def nav() -> float:
        return cash + sum(pos[s]["units"] * last_px[s] for s in order if pos[s]["in"])

    def exposure() -> float:
        return sum(pos[s]["units"] * last_px[s] for s in order if pos[s]["in"])

    for ts in all_ts:
        # rafraichir les prix connus du jour
        for s in order:
            cell = assets[s].get(ts)
            if cell is not None:
                last_px[s] = cell[0]

        # 1) SORTIES d'abord (liberent cap + cash)
        for s in order:
            cell = assets[s].get(ts)
            if cell is None:
                continue
            price = cell[0]
            if pending[s] == "sell" and pos[s]["in"]:
                cash += pos[s]["units"] * price * (1 - fee_side)
                pos[s] = {"units": 0.0, "entry": 0.0, "in": False}
                pending[s] = None

        # 2) ENTREES ensuite, dans l'ordre de flotte (rationnement par le cap)
        for s in order:
            cell = assets[s].get(ts)
            if cell is None:
                continue
            price = cell[0]
            if pending[s] == "buy" and not pos[s]["in"]:
                cur_nav = nav()
                desired = position_pct * cur_nav
                cap_room = cap * cur_nav - exposure()
                spend = min(desired, max(0.0, cap_room), cash * 0.95)
                if spend >= MIN_TRADE and price > 0:
                    units = spend / price * (1 - fee_side)
                    cash -= spend
                    pos[s] = {"units": units, "entry": price, "in": True}
                    n_buys += 1
                pending[s] = None

        # 3) valeur du book (mark-to-market) + suivi de l'invariant d'exposition
        cur_nav = nav()
        curve.append(cur_nav)
        if cur_nav > 0:
            max_expo_ratio = max(max_expo_ratio, exposure() / cur_nav)

        # 4) decision du jour -> executera a la barre suivante de l'actif
        for s in order:
            cell = assets[s].get(ts)
            if cell is None:
                continue
            action = cell[1]
            if action == "buy" and not pos[s]["in"]:
                pending[s] = "buy"
            elif action == "sell" and pos[s]["in"]:
                pending[s] = "sell"
            else:
                pending[s] = None

    # liquidation finale
    for s in order:
        if pos[s]["in"]:
            cash += pos[s]["units"] * last_px[s] * (1 - fee_side)
            pos[s]["in"] = False
    if curve:
        curve[-1] = cash

    return {"equity": curve, "final": curve[-1] if curve else INITIAL,
            "n_buys": n_buys, "max_expo_ratio": round(max_expo_ratio, 4)}


def hold_at_cap(closes: list[float], warmup: int, cap: float, fee_rt: float) -> list[float]:
    """CAP investi en BTC a closes[warmup], reste cash, 1 A/R net."""
    fee_side = fee_rt / 2.0
    seg = closes[warmup:]
    if len(seg) < 2:
        return [INITIAL] * max(1, len(seg))
    invested = INITIAL * cap
    cash = INITIAL - invested
    units = invested / seg[0] * (1 - fee_side)
    curve = [cash + units * c for c in seg]
    curve[-1] = cash + units * seg[-1] * (1 - fee_side)
    return curve


def metrics(curve: list[float], ts_span_days: float, ppy: float) -> dict:
    post = curve
    return {
        "ret": round((curve[-1] / INITIAL - 1) * 100, 2),
        "cagr": cagr_pct(INITIAL, curve[-1], ts_span_days),
        "sharpe": compute_sharpe(post, ppy),
        "maxdd": max_drawdown_pct(post),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest du book deploye (swarm + cap)")
    ap.add_argument("--exit-buffer", type=float, default=0.0)
    ap.add_argument("--cap", type=float, default=CAP)
    ap.add_argument("--pos", type=float, default=POSITION_PCT)
    ap.add_argument("--days", type=int, default=1825)
    args = ap.parse_args()

    ppy = GRAN_TO_PPY[GRANULARITY]
    order = LIVE_FLEET[:]   # ordre de flotte (rationnement)

    # fetch + signaux par actif, alignes sur le calendrier commun
    per_sym = {}
    all_ts_set = set()
    for sym in order:
        candles = await fetch_daily(sym.replace("USDC", "USD"), args.days)
        ts = [c[0] for c in candles]
        closes = [c[1] for c in candles]
        sigs = _precompute_signals(closes, args.exit_buffer)
        per_sym[sym] = {ts[i]: (closes[i], sigs[i]) for i in range(len(ts))}
        all_ts_set.update(ts)
        await asyncio.sleep(0.35)
    all_ts = sorted(all_ts_set)
    span = (all_ts[-1] - all_ts[0]) / 86400.0

    # BTC seul pour les benchmarks concentres/hold
    btc = order[0]
    btc_ts = sorted(per_sym[btc].keys())
    btc_closes = [per_sym[btc][t][0] for t in btc_ts]

    swarm    = simulate_book(per_sym, all_ts, order, args.pos, args.cap, REFERENCE_FEE)
    btc_conc = simulate_book({btc: per_sym[btc]}, btc_ts, [btc], args.cap, args.cap, REFERENCE_FEE)
    hold_btc = hold_at_cap(btc_closes, SMA_PERIOD, args.cap, REFERENCE_FEE)

    m_swarm = metrics(swarm["equity"], span, ppy)
    m_conc  = metrics(btc_conc["equity"], (btc_ts[-1] - btc_ts[0]) / 86400.0, ppy)
    m_hold  = metrics(hold_btc, (btc_ts[-1] - btc_ts[SMA_PERIOD]) / 86400.0, ppy)

    print("=" * 76)
    print(f"  BOOK DEPLOYE — swarm {len(order)} bots · pos {args.pos*100:.0f}% · cap {args.cap*100:.0f}%"
          f" · sortie {args.exit_buffer:.1f}% · frais {REFERENCE_FEE*100:.1f}%")
    print(f"  {len(all_ts)} jours · budget de risque = {args.cap*100:.0f}% du NAV (reste en cash)")
    print("=" * 76)
    print(f"  {'CONFIG':<26}{'rendt':>8}{'CAGR':>8}{'Sharpe':>8}{'MaxDD':>8}")
    print("-" * 76)
    def row(name, m, extra=""):
        print(f"  {name:<26}{m['ret']:>+7.1f}%{m['cagr']:>+7.1f}%{m['sharpe']:>8.2f}{m['maxdd']:>7.1f}% {extra}")
    row(f"SWARM-{len(order)} (etaler 6%)", m_swarm, f"({swarm['n_buys']} entrees)")
    row("BTC concentre (6% BTC)", m_conc, f"({btc_conc['n_buys']} entrees)")
    row("HOLD BTC a 6% + cash", m_hold)
    print("-" * 76)
    print("  CASH 100%                    +0.0%   +0.0%    0.00    0.0%")
    print("=" * 76)
    # verdict de decision (aide, pas prescription)
    better_ret = "SWARM" if m_swarm["ret"] > m_conc["ret"] else "BTC-CONC"
    better_dd  = "SWARM" if m_swarm["maxdd"] < m_conc["maxdd"] else "BTC-CONC"
    better_sh  = "SWARM" if m_swarm["sharpe"] > m_conc["sharpe"] else "BTC-CONC"
    print(f"  A budget de risque egal (6%) : rendt -> {better_ret} | drawdown -> {better_dd} | Sharpe -> {better_sh}")
    print(f"  Lecture : les 4 alts AJOUTENT-ILS de la valeur au book, ou BTC concentre suffit-il ?")
    print(f"  (aide a la decision — la decision live et tout acte restent a Brice)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
