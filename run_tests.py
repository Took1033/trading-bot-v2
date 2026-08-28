#!/usr/bin/env python
"""
run_tests.py — runner de tests unifié (source de vérité unique : hook + CI).

Kairos touche du CAPITAL RÉEL en mode live. Ce runner est le filet anti-régression
sur le chemin critique d'exécution. Il est appelé de façon identique par :
  - le hook git `.githooks/pre-push`  (filet local, avant chaque push)
  - GitHub Actions `.github/workflows/ci.yml`  (filet serveur)

Garde-fous de sécurité (défense en profondeur) :
  - `COINBASE_MODE=paper` est FORCÉ dans l'environnement de chaque test, même si
    le test oublie de le faire lui-même → aucun test automatisé ne peut trader réel.
  - Les canaux de notification sont neutralisés (aucun effet de bord réseau).
  - `test_live.py` est EXCLU par nom : c'est un smoke-test de connexion manuel qui
    force `COINBASE_MODE=live` et interroge la vraie API Coinbase + le solde réel.
    Il ne doit JAMAIS tourner en CI. Lancer manuellement : `python test_live.py`.

Chaque test est un script auto-exécutable : exit 0 = tout vert, exit 1 = ≥1 check KO.

Usage :
    python run_tests.py            # lance tous les tests hermétiques
    python run_tests.py -v         # affiche aussi la sortie complète des tests OK
Sortie : exit 0 si tout passe, 1 sinon (consommable par un hook / une CI).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Sorties accentuées lisibles partout : console Windows (cp1252) comme CI Ubuntu.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent

# Tests qui touchent le RÉSEAU RÉEL / le compte réel — jamais en automatisé.
EXCLUDE: dict[str, str] = {
    "test_live.py": "smoke-test de connexion live (vraie API Coinbase + solde réel)",
}

# Timeout par test (s) : un test qui pend ne doit pas bloquer un push/une CI.
PER_TEST_TIMEOUT = 180


def _safe_env() -> dict[str, str]:
    """Environnement durci : paper forcé, notifications muettes."""
    env = os.environ.copy()
    env["COINBASE_MODE"] = "paper"          # garde-fou capital réel
    env["TELEGRAM_BOT_TOKEN"] = ""          # pas de notif réseau
    env["DISCORD_WEBHOOK_URL"] = ""
    env["PYTHONIOENCODING"] = "utf-8"       # sorties accentuées sous Windows CI
    return env


def discover() -> list[Path]:
    tests = sorted(p for p in ROOT.glob("test_*.py") if p.name not in EXCLUDE)
    return tests


def run_one(path: Path, verbose: bool) -> tuple[str, str, float]:
    """Retourne (statut, sortie, durée). Statut ∈ {PASS, FAIL, ERR}."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, path.name],
            cwd=ROOT,
            env=_safe_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PER_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "ERR", f"TIMEOUT (> {PER_TEST_TIMEOUT}s)", time.monotonic() - start
    dur = time.monotonic() - start
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return "PASS", out, dur
    if proc.returncode == 1:
        return "FAIL", out, dur
    return "ERR", out, dur


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    tests = discover()

    if EXCLUDE:
        print("Exclus de l'automatisation (à lancer à la main) :")
        for name, why in EXCLUDE.items():
            print(f"  - {name} — {why}")
        print()

    if not tests:
        print("Aucun test hermétique découvert.")
        return 1

    print(f"=================== KAIROS · {len(tests)} tests ===================")
    results: list[tuple[str, str]] = []
    icon = {"PASS": "[ ok ]", "FAIL": "[FAIL]", "ERR": "[ERR ]"}
    for t in tests:
        status, out, dur = run_one(t, verbose)
        print(f"  {icon[status]} {t.name:<28} {dur:5.1f}s")
        results.append((status, t.name))
        if status != "PASS":
            tail = "\n".join(out.strip().splitlines()[-15:])
            print("\n".join("       " + ln for ln in tail.splitlines()))
        elif verbose:
            print("\n".join("       " + ln for ln in out.strip().splitlines()))

    n_pass = sum(1 for s, _ in results if s == "PASS")
    n_fail = sum(1 for s, _ in results if s == "FAIL")
    n_err = sum(1 for s, _ in results if s == "ERR")
    print("=================================================================")
    print(f"  BILAN : {n_pass} OK  /  {n_fail} FAIL  /  {n_err} ERR")

    ok = n_fail == 0 and n_err == 0
    print("  -> " + ("TOUT VERT" if ok else "ECHEC — push/deploiement a bloquer"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
