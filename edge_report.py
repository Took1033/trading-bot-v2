"""
edge_report.py — la CARTE D'IDENTITE reproductible de l'edge Kairos (Axe 3).

Rejoue la strategie de PRODUCTION (trend-following SMA daily, long-only, flip a la
SMA) sur les actifs du fleet live, et produit un rapport de PREUVE d'edge concu pour
ne PAS mentir — ni par bug, ni par optimisme, ni par cherry-picking.

Garde-fous scientifiques (spec quant) :
  - Moteur ALL-IN, NET DE FRAIS (le Backtester par defaut ignore les frais et ne
    deploie que 2 % : non representatif du live). Frais par cote = FEE_RT / 2.
  - Anti look-ahead : decision a la cloture du jour, EXECUTION A LA BARRE SUIVANTE.
  - Walk-forward : l'edge tient-il HORS echantillon, ou est-ce une seule fenetre bull ?
  - Table de SENSIBILITE AUX FRAIS : l'edge survit-il aux frais reels, ou seulement
    au regime 0-frais (Coinbase One) ?
  - Benchmark buy&hold EQUITABLE (meme periode active, memes frais).
  - Detection des TROUS de donnees (ex. XRP suspendu ~2021-2023 par le proces SEC).
  - GATE GO/NO-GO : "EDGE PROUVE" seulement si 7 criteres passent ; sinon le rapport
    DIT pourquoi il refuse de conclure (INSUFFISANT / NON PROBANT / MITIGE).
  - Reproductibilite : provenance figee + empreinte SHA256 des donnees et resultats.

Usage :
    python edge_report.py                       # fleet live par defaut, 5 ans
    python edge_report.py --days 1825 --out rapport.json
    python edge_report.py --symbols BTC-USDC,ETH-USDC

NB : lancer sur la machine de Brice (truststore -> SSL Coinbase OK).
"""
from __future__ import annotations

# truststore EN PREMIER (avant tout import reseau) : delegue la validation TLS au
# magasin Windows, seule facon de joindre Coinbase quand un AV (Avast) intercepte le HTTPS.
try:
    import truststore
    truststore.inject_into_ssl()
    _TRUSTSTORE = True
except Exception:
    _TRUSTSTORE = False

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

import aiohttp

