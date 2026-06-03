"""
ParamTuner - Opus 4.7 analyse les 30 derniers jours et propose des ajustements.

CLAUDE.md : Opus 4.7 | effort=xhigh | conception alpha
Coût : ~$1 par run, déclenché 1×/semaine (dimanche 21h UTC) ou manuellement via /tune.

NE MODIFIE JAMAIS .env automatiquement.
Sauvegarde la proposition dans memory/tuning_proposals/<timestamp>.json
et envoie un récap Telegram avec instructions pour appliquer.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from dotenv import load_dotenv

from interfaces import notifier
from interfaces.claude_client import complete, ENABLED, MODEL_OPUS

load_dotenv()
log = structlog.get_logger()

DB_PATH       = os.getenv("DB_PATH", "memory/trading.db")
MODE          = os.getenv("COINBASE_MODE", "paper")
# En live, exclut les residus paper (~10000) du calcul P&L 30j (cf. dashboard).
PAPER_LIVE_SPLIT = float(os.getenv("PAPER_LIVE_SPLIT_USDC", "1000"))
PROPOSALS_DIR = Path(DB_PATH).parent / "tuning_proposals"
TUNE_DOW      = int(os.getenv("PARAM_TUNE_DOW_UTC",  "6"))   # Dimanche
TUNE_HOUR     = int(os.getenv("PARAM_TUNE_HOUR_UTC", "21"))


# Paramètres tunables avec leurs bornes de sécurité
TUNABLE = {
    "RISK_MAX_POSITION_PCT":   (0.005, 0.10, "Taille max par trade (% portfolio)"),
    "RISK_MIN_CONFIDENCE":     (0.55,  0.85, "Confiance min pour trader"),
    "STRATEGY_VOL_MIN_PCT":    (0.03,  0.20, "Volatilité min pour trader"),
    "ATR_SL_MULT":             (1.0,   3.0,  "Multiplicateur SL ATR"),
    "ATR_TP_MULT":             (1.5,   4.0,  "Multiplicateur TP ATR"),
    "ENSEMBLE_MIN_SCORE":      (0.8,   2.5,  "Score min ensemble pour trader"),
    "MR_RSI_BUY":              (25,    35,   "RSI seuil BUY mean reversion"),
    "MR_RSI_SELL":             (65,    75,   "RSI seuil SELL mean reversion"),
    "RISK_MAX_COMBINED_EXPOSURE_PCT": (0.03, 0.15, "Exposition combinée max"),
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seconds_until_next_weekday(dow: int, hour: int, minute: int = 0) -> float:
    now    = datetime.now(timezone.utc)
    days_ahead = (dow - now.weekday()) % 7
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) \
             + timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _collect_30d_data() -> dict:
    """Collecte les stats 30j pour donner du contexte a Opus."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with _db() as conn:
        sells = conn.execute(
            "SELECT symbol, timestamp, metadata FROM decisions "
            "WHERE task_type='order' AND role='orchestrator' AND action='sell' "
            "AND timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()

        buys = conn.execute(
            "SELECT symbol, timestamp, metadata FROM decisions "
            "WHERE task_type='order' AND role='orchestrator' AND action='buy' "
            "AND timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()

        rejected = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='order' AND action='rejected' AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        signals_held = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE task_type='signal' AND action='hold' AND timestamp >= ?",
            (since,),
        ).fetchone()[0]

        if MODE == "live":
            snapshots = conn.execute(
                "SELECT total_usdc, timestamp FROM portfolio_snapshots "
                "WHERE timestamp >= ? AND total_usdc < ? ORDER BY timestamp",
                (since, PAPER_LIVE_SPLIT),
            ).fetchall()
        else:
            snapshots = conn.execute(
                "SELECT total_usdc, timestamp FROM portfolio_snapshots "
                "WHERE timestamp >= ? ORDER BY timestamp",
                (since,),
            ).fetchall()

    # Apparier BUY/SELL pour PnL par trade
    trades: list[dict] = []
    open_pos: dict[str, list[dict]] = {}
    for r in buys:
        try:
            m = json.loads(r["metadata"]) if r["metadata"] else {}
            open_pos.setdefault(r["symbol"], []).append({
                "price": float(m.get("price", 0)),
                "qty":   float(m.get("qty", 0)),
                "ts":    r["timestamp"],
            })
        except Exception:
            pass

    for r in sells:
        try:
            m = json.loads(r["metadata"]) if r["metadata"] else {}
            stk = open_pos.get(r["symbol"], [])
            if not stk:
                continue
            b = stk.pop(0)
            if b["price"] > 0:
                sell_price = float(m.get("price", 0))
                pnl_pct = (sell_price - b["price"]) / b["price"] * 100
                trades.append({
                    "symbol":  r["symbol"],
                    "buy":     round(b["price"], 2),
                    "sell":    round(sell_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                })
        except Exception:
            pass

    n_trades = len(trades)
    n_wins   = sum(1 for t in trades if t["pnl_pct"] > 0)
    by_symbol: dict[str, list[float]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t["pnl_pct"])

    values = [s["total_usdc"] for s in snapshots]
    pnl_30d = ((values[-1] - values[0]) / values[0] * 100) if len(values) >= 2 and values[0] > 0 else 0

    return {
        "n_trades":       n_trades,
        "n_wins":         n_wins,
        "win_rate":       round(n_wins / n_trades * 100, 1) if n_trades else None,
        "avg_pnl":        round(sum(t["pnl_pct"] for t in trades) / n_trades, 2) if n_trades else None,
        "best":           max((t["pnl_pct"] for t in trades), default=0),
        "worst":          min((t["pnl_pct"] for t in trades), default=0),
        "n_rejected":     rejected,
        "n_holds":        signals_held,
        "pnl_30d_pct":    round(pnl_30d, 2),
        "by_symbol":      {s: {"n": len(p), "avg": round(sum(p)/len(p), 2)}
                           for s, p in by_symbol.items()},
        "n_snapshots":    len(snapshots),
    }


def _current_params() -> dict:
    """Lit la valeur actuelle des params tunables depuis .env."""
    return {k: float(os.getenv(k, "")) if os.getenv(k) else None for k in TUNABLE.keys()}


async def generate_proposal() -> dict | None:
    """
    Lance l'analyse Opus + genere une proposition.
    Retourne dict {analysis, proposals, rationale} ou None si erreur.
    """
    if not ENABLED:
        log.warning("param_tuner_skip", reason="ANTHROPIC_API_KEY absent")
        return None

    data = _collect_30d_data()
    if data["n_trades"] < 5:
        log.info("param_tuner_skip", reason="trop peu de trades", n=data["n_trades"])
        return None

    current = _current_params()
    bounds_txt = "\n".join(
        f"  - {k}: actuel={current.get(k)}, plage=[{lo}, {hi}], {desc}"
        for k, (lo, hi, desc) in TUNABLE.items()
    )

    by_sym_txt = "\n".join(
        f"  - {s}: {v['n']} trades, avg {v['avg']:+.2f}%"
        for s, v in data["by_symbol"].items()
    )

    prompt = f"""Tu es un quant trader senior. Analyse 30 jours de performance d'un bot crypto
multi-strategies (EMA crossover + MACD + mean reversion + ensemble voting) et propose
des ajustements de paramètres.

═══ PERFORMANCE 30J ═══
- Trades fermes : {data['n_trades']}
- Win rate      : {data['win_rate']}%
- P&L moyen     : {data['avg_pnl']}% par trade
- Best / Worst  : {data['best']:+.2f}% / {data['worst']:+.2f}%
- Rejected      : {data['n_rejected']} ordres rejetes par risk agent
- Holds         : {data['n_holds']} signaux HOLD
- P&L total 30j : {data['pnl_30d_pct']:+.2f}%

Par symbole :
{by_sym_txt or '  (aucune donnee par symbole)'}

═══ PARAMETRES TUNABLES ═══
{bounds_txt}

═══ TON TRAVAIL ═══
1. Diagnostique en 2-3 phrases ce qui fonctionne / ne fonctionne pas
2. Propose des ajustements pour 3-5 parametres maximum (pas tous)
3. Pour chaque ajustement : nouvelle valeur (dans la plage), variation %, justification courte
4. Conserve une logique conservatrice — pas de changements > 30% par run

Reponds UNIQUEMENT en JSON valide :
{{
  "diagnosis": "...",
  "proposals": [
    {{"key": "PARAM_NAME", "current": X, "proposed": Y, "change_pct": +Z, "reason": "..."}}
  ],
  "expected_impact": "..."
}}"""

    log.info("param_tuner_calling_opus",
             n_trades=data["n_trades"], pnl=data["pnl_30d_pct"])

    response = await complete(prompt, model=MODEL_OPUS, max_tokens=1200, timeout_s=60, use_cache=False)
    if not response:
        log.warning("param_tuner_no_response")
        return None

    # Parse JSON (Opus peut envelopper dans ```json...```)
    raw = response.strip()
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.split("\n")
                        if not line.strip().startswith("```"))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("param_tuner_parse_error", error=str(exc), raw=response[:200])
        return None

    # Valide les bornes
    valid_proposals = []
    for p in parsed.get("proposals", []):
        key = p.get("key", "")
        if key not in TUNABLE:
            continue
        lo, hi, _ = TUNABLE[key]
        proposed = p.get("proposed")
        if proposed is None or not (lo <= float(proposed) <= hi):
            log.warning("param_tuner_out_of_bounds",
                        key=key, proposed=proposed, bounds=(lo, hi))
            continue
        valid_proposals.append(p)

    result = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "data_30d":   data,
        "diagnosis":  parsed.get("diagnosis", ""),
        "proposals":  valid_proposals,
        "expected_impact": parsed.get("expected_impact", ""),
        "model":      MODEL_OPUS,
    }

    # Sauvegarde
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    ts_file = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = PROPOSALS_DIR / f"proposal_{ts_file}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("param_tuner_saved", path=str(path), n_proposals=len(valid_proposals))

    return result


