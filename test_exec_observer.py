"""
test_exec_observer.py — la boîte noire d'exécution (Axe 2).

Vérifie : compteurs, résumé /api/health, journal JSONL persistant, et surtout
le CONTRAT DE SÛRETÉ (aucune entrée ne doit jamais lever d'exception).

Lancer : python test_exec_observer.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Journal redirigé vers un fichier temporaire : aucun effet sur logs/ réel.
_TMP = tempfile.mkdtemp(prefix="kairos_obs_test_")
os.environ["EXEC_JOURNAL_PATH"] = os.path.join(_TMP, "exec_journal.jsonl")
os.environ["COINBASE_MODE"] = "paper"

import exec_observer as obs  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def test_counters_and_last() -> None:
    print("\n[1] compteurs + dernier évènement par type")
    obs.reset()
    obs.record_fill("BTC-USDC", "buy", 0.001, 60000.0, estimated=False, order_id="o1")
    obs.record_fill("ETH-USDC", "buy", 0.02, 2500.0, estimated=True, order_id="o2")
    snap = obs.snapshot()
    check("2 fills comptés", snap["counters"].get("fill") == 2)
    check("1 fill estimé sous-compté", snap["fills_estimated"] == 1)
    check("last_fill = le plus récent (ETH)", snap["last_fill"]["symbol"] == "ETH-USDC")
    check("last_fill porte le flag estimated", snap["last_fill"]["estimated"] is True)


def test_divergence_and_purge() -> None:
    print("\n[2] divergence broker + purge fantôme")
    obs.reset()
    obs.record_divergence("SOL-USDC", local_qty=1.0, real_qty=0.7)
    obs.record_phantom_purge("DOGE-USDC")
    snap = obs.snapshot()
    check("divergence comptée", snap["divergences"] == 1)
    check("delta = real - local (-0.3)", abs(snap["last_divergence"]["delta"] + 0.3) < 1e-9)
    check("purge fantôme enregistrée", snap["counters"].get("phantom_purge") == 1)


def test_cycle_heartbeat() -> None:
    print("\n[3] battement de cœur (âge du dernier cycle)")
    obs.reset()
    check("aucun cycle -> âge None", obs.snapshot()["cycle_age_s"] is None)
    obs.mark_cycle("test")
    snap = obs.snapshot()
    check("cycle horodaté", snap["last_cycle_ts"] is not None)
    check("âge du cycle >= 0", snap["cycle_age_s"] is not None and snap["cycle_age_s"] >= 0)


def test_jsonl_persisted() -> None:
    print("\n[4] journal JSONL persistant et valide")
    obs.reset()
    obs.record_retry("429 Too Many Requests", attempt=1)
    path = os.environ["EXEC_JOURNAL_PATH"]
    check("fichier journal créé", os.path.exists(path))
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    check("au moins une ligne écrite", len(lines) >= 1)
    last = json.loads(lines[-1])   # doit être un JSON valide
    check("dernière ligne = rest_retry", last.get("kind") == "rest_retry")
    check("ligne horodatée (ts)", "ts" in last)


def test_safety_never_raises() -> None:
    print("\n[5] contrat de sûreté : aucune entrée ne lève")
    obs.reset()
    raised = False
    try:
        # Types volontairement incohérents : le chemin critique ne doit jamais casser.
        obs.record_fill(None, None, "pas_un_float", None, estimated="oui", order_id=None)  # type: ignore
        obs.record_divergence(None, "x", object())  # type: ignore
        obs.record_retry(12345)  # type: ignore
        obs.mark_cycle(object())  # type: ignore
        obs.snapshot()
        obs.recent(10)
    except Exception as exc:
        raised = True
        print(f"       a levé : {exc!r}")
    check("aucune exception propagée", raised is False)


def test_recent_bounded() -> None:
    print("\n[6] ring buffer borné")
    obs.reset()
    for i in range(20):
        obs.record_retry(f"e{i}")
    rec = obs.recent(5)
    check("recent(5) renvoie 5 évènements", len(rec) == 5)
    check("recent garde les plus récents", rec[-1]["reason"] == "e19")


def main() -> int:
    print("=== test_exec_observer : boîte noire d'exécution ===")
    test_counters_and_last()
    test_divergence_and_purge()
    test_cycle_heartbeat()
    test_jsonl_persisted()
    test_safety_never_raises()
    test_recent_bounded()

    print("\n" + "=" * 50)
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"    - {f}")
        return 1
    print("  Tous les checks passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
