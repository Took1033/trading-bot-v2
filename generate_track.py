"""
generate_track.py — regenere track_edge.html depuis les donnees FRAICHES (page /track auto).

La carte servie a /track etait un snapshot fige. Ce generateur la rejoue en une commande :
rapport d'edge complet + serie heros BTC + provenance/empreinte, injectes dans
track_edge.template.html. La page reste rapide (statique) mais auto-fraiche (planifiable).

Config par DEFAUT = flip strict (exit 0) — le choix HONNETE (la bande d'hysteresis ne
survit pas au walk-forward OOS ; cf. run_backtest_band_walkforward.py). --exit-buffer
pour regenerer une autre config.

NB : lancer sur la machine de Brice (truststore -> SSL Coinbase). Aucune donnee live.
Usage : python generate_track.py            # flip strict, honnete
        python generate_track.py --exit-buffer 1
Planif (hebdo, exemple) : Register-ScheduledTask ... pythonw generate_track.py
"""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import edge_report as er

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TEMPLATE = "track_edge.template.html"
OUT = "track_edge.html"
_MONTHS_FR = {"Jan": "janv.", "Feb": "févr.", "Mar": "mars", "Apr": "avr.", "May": "mai",
              "Jun": "juin", "Jul": "juil.", "Aug": "août", "Sep": "sept.", "Oct": "oct.",
              "Nov": "nov.", "Dec": "déc."}
_VERDICT_DISP = {"EDGE PROUVE": "EDGE PROUVÉ", "MITIGE": "MITIGÉ",
                 "NON PROBANT": "NON PROBANT", "INSUFFISANT": "INSUFFISANT"}


def _fr_month(label: str) -> str:
    parts = label.split()
    return f"{_MONTHS_FR.get(parts[0], parts[0])} {parts[1]}" if len(parts) == 2 else label


def _window_dates(sym: dict) -> list[tuple[str, str, float]]:
    first = datetime.strptime(sym["first_date"], "%Y-%m-%d")
    acc = er.SMA_PERIOD
    out = []
    for w in sym["walk_forward"]["windows"]:
        a = first.fromordinal(first.toordinal() + acc)
        acc += w["n_days"]
        b = first.fromordinal(first.toordinal() + acc)
        out.append((a.strftime("%b %Y"), b.strftime("%b %Y"), w["ret_pct"]))
    return out


def _a_entry(s: dict) -> dict:
    r, bh, wf, g = s["reference"], s["buy_hold"], s["walk_forward"], s["gate"]
    proven = g["verdict"] == "EDGE PROUVE"
    cls = "ok" if proven else ("warn" if g["verdict"] == "MITIGE" else "no")
    wd = _window_dates(s)
    dom = max(wd, key=lambda x: x[2]) if wd else ("", "", 0)
    e = {
        "t": s["symbol_live"].replace("-USDC", ""), "d": s["symbol_live"],
        "v": _VERDICT_DISP.get(g["verdict"], g["verdict"]), "cls": cls,
        "ret": round(r["total_return_pct"], 1), "cagr": round(r["cagr_pct"], 1),
        "sharpe": r["sharpe"], "sortino": r["sortino"], "maxdd": round(r["max_drawdown_pct"], 1),
        "calmar": r["calmar"], "pf": r["profit_factor"], "expo": round(r["exposure_pct"]),
        "trades": r["n_trades"], "win": round(r["win_rate_pct"]),
        "wlo": round(r["win_rate_ci95"][0]), "whi": round(r["win_rate_ci95"][1]),
        "bh": round(bh["total_return_pct"], 1),
        "edge": round(r["total_return_pct"] - bh["total_return_pct"], 1),
        "wf": [round(w["ret_pct"]) for w in wf["windows"]],
        "conc": round(wf["max_window_share"] * 100), "wfv": wf["verdict"],
        "fees": [round(f["total_return_pct"], 1) for f in s["fee_sensitivity"]],
        "proven": proven,
        "dom": f"{_fr_month(dom[0])} → {_fr_month(dom[1])}", "domr": round(dom[2]),
    }
    if s["n_candles"] < 1500:
        e["short"] = True
    return e


async def _hero_series(exit_buf: float) -> dict:
    candles = await er.fetch_daily("BTC-USD", 1825)
    ts = [c[0] for c in candles]; closes = [c[1] for c in candles]; P = er.SMA_PERIOD
    sma = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= P:
            sma[i] = sum(closes[i + 1 - P:i + 1]) / P
    sim = er.simulate(closes, P, er.REFERENCE_FEE, 0.0, exit_buf)
    eq, fl = sim["equity"], sim["flags"]
    bh = [None] * len(closes); entry = closes[P]
    for i in range(P, len(closes)):
        bh[i] = er.INITIAL * (closes[i] / entry) * (1 - er.REFERENCE_FEE / 2)
    step = 7
    idx = list(range(P, len(closes), step))
    if idx[-1] != len(closes) - 1:
        idx.append(len(closes) - 1)
    return {
        "dates": [datetime.fromtimestamp(ts[i], timezone.utc).strftime("%Y-%m") for i in idx],
        "price": [round(closes[i], 1) for i in idx],
        "sma": [round(sma[i], 1) if sma[i] else None for i in idx],
        "equity": [round(eq[i], 1) for i in idx],
        "bh": [round(bh[i], 1) if bh[i] else None for i in idx],
        "inpos": [1 if fl[i] else 0 for i in idx],
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Regenere track_edge.html")
    ap.add_argument("--exit-buffer", type=float, default=0.0)
    args = ap.parse_args()
    exit_buf = args.exit_buffer

    report = await er.build_report(er.LIVE_FLEET, 1825, 0.0, exit_buf)
    A = [_a_entry(s) for s in report["symbols"]]
    hero = await _hero_series(exit_buf)

    label = f"hystérésis {exit_buf:.0f}%" if exit_buf > 0 else "flip strict"
    cfg = f"hystérésis sortie {exit_buf:.0f} %" if exit_buf > 0 else "sortie flip strict (aucune bande)"
    meta = {
        "config": cfg, "exit_label": label,
        "generated": report["generated_at"][:10],
        "code": (report["provenance"]["code_version"] or "unknown")[:8],
        "fingerprint": report["fingerprint"],
    }

    tpl = open(TEMPLATE, encoding="utf-8").read()
    tpl = tpl.replace("__META__", json.dumps(meta, ensure_ascii=False))
    tpl = tpl.replace("__A__", json.dumps(A, ensure_ascii=False))
    tpl = tpl.replace("__HERO__", json.dumps(hero, ensure_ascii=False))
    for ph in ("__META__", "__A__", "__HERO__"):
        assert ph not in tpl, f"placeholder residuel {ph}"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(tpl)

    proven = [a["t"] for a in A if a["proven"]]
    print(f"{OUT} regenere · config '{label}' · {len(proven)}/{len(A)} prouve(s) : "
          f"{proven or 'aucun'} · genere {meta['generated']} · {meta['fingerprint'][:20]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