def format_proposal_telegram(proposal: dict) -> str:
    """Formate la proposition pour Telegram (Markdown)."""
    lines = [
        f"🧠 *ANALYSE OPUS — Tuning paramètres*",
        f"",
        f"_{proposal['diagnosis']}_",
        f"",
        f"*Propositions :*",
    ]
    for p in proposal["proposals"]:
        arrow = "↗️" if p.get("change_pct", 0) > 0 else "↘️"
        lines.append(
            f"  {arrow} `{p['key']}` : "
            f"`{p['current']}` → `{p['proposed']}` "
            f"({p.get('change_pct', 0):+.0f}%)"
        )
        lines.append(f"     _{p.get('reason', '')[:120]}_")

    lines += [
        f"",
        f"*Impact attendu :*",
        f"_{proposal.get('expected_impact', '')[:300]}_",
        f"",
        f"💡 *Pour appliquer*, édite `.env` manuellement avec ces valeurs.",
        f"_Aucune modification automatique. Décision = ton ressort._",
    ]
    return "\n".join(lines)


async def run_tuning_once() -> bool:
    """Lance une session de tuning. Retourne True si proposition envoyee."""
    proposal = await generate_proposal()
    if not proposal:
        return False
    if not proposal["proposals"]:
        log.info("param_tuner_no_proposals")
        return False
    msg = format_proposal_telegram(proposal)
    await notifier.notify(msg)
    return True


async def param_tuner_loop() -> None:
    """Boucle infinie : tuning auto le dimanche 21h UTC."""
    log.info("param_tuner_started", dow=TUNE_DOW, hour=TUNE_HOUR)

    while True:
        try:
            wait_s = _seconds_until_next_weekday(TUNE_DOW, TUNE_HOUR)
            log.info("param_tuner_sleeping",
                     seconds=int(wait_s),
                     next_at=f"jour={TUNE_DOW} {TUNE_HOUR:02d}:00 UTC")
            await asyncio.sleep(wait_s)

            sent = await run_tuning_once()
            log.info("param_tuner_run_done", sent=sent)
            await asyncio.sleep(70)
        except asyncio.CancelledError:
            log.info("param_tuner_stopped")
            raise
        except Exception as exc:
            log.error("param_tuner_error", error=str(exc))
            await asyncio.sleep(600)