from strategies.backtester import (
    compute_exposure,
    compute_profit_factor,
    compute_sharpe,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# Config de PRODUCTION (source : recon config, .env.example + defauts code)
# ─────────────────────────────────────────────────────────────────────────────

# Fleet LIVE reel (per Brice) — PAS le config/bots.json du worktree, qui est perime
# (il declare 7 bots aave/near/hype/link). Configurable via --symbols.
LIVE_FLEET = ["BTC-USDC", "ETH-USDC", "SOL-USDC", "XRP-USDC", "DOGE-USDC"]

SMA_PERIOD = 50      # TREND_SMA_PERIOD
ENTRY_BUF  = 0.0     # TREND_ENTRY_BUFFER_PCT -> flip strict a la SMA
EXIT_BUF   = 0.0     # TREND_EXIT_BUFFER_PCT
GRANULARITY = 86400  # daily
INITIAL = 10_000.0

# Scenarios de frais (round-trip). Coinbase One ~0, mais on PROUVE la robustesse.
FEE_SCENARIOS = [
    ("Coinbase One (revendique)", 0.0),
    ("Realiste bas",              0.002),
    ("Taker live observe",        0.015),
]
REFERENCE_FEE = 0.002   # le scenario de reference pour les verdicts (prudent, pas 0)

# Annualisation du Sharpe : crypto 24/7 => 365, JAMAIS 252 (actions).
GRAN_TO_PPY = {86400: 365, 21600: 365 * 4, 3600: 365 * 24}

# Seuils du gate de publication.
MIN_CANDLES  = 365 * 2   # >= 2 ans de daily
MIN_TRADES   = 20        # < 20 round-trips = non significatif
K_WINDOWS    = 5         # fenetres walk-forward
MAX_WINDOW_SHARE = 0.60  # une fenetre ne doit pas porter >60 % du gain


# ─────────────────────────────────────────────────────────────────────────────
# Metriques PURES (testables sans reseau)
# ─────────────────────────────────────────────────────────────────────────────

def max_drawdown_pct(equity: list[float]) -> float:
    """Pire recul depuis un pic (mark-to-market)."""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > mdd:
                mdd = dd
    return round(mdd, 2)


def time_underwater(equity: list[float]) -> tuple[float, int]:
    """(% de barres sous le pic precedent, plus longue serie consecutive sous l'eau)."""
    if not equity:
        return 0.0, 0
    peak = equity[0]
    under = streak = max_streak = 0
    for v in equity:
        if v >= peak:
            peak = v
            streak = 0
        else:
            under += 1
            streak += 1
            max_streak = max(max_streak, streak)
    return round(under / len(equity) * 100, 1), max_streak


def sortino(rets: list[float], periods_per_year: float) -> float:
    """Comme le Sharpe mais penalise uniquement la volatilite BAISSIERE."""
    if len(rets) < 2:
        return 0.0
    downside = [min(r, 0.0) for r in rets]
    dd = math.sqrt(sum(x * x for x in downside) / len(downside))
    if dd <= 0:
        return 0.0
    return round(statistics.fmean(rets) / dd * math.sqrt(periods_per_year), 3)


def cagr_pct(initial: float, final: float, span_days: float) -> float:
    """Taux de croissance annuel compose, sur le span CALENDAIRE reel (pas n_candles)."""
    if initial <= 0 or span_days <= 0:
        return 0.0
    years = span_days / 365.0
    if years <= 0:
        return 0.0
    return round(((final / initial) ** (1.0 / years) - 1.0) * 100, 2)


def calmar(cagr_value_pct: float, mdd_pct: float) -> float | None:
    """CAGR / MaxDD. None si aucun drawdown (indefini)."""
    if mdd_pct <= 0:
        return None
    return round(cagr_value_pct / mdd_pct, 2)


def wilson_ci(wins: int, n: int) -> tuple[float, float]:
    """Intervalle de confiance de Wilson a 95 % sur le win-rate (%). Coupe court a la
    sur-interpretation d'un win-rate sur peu de trades."""
    if n <= 0:
        return 0.0, 0.0
    z = 1.96
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round((centre - half) * 100, 1), round((centre + half) * 100, 1)


def sha256_floats(values: list[float]) -> str:
    """Empreinte deterministe d'une serie de nombres (donnees d'entree du rapport)."""
    canon = ",".join(repr(round(float(v), 8)) for v in values)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Decision de PROD (inline, prouve identique a compute_trend_signal par test)
# ─────────────────────────────────────────────────────────────────────────────

def trend_decision(price: float, window: list[float], sma_period: int,
                   entry_buf: float, exit_buf: float) -> str:
    """Replique EXACTE de compute_trend_signal (strategies/trend_daily.py) : hold tant
    que < sma_period clotures ; sinon buy si price > SMA*(1+entry%), sell si
    price < SMA*(1-exit%), hold dans la bande. `window` inclut la barre courante
    (fidele au flux live : la SMA inclut le point courant)."""
    if len(window) < sma_period:
        return "hold"
    sma = sum(window[-sma_period:]) / sma_period
    if price > sma * (1 + entry_buf / 100):
        return "buy"
    if price < sma * (1 - exit_buf / 100):
        return "sell"
    return "hold"


# ─────────────────────────────────────────────────────────────────────────────
# Moteur de simulation : ALL-IN, NET DE FRAIS, execution barre suivante
# ─────────────────────────────────────────────────────────────────────────────

def simulate(closes: list[float], sma_period: int, fee_rt: float,
             entry_buf: float, exit_buf: float) -> dict:
    """Rejoue le trend long-only all-in. Decision a la cloture i (info connue),
    EXECUTION a la cloture i+1 (anti same-bar look-ahead). Retourne courbe d'equite,
    trades clotures et P&L par trade."""
    fee_side = fee_rt / 2.0
    cash = INITIAL
    units = 0.0
    in_pos = False
    entry_price = 0.0
    curve: list[float] = []
    flags: list[bool] = []   # en position a chaque barre (pour l'exposition)
    pnls: list[float] = []   # P&L % par round-trip cloture
    trades: list[dict] = []  # journal d'audit (side, idx, price)
    n_buys = 0
    pending: str | None = None

    for i, price in enumerate(closes):
        # 1) executer l'ordre decide a la barre PRECEDENTE (a ce prix-ci)
        if pending == "buy" and not in_pos:
            units = cash / price * (1 - fee_side)
            cash = 0.0
            entry_price = price
            in_pos = True
            n_buys += 1
            trades.append({"side": "buy", "idx": i, "price": price})
        elif pending == "sell" and in_pos:
            cash = units * price * (1 - fee_side)
            pnls.append((price / entry_price - 1) * 100)
            trades.append({"side": "sell", "idx": i, "price": price})
            units = 0.0
            in_pos = False
        pending = None

        # 2) valeur du portefeuille (mark-to-market) + etat de position
        curve.append(cash + units * price)
        flags.append(in_pos)

        # 3) decider a la cloture i (executera a i+1) — seulement s'il reste une barre
        if i + 1 < len(closes):
            action = trend_decision(price, closes[: i + 1], sma_period, entry_buf, exit_buf)
            if action == "buy" and not in_pos:
                pending = "buy"
            elif action == "sell" and in_pos:
                pending = "sell"

    # liquidation finale a la derniere cloture
    if in_pos:
        price = closes[-1]
        cash = units * price * (1 - fee_side)
        pnls.append((price / entry_price - 1) * 100)
        trades.append({"side": "sell", "idx": len(closes) - 1, "price": price})
        curve[-1] = cash
        units = 0.0
        in_pos = False

    final = curve[-1] if curve else INITIAL
    return {
        "equity": curve,
        "flags": flags,
        "final": final,
        "total_return_pct": round((final / INITIAL - 1) * 100, 2),
        "pnls_pct": pnls,
        "trades": trades,
        "n_trades": len(pnls),
        "n_wins": sum(1 for p in pnls if p > 0),
        "n_buys": n_buys,
    }


def full_metrics(sim: dict, closes: list[float], ts: list[int], warmup: int,
                 periods_per_year: float) -> dict:
    """Ensemble de metriques a partir d'une simulation. Le Sharpe/Sortino sont
    calcules sur l'equite POST-WARMUP (les zeros d'amorcage ne diluent pas)."""
    equity = sim["equity"]
    post = equity[warmup:] if len(equity) > warmup else equity
    # rendements pas-a-pas post-warmup
    rets = [(post[i] - post[i - 1]) / post[i - 1] for i in range(1, len(post)) if post[i - 1] > 0]

    mdd = max_drawdown_pct(post)
    tuw_pct, tuw_days = time_underwater(post)
    span_days = (ts[-1] - ts[warmup]) / 86400.0 if len(ts) > warmup else 0.0
    cg = cagr_pct(INITIAL, sim["final"], span_days)
    wr = round(sim["n_wins"] / sim["n_trades"] * 100, 1) if sim["n_trades"] else 0.0
    wlo, whi = wilson_ci(sim["n_wins"], sim["n_trades"])
    flags_post = sim["flags"][warmup:] if len(sim["flags"]) > warmup else sim["flags"]

    return {
        "total_return_pct": sim["total_return_pct"],
        "cagr_pct": cg,
        "sharpe": compute_sharpe(post, periods_per_year),
        "sortino": sortino(rets, periods_per_year),
        "max_drawdown_pct": mdd,
        "calmar": calmar(cg, mdd),
        "profit_factor": compute_profit_factor(sim["pnls_pct"]),
        "exposure_pct": compute_exposure(sum(1 for f in flags_post if f), len(flags_post)),
        "win_rate_pct": wr,
        "win_rate_ci95": [wlo, whi],
        "n_trades": sim["n_trades"],
        "time_underwater_pct": tuw_pct,
        "max_underwater_days": tuw_days,
        "n_returns": len(rets),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark buy & hold EQUITABLE
# ─────────────────────────────────────────────────────────────────────────────

def buy_hold(closes: list[float], warmup: int, fee_rt: float,
             periods_per_year: float) -> dict:
    """Achat a closes[warmup] (la ou la strat devient tradable), 1 aller-retour net
    de frais, meme capital. Comparaison honnete (memes periode et frais)."""
    fee_side = fee_rt / 2.0
    seg = closes[warmup:]
    if len(seg) < 2:
        return {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0}
    entry = seg[0]
    curve = [INITIAL * (c / entry) * (1 - fee_side) for c in seg]
    curve[-1] = curve[-1] * (1 - fee_side)   # frais de sortie
    return {
        "total_return_pct": round((curve[-1] / INITIAL - 1) * 100, 2),
        "sharpe": compute_sharpe(curve, periods_per_year),
        "max_drawdown_pct": max_drawdown_pct(curve),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward : stabilite temporelle de l'edge
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward(closes: list[float], k: int, sma_period: int, fee_rt: float,
                 entry_buf: float, exit_buf: float) -> dict:
    """Stabilite temporelle a SMA CONTINUE : on simule UNE fois (la SMA ne se
    reinitialise jamais, les positions traversent les frontieres), puis on decoupe
    la courbe d'equite POST-WARMUP en K fenetres calendaires. Le rendement d'une
    fenetre = variation de l'equite pendant cette periode ; les K fenetres
    compoundent EXACTEMENT au rendement total (pas d'artefact de frontiere).
    Robuste = positif sur la majorite ET aucune fenetre ne porte >60 % du gain."""
    sim = simulate(closes, sma_period, fee_rt, entry_buf, exit_buf)
    equity = sim["equity"]
    usable = equity[sma_period:] if len(equity) > sma_period else equity
    if len(usable) < k * 2:
        return {"k": k, "windows": [], "n_positive": 0, "n_windows": 0,
                "max_window_share": 0.0, "full_return_pct": sim["total_return_pct"],
                "verdict": "INDETERMINE"}

    block = len(usable) // k
    windows = []
    for j in range(k):
        lo = j * block
        hi = len(usable) if j == k - 1 else (j + 1) * block
        seg = usable[lo:hi]
        if len(seg) < 2 or seg[0] <= 0:
            continue
        ret = (seg[-1] / seg[0] - 1) * 100
        windows.append({"ret_pct": round(ret, 2), "n_days": len(seg)})

    rets = [w["ret_pct"] for w in windows]
    npos = sum(1 for r in rets if r > 0)
    # Concentration : part de la meilleure fenetre dans le gain compose (log-rendement).
    pos_logs = [math.log(1 + r / 100) for r in rets if r > 0]
    total_pos = sum(pos_logs)
    max_share = round(max(pos_logs) / total_pos, 3) if total_pos > 0 else 0.0

    full = sim["total_return_pct"]
    need = (len(rets) + 1) // 2  # majorite
    if not windows:
        verdict = "INDETERMINE"
    elif npos >= need and full > 0 and max_share < MAX_WINDOW_SHARE:
        verdict = "ROBUSTE"
    elif npos <= 1 or max_share >= MAX_WINDOW_SHARE:
        verdict = "FRAGILE"
    else:
        verdict = "MITIGE"

    return {
        "k": k, "windows": windows, "n_positive": npos, "n_windows": len(rets),
        "max_window_share": max_share, "full_return_pct": full, "verdict": verdict,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Gate de publication GO / NO-GO
# ─────────────────────────────────────────────────────────────────────────────

def publication_gate(sym: dict) -> dict:
    """Verdict GO/NO-GO + raisons. "EDGE PROUVE" seulement si TOUT passe ; sinon le
    rapport descend et DIT pourquoi."""
    reasons: list[str] = []
    ref = sym["reference"]
    wf = sym["walk_forward"]
    bh = sym["buy_hold"]

    insufficient = False
    if sym["n_candles"] < MIN_CANDLES:
        reasons.append(f"historique insuffisant ({sym['n_candles']} candles < {MIN_CANDLES})")
        insufficient = True
    if sym["gaps"]:
        reasons.append(f"{len(sym['gaps'])} trou(s) de donnees detecte(s) (ex. suspension d'echange)")
        insufficient = True
    if ref["n_trades"] < MIN_TRADES:
        reasons.append(f"trop peu de trades ({ref['n_trades']} < {MIN_TRADES})")
        insufficient = True

    beats_bh = ref["total_return_pct"] > bh["total_return_pct"]
    if not beats_bh:
        reasons.append(f"ne bat pas le buy&hold net ({ref['total_return_pct']}% <= {bh['total_return_pct']}%)")
    if wf["verdict"] == "FRAGILE":
        reasons.append("edge non stable en walk-forward (fragile / porte par 1 fenetre)")
    elif wf["verdict"] == "MITIGE":
        reasons.append("edge mitige en walk-forward")

    # L'edge survit-il aux frais reels ? (scenario realiste vs 0-frais)
    fee_map = {round(f["fee_rt"], 4): f for f in sym["fee_sensitivity"]}
    real = fee_map.get(0.002)
    if real is not None and real["total_return_pct"] <= bh["total_return_pct"]:
        reasons.append("l'edge disparait aux frais realistes (0.2 %)")

    if insufficient:
        verdict = "INSUFFISANT"
    elif not beats_bh or wf["verdict"] == "FRAGILE":
        verdict = "NON PROBANT"
    elif reasons:
        verdict = "MITIGE"
    else:
        verdict = "EDGE PROUVE"
    return {"verdict": verdict, "reasons": reasons}


# ─────────────────────────────────────────────────────────────────────────────
# Fetch (reseau) + detection de trous
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_daily(symbol_usd: str, days: int) -> list[tuple[int, float]]:
    """(timestamp, close) daily via Coinbase Exchange, pagine 300x86400, ancien->recent.
    Retry sur 429/erreur transitoire (ne renvoie PAS silencieusement un historique tronque)."""
    end = int(time.time())
    start_all = end - days * 86400
    url = f"https://api.exchange.coinbase.com/products/{symbol_usd}/candles"
    out: list[list] = []
    cur_end = end
    async with aiohttp.ClientSession(headers={"User-Agent": "kairos-edge-report"}) as s:
        while cur_end > start_all:
            cur_start = max(start_all, cur_end - 300 * 86400)
            params = {"granularity": GRANULARITY, "start": cur_start, "end": cur_end}
            data = None
            for attempt in range(4):
                try:
                    async with s.get(url, params=params,
                                     timeout=aiohttp.ClientTimeout(total=25)) as r:
                        if r.status == 200:
                            data = await r.json()
                            break
                        if r.status in (429, 502, 503, 504):
                            await asyncio.sleep(0.5 * (2 ** attempt))
                            continue
                        # 4xx definitif (produit inexistant) -> stop propre
                        data = []
                        break
                except Exception:
                    await asyncio.sleep(0.5 * (2 ** attempt))
            if not data:
                break
            out.extend(data)
            cur_end = cur_start
            await asyncio.sleep(0.3)
    out.sort(key=lambda c: c[0])
    return [(int(c[0]), float(c[4])) for c in out]   # close = index 4


def detect_gaps(ts: list[int]) -> list[dict]:
    """Trous > 1.5 jour entre 2 candles daily (suspension, delisting...)."""
    gaps = []
    for i in range(1, len(ts)):
        d = ts[i] - ts[i - 1]
        if d > GRANULARITY * 1.5:
            gaps.append({
                "from": datetime.fromtimestamp(ts[i - 1], timezone.utc).date().isoformat(),
                "to": datetime.fromtimestamp(ts[i], timezone.utc).date().isoformat(),
                "days": round(d / 86400, 1),
            })
    return gaps


# ─────────────────────────────────────────────────────────────────────────────
# Analyse d'un symbole
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_symbol(symbol_live: str, days: int,
                         entry_buf: float = ENTRY_BUF, exit_buf: float = EXIT_BUF) -> dict:
    symbol_data = symbol_live.replace("USDC", "USD")   # historique daily = -USD
    candles = await fetch_daily(symbol_data, days)
    ts = [c[0] for c in candles]
    closes = [c[1] for c in candles]
    ppy = GRAN_TO_PPY[GRANULARITY]
    warmup = SMA_PERIOD

    base = {
        "symbol_live": symbol_live,
        "symbol_data": symbol_data,
        "n_candles": len(closes),
        "closes_sha256": sha256_floats(closes),
    }
    if len(closes) < warmup + 15:
        base.update({"error": "historique insuffisant pour amorcer la SMA",
                     "span_days": 0, "gaps": [], "fee_sensitivity": [],
                     "reference": {"n_trades": 0, "total_return_pct": 0.0},
                     "buy_hold": {"total_return_pct": 0.0},
                     "walk_forward": {"verdict": "INDETERMINE", "windows": []},
                     "gate": {"verdict": "INSUFFISANT", "reasons": ["historique quasi vide"]}})
        return base

    span_days = (ts[-1] - ts[0]) / 86400.0
    gaps = detect_gaps(ts)

    # Sensibilite aux frais : la strat rejouee a chaque niveau de frais.
    fee_sensitivity = []
    ref_metrics = None
    ref_bh = None
    for label, fee in FEE_SCENARIOS:
        sim = simulate(closes, SMA_PERIOD, fee, entry_buf, exit_buf)
        m = full_metrics(sim, closes, ts, warmup, ppy)
        bh = buy_hold(closes, warmup, fee, ppy)
        fee_sensitivity.append({
            "scenario": label, "fee_rt": fee,
            "total_return_pct": m["total_return_pct"], "sharpe": m["sharpe"],
            "n_trades": m["n_trades"], "edge_vs_bh_pct": round(m["total_return_pct"] - bh["total_return_pct"], 2),
        })
        if abs(fee - REFERENCE_FEE) < 1e-9:
            ref_metrics = m
            ref_bh = bh

    if ref_metrics is None:   # securite
        sim = simulate(closes, SMA_PERIOD, REFERENCE_FEE, entry_buf, exit_buf)
        ref_metrics = full_metrics(sim, closes, ts, warmup, ppy)
        ref_bh = buy_hold(closes, warmup, REFERENCE_FEE, ppy)

    wf = walk_forward(closes, K_WINDOWS, SMA_PERIOD, REFERENCE_FEE, entry_buf, exit_buf)

    base.update({
        "span_days": round(span_days, 1),
        "first_date": datetime.fromtimestamp(ts[0], timezone.utc).date().isoformat(),
        "last_date": datetime.fromtimestamp(ts[-1], timezone.utc).date().isoformat(),
        "gaps": gaps,
        "fee_sensitivity": fee_sensitivity,
        "reference": ref_metrics,
        "buy_hold": ref_bh,
        "walk_forward": wf,
    })
    base["gate"] = publication_gate(base)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Rapport global
# ─────────────────────────────────────────────────────────────────────────────

def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


async def build_report(symbols: list[str], days: int,
                       entry_buf: float = ENTRY_BUF, exit_buf: float = EXIT_BUF) -> dict:
    results = []
    for sym in symbols:
        results.append(await analyze_symbol(sym, days, entry_buf, exit_buf))
        await asyncio.sleep(0.35)   # anti-429 entre symboles

    params = {
        "sma_period": SMA_PERIOD, "entry_buffer_pct": entry_buf, "exit_buffer_pct": exit_buf,
        "granularity": GRANULARITY, "warmup": SMA_PERIOD, "initial_usdc": INITIAL,
        "reference_fee_rt": REFERENCE_FEE, "fee_scenarios": [f for _, f in FEE_SCENARIOS],
        "k_windows": K_WINDOWS, "execution": "next_bar", "risk_free_annual": 0.0,
        "days_requested": days,
    }
    provenance = {
        "data_source": "api.exchange.coinbase.com/products/{sym}/candles",
        "granularity": GRANULARITY,
        "code_version": _git_head(),
        "truststore_active": _TRUSTSTORE,
        "strategy": "trend-following SMA daily, long-only, all-in, flip a la SMA",
        "note_risk_free": "rf=0 : le cash oisif n'est pas remunere ; un rf reel (>0) REDUIRAIT le Sharpe. Chiffre optimiste de ce montant.",
        "note_execution": "signal a la cloture i, execution a la cloture i+1 (anti look-ahead).",
        "note_fleet": "symboles = fleet LIVE reel (per Brice), pas config/bots.json du worktree (perime).",
    }

    # Empreinte : sur donnees + params + resultats (SANS generated_at, pour rester stable).
    fingerprint_payload = {
        "params": params,
        "closes_sha": {r["symbol_live"]: r.get("closes_sha256") for r in results},
        "results": [{k: v for k, v in r.items() if k != "closes_sha256"} for r in results],
    }
    canon = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, default=str)
    fingerprint = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance,
        "params": params,
        "symbols": results,
        "fingerprint": fingerprint,
        "verify": "Refetch les memes candles daily (-USD) sur [first_date,last_date], "
                  "rejoue avec `params`, recompute le sha256 du payload -> doit matcher.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rendu texte
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v, suffix="", none="—"):
    return none if v is None else f"{v}{suffix}"


def render_text(report: dict) -> str:
    L = []
    L.append("=" * 78)
    L.append("  KAIROS — CARTE D'IDENTITE DE L'EDGE  (rapport de preuve reproductible)")
    L.append("=" * 78)
    p = report["params"]
    L.append(f"  Strategie   : trend SMA{p['sma_period']} daily, long-only, all-in, flip strict")
    L.append(f"  Execution   : {p['execution']} (anti look-ahead)   |   Sharpe annualise x sqrt(365)")
    L.append(f"  Frais ref.  : {p['reference_fee_rt']*100:.1f}% round-trip (scenarios: "
             + ", ".join(f'{f*100:.1f}%' for f in p['fee_scenarios']) + ")")
    L.append(f"  Genere le   : {report['generated_at']}   |   code {report['provenance']['code_version'][:10]}")
    L.append(f"  Empreinte   : {report['fingerprint']}")
    L.append("")

    for s in report["symbols"]:
        L.append("-" * 78)
        head = f"  {s['symbol_live']}  (donnees {s['symbol_data']})"
        if "error" in s:
            L.append(head + f"  —  {s['error']}")
            L.append(f"     VERDICT : {s['gate']['verdict']}")
            continue
        L.append(head + f"  —  {s['n_candles']} jours  [{s['first_date']} → {s['last_date']}]")
        if s["gaps"]:
            L.append(f"     ⚠ TROUS : " + "; ".join(f"{g['from']}→{g['to']} ({g['days']}j)" for g in s["gaps"][:3])
                     + (" ..." if len(s["gaps"]) > 3 else ""))
        r = s["reference"]
        L.append(f"     Rendement {r['total_return_pct']:>+8.1f}%   CAGR {r['cagr_pct']:>+6.1f}%   "
                 f"Sharpe {r['sharpe']:>5}   Sortino {r['sortino']:>5}   Calmar {_fmt(r['calmar'])}")
        L.append(f"     MaxDD {r['max_drawdown_pct']:>5.1f}%   sous l'eau {r['time_underwater_pct']:>4.0f}% "
                 f"(max {r['max_underwater_days']}j)   expo {r['exposure_pct']:>4.0f}%   "
                 f"PF {_fmt(r['profit_factor'])}")
        L.append(f"     Trades {r['n_trades']:>3}   win {r['win_rate_pct']:>4.0f}% "
                 f"(IC95 {r['win_rate_ci95'][0]:.0f}-{r['win_rate_ci95'][1]:.0f}%)")
        bh = s["buy_hold"]
        edge = round(r["total_return_pct"] - bh["total_return_pct"], 1)
        L.append(f"     vs BUY&HOLD net {bh['total_return_pct']:>+8.1f}% (MaxDD {bh['max_drawdown_pct']:.0f}%)  "
                 f"→ edge {edge:>+7.1f} pts")
        # sensibilite frais
        fs = "   ".join(f"{f['fee_rt']*100:.1f}%→{f['total_return_pct']:+.0f}%" for f in s["fee_sensitivity"])
        L.append(f"     Sensibilite frais : {fs}")
        # walk-forward
        wf = s["walk_forward"]
        wins = " ".join(f"{w['ret_pct']:+.0f}%" for w in wf["windows"])
        L.append(f"     Walk-forward [{wins}]  {wf['n_positive']}/{wf['n_windows']}+  "
                 f"concentration {wf['max_window_share']*100:.0f}%  → {wf['verdict']}")
        g = s["gate"]
        L.append(f"     ┏━ VERDICT : {g['verdict']}")
        for reason in g["reasons"]:
            L.append(f"     ┃   • {reason}")

    L.append("=" * 78)
    # Synthese
    proven = [s["symbol_live"] for s in report["symbols"] if s.get("gate", {}).get("verdict") == "EDGE PROUVE"]
    L.append(f"  SYNTHESE : {len(proven)}/{len(report['symbols'])} edge(s) prouve(s) "
             + (f"→ {', '.join(proven)}" if proven else "→ aucun ne passe le gate complet"))
    L.append("  (Un verdict < EDGE PROUVE n'est pas un echec : c'est le rapport qui refuse")
    L.append("   de gonfler un chiffre qu'il ne peut pas soutenir. C'est ca, ne pas mentir.)")
    L.append("=" * 78)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Carte d'identite de l'edge Kairos")
    ap.add_argument("--symbols", default=",".join(LIVE_FLEET),
                    help="liste -USDC separee par des virgules (defaut = fleet live)")
    ap.add_argument("--days", type=int, default=1825, help="profondeur d'historique (defaut 5 ans)")
    ap.add_argument("--exit-buffer", type=float, default=EXIT_BUF,
                    help="bande d'hysteresis de SORTIE en %% (defaut 0 = flip strict ; ~1 = calibre)")
    ap.add_argument("--entry-buffer", type=float, default=ENTRY_BUF,
                    help="bande d'hysteresis d'ENTREE en %% (defaut 0)")
    ap.add_argument("--out", default="", help="chemin du JSON de sortie (optionnel)")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    report = asyncio.run(build_report(symbols, args.days, args.entry_buffer, args.exit_buffer))

    print(render_text(report))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nJSON auditable ecrit : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
