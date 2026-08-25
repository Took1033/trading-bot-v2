"""
test_push_prefs.py — tests hermetiques (sans reseau) des preferences de notif (P5).
Verifie la categorisation des notifs reelles + le round-trip get/set des preferences.

Lancer : python test_push_prefs.py   (exit 0 = tout passe)
"""
from __future__ import annotations

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="kairos_push_test_"), "trading.db")

import push_manager

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'KO'}] {label}")
    if not cond:
        _failures.append(label)


def test_categorize() -> None:
    print("\n[1] categorize — notifs reelles -> bonne categorie")
    cases = [
        ("🤖 Kairos Alpha — Swarm démarré",            "system"),
        ("🚨 KILL SWITCH ACTIVE",                       "system"),
        ("🔴 Kairos — preflight ECHEC (démarrage bloqué)", "system"),
        ("⚠️ Alerte drawdown 5%",                       "system"),
        ("💥 Kairos a crashé",                          "system"),
        ("🗓 Résumé quotidien",                          "reports"),
        ("Bilan hebdomadaire",                          "reports"),
        ("📈 TREND — Entrée BTC-USDC",                  "entries"),
        ("📉 TREND — Sortie ETH-USDC",                  "exits"),
        ("✅ Clôture manuelle BTC-USDC",                "exits"),
        ("🚀 BTC-USDC — position à +12%",               "gains"),
        ("🔔 Kairos — notification de test",            "system"),  # inconnu -> system
    ]
    for title, expected in cases:
        got = push_manager.categorize(title, "")
        check(f"{title[:34]!r} -> {expected}", got == expected)


def test_prefs_roundtrip() -> None:
    print("\n[2] get/set prefs")
    p = push_manager.get_prefs()
    check("defaut : tout ON", all(p.get(k) for k in ("entries", "exits", "gains", "reports")))

    push_manager.set_prefs({"reports": False, "gains": False})
    p = push_manager.get_prefs()
    check("reports desactive persiste", p["reports"] is False)
    check("gains desactive persiste", p["gains"] is False)
    check("entries reste ON", p["entries"] is True)

    push_manager.set_prefs({"reports": True})
    check("reports reactive", push_manager.get_prefs()["reports"] is True)

    # cle inconnue ignoree, 'system' non configurable (absent des defauts)
    push_manager.set_prefs({"zzz": False, "system": False})
    p = push_manager.get_prefs()
    check("cle inconnue ignoree", "zzz" not in p)
    check("'system' non stocke (toujours delivre)", "system" not in p)


if __name__ == "__main__":
    print("=== Push prefs (P5) — tests hermetiques ===")
    test_categorize()
    test_prefs_roundtrip()
    print(f"\n{'=' * 42}")
    if _failures:
        print(f"  {len(_failures)} test(s) EN ECHEC :")
        for f in _failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print("  Tous les tests passent.")
    raise SystemExit(0)
