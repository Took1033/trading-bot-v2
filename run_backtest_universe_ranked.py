"""
run_backtest_universe_ranked.py — avait-on les BONS alts ? (le gate rigoureux, pas le filtre faible)

L'ancien filtre de deploiement (`run_backtest_universe.py`) gardait un actif sur
"rendement compose > 0", in-sample, sans walk-forward, sur univers de survivants.
La flotte actuelle est le fossile de ce filtre faible. Ce script applique le GATE
HONNETE d'edge_report (walk-forward, sensibilite aux frais, benchmark equitable,
seuils de donnees) a un univers LARGE, et classe par robustesse.

Question : les 4 alts actuels (ETH/SOL/XRP/DOGE) sont-ils meme les plus defendables,
ou d'autres alts ont-ils un edge trend plus robuste ?

CAVEAT HONNETE (survivorship) : cet univers ne contient que des coins VIVANTS
aujourd'hui. Le cimetiere (LUNA, etc.) est absent -> le vrai risque du trend long-only
sur alt (etre long dans la mort) est SOUS-estime. Ce classement dit "parmi les alts
tradables aujourd'hui, lesquels ont un edge trend robuste in-sample", pas "lesquels
seront robustes demain".

NB : lancer sur la machine de Brice (truststore). Aucune donnee live.
Usage : python run_backtest_universe_ranked.py
"""
from __future__ import annotations

import asyncio
import sys

import edge_report as er

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Univers : les 5 du fleet + 11 alts etablis (historique decent sur Coinbase).
UNIVERSE = [
    "BTC-USDC", "ETH-USDC", "SOL-USDC", "XRP-USDC", "DOGE-USDC",
    "ADA-USDC", "AVAX-USDC", "LINK-USDC", "DOT-USDC", "LTC-USDC",
    "BCH-USDC", "ATOM-USDC", "XLM-USDC", "UNI-USDC", "AAVE-USDC", "NEAR-USDC",
]
FLEET = {"BTC-USDC", "ETH-USDC", "SOL-USDC", "XRP-USDC", "DOGE-USDC"}
VERDICT_RANK = {"EDGE PROUVE": 0, "MITIGE": 1, "NON PROBANT": 2, "INSUFFISANT": 3}


async def main() -> int:
    rows = []
    for sym in UNIVERSE:
        try:
            r = await er.analyze_symbol(sym, 1825)
        except Exception as exc:
            print(f"  (skip {sym} : {exc})")
            continue
        g = r.get("gate", {})
        ref = r.get("reference", {})
        bh = r.get("buy_hold", {})
        wf = r.get("walk_forward", {})
        fees = {round(f.get("fee_rt", -1), 4): f for f in r.get("fee_sensitivity", [])}
        surv = fees.get(0.015, {}).get("total_return_pct")
        rows.append({
            "sym": sym.replace("-USDC", ""),
            "in_fleet": sym in FLEET,
            "verdict": g.get("verdict", "?"),
            "ret": ref.get("total_return_pct"),
            "edge": (None if ref.get("total_return_pct") is None or bh.get("total_return_pct") is None
                     else round(ref["total_return_pct"] - bh["total_return_pct"], 1)),
            "wfv": wf.get("verdict", "?"),
            "npos": wf.get("n_positive", 0), "nwin": wf.get("n_windows", 0),
            "conc": round(wf.get("max_window_share", 0) * 100),
            "surv15": surv,
            "cand": r.get("n_candles", 0),
        })
        await asyncio.sleep(0.35)

    rows.sort(key=lambda x: (VERDICT_RANK.get(x["verdict"], 9), -x["npos"],
                             -(x["edge"] if x["edge"] is not None else -1e9)))

    print("=" * 92)
    print("  UNIVERS CLASSE PAR ROBUSTESSE — gate honnete (walk-forward + frais + benchmark), 5 ans")
    print("  survivorship : coins vivants seulement -> queue gauche (delisting) SOUS-estimee")
    print("=" * 92)
    print(f"  {'#':>2} {'ACTIF':<7}{'fleet':>6}  {'VERDICT':<13}{'rendt':>8}{'vsHold':>8}"
          f"  {'walk-fwd':<9}{'conc':>5}{'@1.5%':>8}{'jours':>7}")
    print("-" * 92)
    for i, x in enumerate(rows, 1):
        fleet = " ●" if x["in_fleet"] else ""
        ret = f"{x['ret']:>+7.0f}%" if x["ret"] is not None else "     — "
        edge = f"{x['edge']:>+7.0f}" if x["edge"] is not None else "     — "
        s15 = f"{x['surv15']:>+7.0f}%" if x["surv15"] is not None else "     — "
        print(f"  {i:>2} {x['sym']:<7}{fleet:>6}  {x['verdict']:<13}{ret}{edge}"
              f"  {x['wfv']:<9}{x['conc']:>4}%{s15}{x['cand']:>7}")
    print("=" * 92)
    proven = [x["sym"] for x in rows if x["verdict"] == "EDGE PROUVE"]
    fleet_ranks = [i for i, x in enumerate(rows, 1) if x["in_fleet"]]
    print(f"  Edge prouve : {', '.join(proven) if proven else 'aucun'} "
          f"| rangs du fleet actuel : {fleet_ranks}")
    print("  Lecture : les 4 alts du fleet sont-ils en haut du classement, ou d'autres alts")
    print("  (hors fleet) ont-ils un edge trend plus robuste ? Le fleet etait-il le bon choix ?")
    print("  (aide a la decision — survivorship non corrige ; la decision reste a Brice)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
