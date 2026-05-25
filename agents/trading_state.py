"""
État global partagé entre l'orchestrateur et le bot Telegram.

Importable depuis n'importe quel module du même processus.
Pas de dépendances externes — module pur Python.
"""
from __future__ import annotations

_paused: bool = False


def is_paused() -> bool:
    """Retourne True si le trading est en pause."""
    return _paused


def pause() -> None:
    """Met le trading en pause (aucun ordre ne sera passé)."""
    global _paused
    _paused = True


def resume() -> None:
    """Reprend le trading."""
    global _paused
    _paused = False
